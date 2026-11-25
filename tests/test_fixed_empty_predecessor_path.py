"""No-snapshot predecessor compatibility with actual local durable publication."""
import io,json,os,unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import test_fixed_known_rejection_path as fixed
from test_dynamic_daily_owner_integration import git,DAY
from dynamic_daily_owner import DynamicPublicationUnknown
import collect_market_data as entry
import market_data_collection as runtime
import market_data_collectors as base

class EmptyPredecessorTests(unittest.TestCase):
    setUp=fixed.KnownRejectionPathTests.setUp
    make_owner=fixed.KnownRejectionPathTests.make_owner
    setup_path=fixed.KnownRejectionPathTests.setup_path
    def run_chain(self,unknown=False,all_empty=False,primary_final=False):
        self.setup_path();self.args[1]='open_60m';calls=[];manifests=[]
        def raw(url,**kwargs):
            kwargs['before_request']();query=parse_qs(urlparse(url).query)
            start=query['start_date'][0][-8:-3];symbol=query['symbol'][0];calls.append((start,symbol))
            if unknown:raise TimeoutError('synthetic unknown')
            rows=[] if start!='10:00' or all_empty else [dict(datetime=DAY+f' 10:{m:02d}:00',open='10',high='12',low='9',close='11',volume='100') for m in range(30)]
            return json.dumps(dict(meta=dict(symbol=symbol,interval='1min'),values=rows)).encode()
        def primary(symbol,asset,day,start,end,access):
            if not primary_final or start!='10:00':return [],{'window_row_count':0,'target_window_match_count':0}
            facts=[]
            for m in range(30):
                stamp=DAY+f'T10:{m:02d}:00-05:00'
                facts.append(dict(source_contract='nasdaq_public_chart_last_sale_volume_v1',symbol=symbol,asset_class=asset,session='regular',event_time=stamp,source_timestamp=stamp,last_sale=11.,reported_volume=10.))
            return facts,{'window_row_count':30,'target_window_match_count':30}
        def publish(args):
            result=self.publisher(args);manifests.append(self.publisher.last_manifest);return result
        original=entry.subprocess.check_output
        def identity(cmd,*a,**kw):
            if cmd==['git','rev-parse','HEAD']:return self.sha+'\n'
            return original(cmd,*a,**kw)
        try:
            with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(entry,'_settlement_gate_result',return_value=None),patch.object(entry,'_wait_until_regular_stage_cutoff'),patch.object(entry.subprocess,'check_output',side_effect=identity),patch.object(runtime.collection,'intraday_universe',return_value=[('AAA','stocks'),('BBB','stocks')]),patch.object(runtime,'fetch_nasdaq_regular',side_effect=primary),patch.object(base,'_http_raw',side_effect=raw),redirect_stdout(io.StringIO()):
                self.assertEqual(entry.cli(self.args,owner_factory=self.factory,stage_publish=publish),0)
        finally:self.chain_calls=calls;self.chain_manifests=manifests
    def assert_chain(self,all_empty):
        self.assertEqual([x[0] for x in self.chain_calls],['09:30','09:30','09:45','09:45','10:00','10:00'])
        head=git(self.remote,'rev-parse','refs/heads/main');self.assertEqual(self.publisher.publisher.head,head)
        self.assertEqual(len(self.chain_manifests),3)
        for stage in ['open_15m','open_30m']+(['open_60m'] if all_empty else []):
            self.assertFalse((self.store/f'snapshots/2026-11/{DAY}/{stage}/latest.json').exists())
            self.assertFalse((self.store/f'proofs/deterministic-metrics/2026-11/{DAY}/{stage}/latest.json').exists())
        for relative in self.chain_manifests:
            manifest=json.loads(git(self.remote,'show',head+':'+relative))
            self.assertTrue(manifest['required_outputs'])
        final=json.loads(git(self.remote,'show',head+':'+self.chain_manifests[-1]))
        paths=[ref['path'] for ref in final['required_outputs']]
        self.assertEqual(sum(p.endswith('-terminal.json') for p in paths),6)
        # Content-addressed identical empty responses are shared, not duplicate charges.
        self.assertEqual(sum(p.endswith('.raw') for p in paths),2 if all_empty else 4)
        self.assertTrue(all(p.removeprefix('data/market-data/') in paths for p in self.chain_manifests[:-1]))
        for ref in final['required_outputs']:self.assertEqual(git(self.remote,'rev-parse',head+':data/market-data/'+ref['path']),ref['blob_sha'])
        budget=json.loads(git(self.remote,'show',head+':data/market-data/'+f'collector-state/2026-11/{DAY}-twelve-data-budget.json'))
        self.assertEqual(budget['credits'],6)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
    def test_two_empty_predecessors_continue_to_real_open60_snapshot(self):
        self.run_chain();self.assert_chain(False)
        self.assertTrue((self.store/f'snapshots/2026-11/{DAY}/open_60m/latest.json').exists())
    def test_all_empty_retains_accounting_without_fake_snapshot_or_proof(self):
        self.run_chain(all_empty=True);self.assert_chain(True)
    def test_unknown_first_predecessor_still_stops_chain(self):
        with self.assertRaises(DynamicPublicationUnknown):self.run_chain(unknown=True)
        self.assertEqual(self.chain_calls,[('09:30','AAA')]);self.assertEqual(self.chain_manifests,[])
    def test_original_manifest_builder_and_verifier_accept_inventory_without_snapshot(self):
        from build_market_data_inventory import build_inventory
        from build_store_publication_manifest import build_manifest
        from verify_store_publication import verify_publication
        p=self.store/'collector-state/market-data-inventory.json';p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(build_inventory(self.store)))
        result=self.repo.parent/'empty-result.json';result.write_text(json.dumps({'status':'no_new_qualified_facts','snapshot_written':False}))
        manifest=build_manifest(store_root=self.store,result_file=result,request_id='synthetic',trade_date=DAY,stage='open_15m')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic legacy no-snapshot closure');git(self.repo,'push','origin','HEAD:main')
        receipt=verify_publication(self.repo,git(self.repo,'rev-parse','HEAD'),required_manifest=manifest.relative_to(self.repo).as_posix())
        self.assertEqual(receipt['status'],'durability_verified')
        self.assertFalse(list(self.store.glob('snapshots/**/latest.json')))

    def test_empty_stage_publication_uncertainty_stops_next_predecessor(self):
        from fixed_stage_publisher_local import LocalStagePublisher
        with patch.object(LocalStagePublisher,'_verify',side_effect=RuntimeError('synthetic stage retention unknown')):
            with self.assertRaisesRegex(RuntimeError,'stage retention unknown'):
                self.run_chain()
        self.assertEqual(self.chain_calls,[('09:30','AAA'),('09:30','BBB')])
        self.assertEqual(self.chain_manifests,[])
        self.assertTrue(self.publisher.failed)

    def test_empty_finalizer_cannot_hide_an_unclosed_attempt(self):
        from fixed_finalizer_offline import build_candidate,FinalizerError
        with self.assertRaises(DynamicPublicationUnknown):self.run_chain(unknown=True)
        with self.assertRaisesRegex(FinalizerError,'unknown or mismatched attempt'):
            build_candidate(repo=self.repo,start_sha=self.start,confirmed_head=git(self.repo,'rev-parse','HEAD'),day=DAY,stage='open_15m',request_id='123-1',run_id='123-1',compute_sha=self.sha,snapshot_path=None,profile='fixed_live',collection_mode='open_15m')

    def test_empty_finalizer_rejects_cross_request_prior_manifest(self):
        from fixed_finalizer_offline import build_candidate,FinalizerError
        with self.assertRaisesRegex(FinalizerError,'prior publication scope mismatch'):
            build_candidate(repo=self.repo,start_sha=self.start,confirmed_head=self.start,day=DAY,stage='open_30m',request_id='123-1',run_id='123-1',compute_sha=self.sha,snapshot_path=None,profile='fixed_live',collection_mode='open_30m',previous_manifest=f'data/market-data/collector-state/2026-11/{DAY}-open_15m-wrongrequest-publication.json')

    def test_paid_predecessors_zero_paid_final_includes_current_budget_blob(self):
        self.run_chain(primary_final=True)
        self.assertEqual(self.chain_calls,[('09:30','AAA'),('09:30','BBB'),('09:45','AAA'),('09:45','BBB')])
        self.assertEqual(len(self.chain_manifests),3)
        head=git(self.remote,'rev-parse','refs/heads/main')
        manifest=json.loads(git(self.remote,'show',head+':'+self.chain_manifests[-1]))
        refs={r['path']:r['blob_sha'] for r in manifest['required_outputs']}
        budget=f'collector-state/2026-11/{DAY}-twelve-data-budget.json'
        self.assertEqual(json.loads(git(self.remote,'show',head+':data/market-data/'+budget))['credits'],4)
        self.assertIn(budget,refs)
        self.assertEqual(refs[budget],git(self.remote,'rev-parse',head+':data/market-data/'+budget))
        self.assertEqual(sum(p.endswith('-terminal.json') for p in refs),4)
        for prior_path in self.chain_manifests[:-1]:
            prior=json.loads(git(self.remote,'show',head+':'+prior_path))
            for ref in prior['required_outputs']:
                if ref['path'] not in {budget,'collector-state/market-data-inventory.json'}:
                    self.assertEqual(refs[ref['path']],ref['blob_sha'])
        for path,sha in refs.items():
            self.assertEqual(sha,git(self.remote,'rev-parse',head+':data/market-data/'+path))
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
