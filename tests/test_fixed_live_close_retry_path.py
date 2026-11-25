"""Actual Close selector/collector/owner/Store path with synthetic transports."""
import io,json,os,time,unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import collect_market_data as entry
import market_data_collection as runtime
import market_data_collectors as base
from fixed_live_owner_candidate import WorkflowFixedOwnerFactory
from dynamic_daily_owner import DynamicDailyOwner,DynamicPublicationUnknown
import test_fixed_live_owner_candidate as owner_fixture
import test_fixed_intraday_offline as fixture
from test_dynamic_daily_owner_integration import git

class FixedCloseRetryPathTests(unittest.TestCase):
    setUp=owner_fixture.FixedLiveOwnerTests.setUp
    make_owner=owner_fixture.FixedLiveOwnerTests.make_owner
    ledger=owner_fixture.FixedLiveOwnerTests.ledger
    def run_mode(self,mode,publish=False):
        day=fixture.fixture.DAY;stamp=day+'T15:59:00-05:00'
        fact=dict(symbol='AAA',asset_class='stocks',session='regular',source_timestamp=stamp,event_time=stamp,last_sale=11.,reported_volume=10.,source_contract='nasdaq_public_chart_last_sale_volume_v1')
        path,sha=runtime.write_capture(self.store,trade_date=day,session='regular',provider=runtime.NASDAQ,capture_id='prior-aaa',generated_at=day+'T16:00:00-05:00',actual_data_cutoff=stamp,window=dict(start='15:45',end='16:00'),feed_scope='nasdaq_public_chart_last_sale_volume_v1',qualified_facts=[fact],missing_symbols=[])
        runtime.write_snapshot(self.store,stage='close',trade_date=day,snapshot_id='prior-close',generated_at=day+'T16:01:00-05:00',data_refs=[dict(path=path,blob_sha=sha,provider=runtime.NASDAQ)],coverage={},missing=['BBB'],target_window=dict(start='15:45',end='16:00'))
        git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic prior Close AAA');git(self.repo,'push','origin','HEAD:main')
        factory=WorkflowFixedOwnerFactory(repo=self.repo,compute=self.compute,compute_sha=self.sha,run_id='123-1',invocation=dict(trigger='schedule'),clock=lambda:self.now,deadline=time.monotonic()+120)
        if publish:
            from fixed_stage_publisher_local import LocalStagePublisher
            publisher=LocalStagePublisher(factory=factory,day=day,eligible_universe=[('AAA','stocks'),('BBB','stocks')])
        symbols=[]
        def response(url,**kwargs):
            kwargs['before_request']()
            symbol=parse_qs(urlparse(url).query)['symbol'][0];symbols.append(symbol)
            self.assertEqual(json.loads(git(self.remote,'show',f'refs/heads/main:data/market-data/collector-state/2026-11/{day}-twelve-data-budget.json'))['credits'],1)
            rows=[dict(datetime=day+f' 15:{m:02d}:00',open='10',high='12',low='9',close='11',volume='100') for m in range(45,60)]
            return json.dumps(dict(meta=dict(symbol=symbol,interval='1min'),values=rows)).encode()
        root=fixture.fixture.oldtests.ROOT
        args=['--mode',mode,'--trade-date',day,'--store-root',str(self.store),'--universe',str(root/'config/collection-universe.json'),'--config',str(root/'config/market-data-store.yaml'),'--access-config',str(root/'config/market-data-collector-access.yaml')]
        original_check_output=entry.subprocess.check_output
        def compute_identity(cmd,*a,**kw):
            if cmd==['git','rev-parse','HEAD']:return self.sha+'\n'
            return original_check_output(cmd,*a,**kw)
        failures=[];original_finish=DynamicDailyOwner._finish
        def traced_finish(owner,*a,**kw):
            try:return original_finish(owner,*a,**kw)
            except Exception as exc:failures.append(str(exc));raise
        with patch.object(DynamicDailyOwner,'_finish',new=traced_finish),patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(entry,'_live_close_universe',return_value=[('AAA','stocks'),('BBB','stocks')]),patch.object(runtime,'fetch_nasdaq_regular',return_value=([],{'window_row_count':0,'target_window_match_count':0})) as primary,patch.object(base,'_http_raw',side_effect=response),redirect_stdout(io.StringIO()):
            try:
                if publish:
                    with patch.object(entry.subprocess,'check_output',side_effect=compute_identity),patch.object(entry,'_wait_until_regular_stage_cutoff'),patch.object(entry,'_settlement_gate_result',return_value=None):
                        self.assertEqual(entry.cli(args,owner_factory=factory,stage_publish=publisher),0)
                else:self.assertEqual(entry._run_live_close(args,owner_factory=factory),0)
            except DynamicPublicationUnknown as exc:raise AssertionError('real finish failures: '+repr(failures)) from exc
        if publish:
            self.assertEqual(publisher.publisher.head,git(self.remote,'rev-parse','refs/heads/main'))
            manifest=json.loads(git(self.remote,'show','refs/heads/main:'+publisher.last_manifest))
            self.assertIn(f'collector-state/{day[:7]}/{day}-close-missing.json',[r['path'] for r in manifest['required_outputs']])
            self.assertEqual(git(self.repo,'status','--porcelain'),'')
        self.assertEqual(symbols,['BBB']);self.assertEqual([c.args[0] for c in primary.call_args_list],['BBB'])
        owner=factory.latest_owner;self.assertIsNone(owner.active)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        terminals=[p for p in paths if p.endswith('-terminal.json')];self.assertEqual(len(terminals),1)
        terminal=json.loads(git(self.remote,'show','refs/heads/main:'+terminals[0]))
        self.assertEqual(terminal['symbol'],'BBB');self.assertEqual(terminal['status'],'SELECTED_TWELVE')
        capture=json.loads((self.store/terminal['selected_capture']).read_bytes())
        self.assertTrue(capture['capture_id'].startswith(mode+'-bbb-'))
        self.assertEqual(terminal['intraday_scope']['stage'],'close')
        self.assertEqual(entry._live_close_retry_symbols(self.store,day,[('AAA','stocks'),('BBB','stocks')]),[])
    def test_close_retry_real_path_keeps_missing_only_and_closes_capture(self):self.run_mode('close_retry')
    def test_close_final_real_path_keeps_missing_only_and_closes_capture(self):self.run_mode('close_final')
    def test_wrong_collection_mode_capture_prefix_still_rejected(self):
        original=runtime._write_regular_capture
        def wrong_mode(*args,**kwargs):
            values=list(args);values[2]='close_final'
            return original(*values,**kwargs)
        with patch.object(runtime,'_write_regular_capture',side_effect=wrong_mode),self.assertRaisesRegex(AssertionError,'intraday capture identity'):
            self.run_mode('close_retry')
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertFalse(any(p.endswith('-terminal.json') for p in paths))
        self.assertEqual(self.ledger()['credits'],1)

    def test_real_close_retry_cli_publishes_final_manifest_locally(self):self.run_mode('close_retry',publish=True)
