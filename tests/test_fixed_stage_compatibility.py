"""Static Store fixtures only: no collection, HTTP, remote or workflow execution."""
import hashlib,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dynamic_daily_owner import encode,blob
from market_data_store import write_capture,write_snapshot
from build_deterministic_metric_proof import build_proof,_write_json,_proof_pointer
from fixed_finalizer_offline import build_candidate,FinalizerError
from test_dynamic_daily_owner_integration import git
DAY='2026-11-17';RUN='900-1';COMPUTE='a'*40
class StageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name).resolve()/'repo';self.repo.mkdir();self.store=self.repo/'data/market-data';self.store.mkdir(parents=True)
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        git(self.repo,'config','user.name','synthetic');git(self.repo,'config','user.email','test@example.invalid')
        (self.store/'seed.json').write_bytes(b'{}');self.commit();self.start=git(self.repo,'rev-parse','HEAD')
    def commit(self):git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic fixture')
    def prepare(self,stage='open_15m',provider='nasdaq_public_intraday'):
        start,end=('15:45','16:00') if stage=='close' else ('09:30','09:45')
        stamp=DAY+('T15:59:00-05:00' if stage=='close' else 'T09:44:00-05:00')
        fact=dict(source_contract='nasdaq_public_chart_last_sale_volume_v1',symbol='AAA',asset_class='stocks',session='regular',event_time=stamp,source_timestamp=stamp,last_sale=11.,reported_volume=10.)
        path,sha=write_capture(self.store,trade_date=DAY,session='regular',provider=provider,capture_id=stage+'-aaa-synthetic',generated_at=DAY+'T17:00:00-05:00',actual_data_cutoff=stamp,window=dict(start=start,end=end),feed_scope='nasdaq_public_chart_last_sale_volume_v1',qualified_facts=[fact],missing_symbols=[])
        self.capture=path
        self.snapshot,_=write_snapshot(self.store,stage=stage,trade_date=DAY,snapshot_id=stage+'-synthetic',generated_at=DAY+'T17:01:00-05:00',data_refs=[dict(path=path,blob_sha=sha,provider=provider)],coverage={},missing=[],target_window=dict(start=start,end=end))
        if stage=='close':
            proof,rel=build_proof(self.store,trade_date=DAY,stage=stage,data_plane_commit_sha=COMPUTE)
            psha=_write_json(self.store/rel,proof)
            _write_json(self.store/f'proofs/deterministic-metrics/2026-11/{DAY}/close/latest.json',_proof_pointer(proof,rel=rel,blob=psha))
        self.args=dict(repo=self.repo,start_sha=self.start,confirmed_head=git(self.repo,'rev-parse','HEAD'),day=DAY,stage=stage,request_id=RUN,run_id=RUN,transaction_id=None,compute_sha=COMPUTE,snapshot_path=self.snapshot,profile='fixed_live',business_request_id=None,collection_mode=stage)
    def test_all_nasdaq_open15_has_no_fabricated_attempt_or_budget(self):
        self.prepare();out=build_candidate(**self.args);paths=[x['path'] for x in json.loads(out.read_bytes())['required_outputs']]
        self.assertIn(self.capture,paths);self.assertIn('collector-state/market-data-inventory.json',paths)
        self.assertFalse(any('attempt-' in p or 'twelve-data-budget' in p for p in paths))
    def test_close_zero_paid_attempt_uses_existing_proof_rules(self):
        self.prepare('close');out=build_candidate(**self.args)
        self.assertTrue(any(x['path'].startswith('proofs/') for x in json.loads(out.read_bytes())['required_outputs']))
    def test_nothing_missing_close_reuses_existing_snapshot(self):
        self.prepare('close');self.commit();head=git(self.repo,'rev-parse','HEAD')
        self.args.update(start_sha=head,confirmed_head=head,collection_mode='close_retry',nothing_missing=True,eligible_universe=[('AAA','stocks')])
        out=build_candidate(**self.args)
        self.assertTrue(out.exists());self.assertEqual(git(self.repo,'rev-parse','HEAD'),head)
    def test_false_nothing_missing_rejected(self):
        self.prepare('close');self.commit();head=git(self.repo,'rev-parse','HEAD')
        self.args.update(start_sha=head,confirmed_head=head,collection_mode='close_retry',nothing_missing=True,eligible_universe=[('AAA','stocks'),('BBB','stocks')])
        with self.assertRaisesRegex(FinalizerError,'nothing_missing'):build_candidate(**self.args)
    def test_unreferenced_capture_rejected(self):
        self.prepare();p=self.store/self.capture;(p.parent/'unreferenced.json').write_bytes(p.read_bytes())
        with self.assertRaisesRegex(FinalizerError,'unexpected uncommitted'):build_candidate(**self.args)
    def paid_attempt(self,*,closed=True,run=RUN):
        # Commit only synthetic attempt artifacts/capture, leaving snapshot new.
        self.prepare(provider='twelve_data_basic')
        scope=dict(provider='twelve_data_basic',stage='open_15m',trade_date=DAY,start='09:30',end='09:45',interval='1min')
        token=hashlib.sha256((RUN+'\0AAA').encode()+b'\0'+encode(scope)).hexdigest()
        claim=f'collector-state/fixed-intraday-attempt-{token}.json'
        terminal=claim[:-5]+'-terminal.json';raw=b'{"synthetic":true}'
        rawpath='collector-state/2026-11/fixed-intraday-AAA-'+hashlib.sha256(raw).hexdigest()+'.raw'
        artifacts={claim:encode(dict(mode='fixed_intraday_attempt',business_request_id=None,execution_day=DAY,compute_sha=COMPUTE,run_id=run,symbol='AAA',intraday_scope=scope)),rawpath:raw}
        if closed:artifacts[terminal]=encode(dict(symbol='AAA',intraday_scope=scope,status='SELECTED_TWELVE',reservation='DEBIT_CONFIRMED',selected_capture=self.capture))
        artifacts[self.capture]=(self.store/self.capture).read_bytes()
        ledger=f'collector-state/2026-11/{DAY}-twelve-data-budget.json'
        artifacts[ledger]=encode(dict(request_id=RUN,trade_date=DAY,stage='fixed_intraday_attempt',credits=1,required_outputs=[dict(path=p,blob_sha=blob(raw)) for p,raw in artifacts.items()]))
        for p,raw in artifacts.items():
            target=self.store/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        git(self.repo,'add',*[str(Path('data/market-data')/p) for p in artifacts]);git(self.repo,'commit','-qm','synthetic paid closure')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
    def test_live_paid_closure_with_null_business_identity(self):
        self.paid_attempt();self.assertTrue(build_candidate(**self.args).exists())
    def test_unclosed_live_attempt_rejected(self):
        self.paid_attempt(closed=False)
        with self.assertRaisesRegex(FinalizerError,'attempt'):build_candidate(**self.args)
    def test_wrong_live_run_rejected(self):
        self.paid_attempt(run='901-1')
        with self.assertRaisesRegex(FinalizerError,'attempt'):build_candidate(**self.args)
    def test_budget_without_attempt_rejected(self):
        self.prepare();p=self.store/f'collector-state/2026-11/{DAY}-twelve-data-budget.json';p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(encode(dict(request_id=RUN,trade_date=DAY,stage='fixed_intraday_attempt',credits=1,required_outputs=[])))
        git(self.repo,'add',str(p.relative_to(self.repo)));git(self.repo,'commit','-qm','synthetic unexplained budget')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'without attempt'):build_candidate(**self.args)
    def test_new_twelve_capture_without_accounting_rejected(self):
        self.prepare(provider='twelve_data_basic')
        with self.assertRaisesRegex(FinalizerError,'unbound Twelve capture'):build_candidate(**self.args)
    def test_committed_twelve_capture_without_attempt_rejected(self):
        self.prepare(provider='twelve_data_basic')
        git(self.repo,'add',str(Path('data/market-data')/self.capture));git(self.repo,'commit','-qm','synthetic unaccounted capture')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'unbound Twelve capture'):build_candidate(**self.args)
    def test_unrelated_paid_attempt_does_not_cover_extra_committed_capture(self):
        self.paid_attempt()
        extra=str(Path(self.capture).with_name('unaccounted.json'))
        (self.store/extra).write_bytes((self.store/self.capture).read_bytes())
        git(self.repo,'add',str(Path('data/market-data')/extra));git(self.repo,'commit','-qm','synthetic unrelated capture')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'unbound Twelve capture'):build_candidate(**self.args)
    def test_capture_binding_requires_terminal_closure_manifest_membership(self):
        self.paid_attempt()
        ledger=self.store/f'collector-state/2026-11/{DAY}-twelve-data-budget.json'
        value=json.loads(ledger.read_bytes());value['required_outputs']=[r for r in value['required_outputs'] if r['path']!=self.capture]
        ledger.write_bytes(encode(value));git(self.repo,'add',str(ledger.relative_to(self.repo)));git(self.repo,'commit','--amend','--no-edit','-q')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'terminal raw closure'):build_candidate(**self.args)

    def close_state(self,**changes):
        from market_data_collection import _write_close_state,_close_state_path
        _write_close_state(self.store,DAY,'close',[])
        path=_close_state_path(self.store,DAY);value=json.loads(path.read_bytes());value.update(changes);path.write_bytes(encode(value))
        return path.relative_to(self.store).as_posix()
    def test_close_missing_state_exact_bytes_in_manifest(self):
        self.prepare('close');path=self.close_state();raw=(self.store/path).read_bytes()
        out=build_candidate(**self.args)
        self.assertIn(dict(path=path,blob_sha=blob(raw)),json.loads(out.read_bytes())['required_outputs'])
    def test_close_missing_state_must_match_snapshot(self):
        self.prepare('close');self.close_state(missing=['AAA'])
        with self.assertRaisesRegex(FinalizerError,'Close missing state'):build_candidate(**self.args)
    def test_close_missing_state_mode_must_match_invocation(self):
        self.prepare('close');self.close_state(last_mode='close_final')
        with self.assertRaisesRegex(FinalizerError,'Close missing state'):build_candidate(**self.args)
    def test_tracked_old_close_state_can_be_updated(self):
        path=self.close_state(missing=['AAA'],last_mode='close_retry')
        self.commit();self.start=git(self.repo,'rev-parse','HEAD')
        self.prepare('close');self.close_state()
        out=build_candidate(**self.args)
        self.assertIn(dict(path=path,blob_sha=blob((self.store/path).read_bytes())),json.loads(out.read_bytes())['required_outputs'])
