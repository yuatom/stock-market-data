"""Actual local data/ledger; synthetic provider responses, no external access."""
import copy,json,os,tempfile,unittest,subprocess,shutil
import yaml
from pathlib import Path
from unittest.mock import patch
import collect_dynamic_candidates as d
import test_dynamic_candidates as existing

def raw_adapter(fetch):
    def response(symbol,*args,**kwargs):
        rows=fetch(symbol,*args,**kwargs)
        return json.dumps(dict(status='ok',meta=dict(symbol=symbol,interval='1day'),values=[dict(datetime=r['trade_date'],**{k:str(v) for k,v in r.items() if k!='trade_date'}) for r in rows])).encode()
    return response

class DynamicDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.old=dict(trade_date='2026-11-12',open=10.,high=12.,low=9.,close=11.,volume=100.)
        self.new=dict(self.old,trade_date='2026-11-13');self.symbols=['AAA','BBB'];self.before={}
        for symbol in self.symbols:
            d.store.append_daily_bars(self.root,provider=d.collectors.TWELVE_PROVIDER,symbol=symbol,asset_class='stocks',bars=[self.old],series_semantics='daily_regular_ohlcv')
            self.before[symbol]=d.store.read_daily_series(self.root,provider=d.collectors.TWELVE_PROVIDER,symbol=symbol)
        self.config={'collector':{'twelve_data_daily':{'overlap_revision_policy':'price_only_volume_blocked_v1'},'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}}}}
        self.access={'twelve_data_basic':{'api_key_env':'TWELVE_DATA_API_KEY'}}
    def invoke(self,fetch,**kwargs):
        info={}
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(d.daily_response,'fetch_raw',side_effect=raw_adapter(fetch)) as call:
            result=d._ensure_daily_history(symbols=self.symbols,trade_date='2026-11-14',store_root=self.root,config=self.config,access=self.access,target_records=256,diagnostics=info,**kwargs)
        return result,info,call.call_count
    def index(self,symbol):return json.loads(d.collectors._series_index_path(self.root,d.collectors.TWELVE_PROVIDER,symbol).read_text())
    def rows(self,symbol):return d.store.read_daily_series(self.root,provider=d.collectors.TWELVE_PROVIDER,symbol=symbol)
    def fetch(self,symbol,*a,**kw):return [dict(self.old,volume=101 if symbol=='AAA' else 100),self.new]
    def test_volume_only_isolation_clean_other_symbol_and_old_rows(self):
        (available,failures),info,count=self.invoke(self.fetch)
        self.assertEqual(count,2);self.assertEqual(available,self.symbols);self.assertEqual(failures,[])
        self.assertEqual(self.index('AAA')['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertNotIn('volume_policy',self.index('BBB')['lineage_segments'][-1])
        for s in self.symbols:self.assertEqual(self.rows(s)[0],self.old)
        self.assertEqual(info['local_budget_debits'],2);self.assertFalse(info['remote_predebit_established'])
        self.assertEqual(json.loads(d.collectors._budget_path(self.root,'2026-11-14').read_text())['credits'],2)
        self.assertEqual(info['provider_charge_status'],'LOCAL_REQUESTS_ACCOUNTED_PROVIDER_BILLING_NOT_VERIFIED')
    def test_known_price_and_volume_conflict_isolated_then_other_symbol_continues(self):
        (_,failures),info,count=self.invoke(lambda symbol,*a,**k:[dict(self.old,high=12.1,volume=101),self.new] if symbol=='AAA' else [self.old,self.new])
        self.assertEqual(count,2);self.assertFalse(info['stopped']);self.assertEqual(self.rows('AAA'),[self.old])
        self.assertEqual(failures[0]['reason'],'MarketDataConflict:conflicting bar for AAA 2026-11-12')
        self.assertEqual(self.index('AAA')['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(self.rows('BBB'),[self.old,self.new]);self.assertEqual(info['known_price_rejected_symbols'],['AAA'])
        self.assertEqual(info['local_budget_debits'],2)
        self.assertTrue(any('price-only-' in p for p in info['required_paths']))
    def test_price_only_conflict_does_not_disable_volume(self):
        _,info,count=self.invoke(lambda *a,**k:[dict(self.old,high=12.1),self.new])
        self.assertEqual(count,2);self.assertFalse(info['stopped']);self.assertFalse(self.index('AAA').get('lineage_segments'))
    def test_second_symbol_failure_preserves_first_quarantine_and_ledger(self):
        def fetch(symbol,*a,**kw):
            if symbol=='BBB':raise TimeoutError('https://secret.invalid?apikey=DO_NOT_LEAK')
            return self.fetch(symbol)
        _,info,count=self.invoke(fetch)
        self.assertEqual(count,2);self.assertEqual(info['local_budget_debits'],1);self.assertEqual(info['provider_charge_status'],'UNKNOWN')
        self.assertEqual(json.loads(d.collectors._budget_path(self.root,'2026-11-14').read_text())['credits'],1)
        self.assertEqual(self.index('AAA')['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertEqual(len(self.rows('AAA')),2);self.assertNotIn('DO_NOT_LEAK',json.dumps(info))
    def test_existing_block_preserved_and_evidence_repeat_idempotent(self):
        self.invoke(self.fetch);before=self.index('AAA')
        (_,failures),info,count=self.invoke(self.fetch)
        self.assertEqual(failures,[]);self.assertEqual(self.index('AAA'),before)
    def test_metadata_conflict_keeps_price_and_stops(self):
        _,info,count=self.invoke(lambda *a,**k:[dict(self.old,source_timestamp='changed'),self.new])
        self.assertEqual(count,1);self.assertTrue(info['stopped']);self.assertEqual(self.rows('AAA'),[self.old])
    def test_stored_quality_invalid_is_not_trusted_price_rejection(self):
        # The legacy Store permits close-only rows. Exercise that actual format.
        self.symbols=['CCC','BBB']
        d.store.append_daily_bars(self.root,provider=d.collectors.TWELVE_PROVIDER,symbol='CCC',asset_class='stocks',bars=[dict(trade_date='2026-11-12',close=11.)],series_semantics='daily_regular_ohlcv')
        (_,failures),info,count=self.invoke(lambda *a,**kw:[self.old,self.new])
        self.assertEqual(count,1);self.assertTrue(info['stopped']);self.assertEqual(info['local_budget_debits'],1)
        self.assertNotIn('known_price_rejected_symbols',info)
        self.assertEqual(self.rows('CCC'),[dict(trade_date='2026-11-12',close=11.)])
        self.assertEqual(self.rows('BBB'),[self.old])
    def test_absent_policy_preserves_exact_legacy_recovery_reason(self):
        self.config['collector']['twelve_data_daily'].pop('overlap_revision_policy')
        (_,failures),info,count=self.invoke(lambda *a,**kw:[dict(self.old,high=12.1),self.new])
        self.assertEqual(count,1)
        self.assertEqual(failures[0]['reason'],'MarketDataConflict:conflicting bar for AAA 2026-11-12')
        self.assertEqual(self.rows('AAA'),[self.old])
    def test_debit_failure_retains_normalized_response_and_stops(self):
        with patch.object(d.collectors,'consume_twelve_credit',side_effect=RuntimeError('private token')):
            (available,failures),info,count=self.invoke(self.fetch)
        self.assertEqual(count,1);self.assertEqual(info['local_budget_debits'],0);self.assertTrue(info['stopped'])
        self.assertEqual(self.rows('AAA'),[self.old]);self.assertTrue(any('price-only-' in p for p in info['required_paths']))
        self.assertNotIn('private token',json.dumps(failures));self.assertEqual(info['provider_charge_status'],'UNKNOWN')
    def test_stop_manifest_binds_retained_response_quarantine_shards_and_actual_budget(self):
        request=existing.DynamicCandidateRequestTests()._request();request.update(trade_date='2026-11-14',candidate_symbols=self.symbols)
        def fetch(symbol,*a,**k):
            if symbol=='BBB':raise TimeoutError('synthetic transport unknown')
            return [dict(self.old,high=12.1,volume=101),self.new]
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(d.daily_response,'fetch_raw',side_effect=raw_adapter(fetch)),patch.object(d,'collect_regular_window') as intraday:
            result=d.collect_request(request=request,store_root=self.root,store_config_path=existing.ROOT/'config/market-data-store.yaml',access_path=existing.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=existing.ROOT/'config/dynamic-candidate-collection.yaml')
        intraday.assert_not_called();self.assertEqual(result['status'],'STOPPED');self.assertFalse(result['market_fact_authority'])
        manifest=json.loads((self.root/result['publication_manifest_path']).read_text());paths=[x['path'] for x in manifest['required_outputs']]
        self.assertTrue(any('price-only-' in p for p in paths));self.assertTrue(any('_index.json' in p for p in paths));self.assertTrue(any('twelve-data-budget' in p for p in paths))
        for item in manifest['required_outputs']:
            raw=(self.root/item['path']).read_bytes();self.assertEqual(item['blob_sha'],d.hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest())
        # Exercise real Git publication and existing durability verifier for STOPPED.
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);remote=root/'remote.git';writer=root/'writer'
            subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
            subprocess.run(['git','init','-q',str(writer)],check=True)
            def git(*args):return subprocess.check_output(['git','-C',str(writer),*args],text=True,stderr=subprocess.DEVNULL).strip()
            git('config','user.name','test');git('config','user.email','test@example.invalid');git('remote','add','origin',str(remote))
            shutil.copytree(self.root,writer/'data/market-data');git('add','.');git('commit','-qm','retained stopped result');git('branch','-M','main');git('push','-q','origin','main')
            receipt=existing.durability.verify_publication(writer,git('rev-parse','HEAD'),required_manifest='data/market-data/'+result['publication_manifest_path'])
            self.assertEqual(receipt['status'],'durability_verified')
    def test_known_price_rejection_allows_other_daily_and_independent_intraday_partial(self):
        request=existing.DynamicCandidateRequestTests()._request();request.update(trade_date='2026-11-14',candidate_symbols=self.symbols)
        def fetch(symbol,*a,**kw):return [dict(self.old,high=12.1) if symbol=='AAA' else self.old,self.new]
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(d.daily_response,'fetch_raw',side_effect=raw_adapter(fetch)) as capture,patch.object(d,'collect_regular_window',return_value={'status':'ok','snapshot_written':False}) as intraday:
            result=d.collect_request(request=request,store_root=self.root,store_config_path=existing.ROOT/'config/market-data-store.yaml',access_path=existing.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=existing.ROOT/'config/dynamic-candidate-collection.yaml')
        self.assertEqual(capture.call_count,2);intraday.assert_called_once()
        self.assertEqual(result['status'],'partial');self.assertEqual(result['daily_history_available'],['BBB'])
        self.assertEqual(result['daily_history_execution']['local_budget_debits'],2)
    def test_workflow_fails_business_only_after_persist_and_durability(self):
        # Exercise the current CLI's ordering, rather than indexing obsolete
        # outer workflow publication steps. Auth/provider are synthetic here;
        # actual finalizer durability is tested in test_data_plane_write_boundary.
        import contextlib, io
        from types import SimpleNamespace
        import run_dynamic_guarded as runtime
        from dynamic_daily_owner import DynamicPublicationUnknown
        events = []
        owner = SimpleNamespace(request={})
        def abort():
            events.append('abort')
            raise DynamicPublicationUnknown('synthetic unknown')
        owner.abort = abort
        def collect(**kwargs):
            events.append('collect')
            return {'status': 'STOPPED'}
        def finalize(writer, result):
            self.assertIs(writer, owner)
            events.append('finalize')
            return dict(result, publication_status='DURABILITY_VERIFIED')
        def git(repo, *args):
            if args[0] in ('status', 'ls-files'): return ''
            if args[0] == 'ls-remote': return 'a'*40 + ' refs/heads/main'
            return 'a'*40
        argv = ['--control', '/synthetic/control', '--control-ref', 'a'*40, '--store', '/synthetic/store']
        output = io.StringIO()
        with patch.object(runtime, 'origin'), patch.object(runtime.base, 'git', side_effect=git), patch.object(runtime.authority, 'attest', return_value={'run_id': '123'}), patch.object(runtime, 'DynamicDailyOwner', return_value=owner), patch.object(d, 'collect_request', side_effect=collect), patch.object(runtime, 'finalize', side_effect=finalize), contextlib.redirect_stdout(output):
            self.assertEqual(runtime.main(argv), 2)
        self.assertEqual(events, ['collect', 'finalize'])
        self.assertEqual(json.loads(output.getvalue())['publication_status'], 'DURABILITY_VERIFIED')
        events.clear(); output = io.StringIO()
        with patch.object(runtime, 'origin'), patch.object(runtime.base, 'git', side_effect=git), patch.object(runtime.authority, 'attest', return_value={'run_id': '123'}), patch.object(runtime, 'DynamicDailyOwner', return_value=owner), patch.object(d, 'collect_request', side_effect=collect), patch.object(runtime, 'finalize', side_effect=RuntimeError('synthetic unknown')), contextlib.redirect_stdout(output):
            with self.assertRaises(DynamicPublicationUnknown): runtime.main(argv)
        self.assertEqual(events, ['collect', 'abort'])
        self.assertEqual(output.getvalue(), '')


if __name__=='__main__':unittest.main()
