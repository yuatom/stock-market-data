"""Real fixed CLI/owner/finalizer/publisher; synthetic provider, local Git only."""
import io,json,os,time,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import collect_market_data as entry
import market_data_collection as runtime
import market_data_collectors as base
from fixed_live_owner_candidate import WorkflowFixedOwnerFactory
from fixed_stage_publisher_local import LocalStagePublisher
from dynamic_daily_owner import DynamicPublicationUnknown
import test_fixed_live_owner_candidate as fixture
from test_dynamic_daily_owner_integration import git,DAY
ROOT=Path(__file__).resolve().parents[1]

class KnownRejectionPathTests(unittest.TestCase):
    setUp=fixture.FixedLiveOwnerTests.setUp
    make_owner=fixture.FixedLiveOwnerTests.make_owner
    def setup_path(self):
        self.factory=WorkflowFixedOwnerFactory(repo=self.repo,compute=self.compute,compute_sha=self.sha,run_id='123-1',invocation={'trigger':'schedule'},clock=lambda:self.now,deadline=time.monotonic()+120)
        self.publisher=LocalStagePublisher(factory=self.factory,day=DAY)
        self.symbols=[]
        self.args=['--mode','open_15m','--trade-date',DAY,'--store-root',str(self.store),'--universe',str(ROOT/'config/collection-universe.json'),'--config',str(ROOT/'config/market-data-store.yaml'),'--access-config',str(ROOT/'config/market-data-collector-access.yaml')]
    def primary(self,symbol,*args):
        stamp=DAY+'T09:44:00-05:00'
        return [dict(source_contract='nasdaq_public_chart_last_sale_volume_v1',symbol=symbol,asset_class='stocks',session='regular',event_time=stamp,source_timestamp=stamp,last_sale=11.,reported_volume=10.)], {'window_row_count':1,'target_window_match_count':1}
    def synthetic_response(self,url,**kwargs):
        kwargs['before_request']();symbol=parse_qs(urlparse(url).query)['symbol'][0]
        if self.symbols:
            paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
            self.assertEqual(sum(p.endswith('-terminal.json') for p in paths),len(self.symbols))
        self.symbols.append(symbol)
        budget=json.loads(git(self.remote,'show',f'refs/heads/main:data/market-data/collector-state/2026-11/{DAY}-twelve-data-budget.json'))
        self.assertEqual(budget['credits'],len(self.symbols))
        return b'{"status":"error","message":"synthetic quota denial"}'
    def run_path(self,raw=None):
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(entry,'_settlement_gate_result',return_value=None),patch.object(entry,'_wait_until_regular_stage_cutoff'),patch.object(runtime.collection,'intraday_universe',return_value=[('AAA','stocks'),('BBB','stocks')]),patch.object(runtime,'fetch_nasdaq_regular',side_effect=self.primary),patch.object(base,'_http_raw',side_effect=raw or self.synthetic_response),redirect_stdout(io.StringIO()):
            return entry.cli(self.args,owner_factory=self.factory,stage_publish=self.publisher)
    def test_known_quota_rejection_preserves_partial_snapshot_through_final_publish(self):
        self.setup_path();self.assertEqual(self.run_path(),0)
        self.assertEqual(self.symbols,['AAA','BBB'])
        self.assertFalse(self.factory.latest_owner.failed);self.assertIsNone(self.factory.latest_owner.active)
        head=git(self.remote,'rev-parse','refs/heads/main')
        manifest=json.loads(git(self.remote,'show',head+':'+self.publisher.last_manifest))
        paths=[r['path'] for r in manifest['required_outputs']]
        self.assertEqual(sum(p.endswith('.raw') for p in paths),2)
        terminals=[p for p in paths if p.endswith('-terminal.json')];self.assertEqual(len(terminals),2)
        for path in terminals:self.assertEqual(json.loads(git(self.remote,'show',head+':data/market-data/'+path))['status'],'REJECTED')
        pointer=json.loads((self.store/f'snapshots/2026-11/{DAY}/open_15m/latest.json').read_bytes())
        snapshot=json.loads((self.store/pointer['snapshot_path']).read_bytes())
        self.assertEqual(sorted(snapshot['coverage']['stage_snapshot']['partial_symbols']),['AAA','BBB'])
        self.assertEqual(snapshot['missing'],[])
        self.assertTrue(all(ref['provider']=='nasdaq_public_intraday' for ref in snapshot['data_refs']))
        for ref in manifest['required_outputs']:self.assertEqual(git(self.remote,'rev-parse',head+':data/market-data/'+ref['path']),ref['blob_sha'])
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
    def test_unknown_http_still_stops_before_snapshot_or_second_symbol(self):
        self.setup_path()
        def unknown(url,**kwargs):self.synthetic_response(url,**kwargs);raise TimeoutError('synthetic uncertainty')
        with self.assertRaises(DynamicPublicationUnknown):self.run_path(unknown)
        self.assertEqual(self.symbols,['AAA']);self.assertTrue(self.factory.latest_owner.failed)
        self.assertIsNone(self.publisher.last_manifest)
        self.assertFalse((self.store/f'snapshots/2026-11/{DAY}/open_15m/latest.json').exists())
    def test_terminal_verification_unknown_still_stops(self):
        self.setup_path();original=self.factory.__class__.__call__
        def make(factory,**kwargs):
            owner=original(factory,**kwargs);verify=owner.verify
            def checked(repo,sha):
                paths=git(repo,'ls-tree','-r','--name-only',sha).splitlines()
                if any(p.endswith('-terminal.json') for p in paths):raise RuntimeError('synthetic terminal readback unknown')
                return verify(repo,sha)
            owner.verify=checked;return owner
        with patch.object(self.factory.__class__,'__call__',new=make),self.assertRaises(DynamicPublicationUnknown):self.run_path()
        self.assertEqual(self.symbols,['AAA']);self.assertTrue(self.factory.latest_owner.failed)
        self.assertIsNone(self.publisher.last_manifest)
        self.assertFalse((self.store/f'snapshots/2026-11/{DAY}/open_15m/latest.json').exists())

    def test_legacy_same_sparse_primary_and_provider_error_remains_partial(self):
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(runtime,'fetch_nasdaq_regular',side_effect=self.primary),patch.object(base,'_http_json',return_value={'status':'error','message':'synthetic quota denial'}):
            result=runtime.collect_regular_window(mode='open_15m',stage=None,trade_date=DAY,start_et='09:30',end_et='09:45',store_root=self.store,universe_config={},config=base.load_yaml(ROOT/'config/market-data-store.yaml'),access=base.load_yaml(ROOT/'config/market-data-collector-access.yaml'),eligible_universe=[('AAA','stocks'),('BBB','stocks')])
        self.assertEqual(result['status'],'partial');self.assertTrue(result['snapshot_written'])
        self.assertEqual(result['coverage']['stage_snapshot']['partial_symbols'],['AAA','BBB'])
