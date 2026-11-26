"""No live Actions or provider; exercise production scheduled owner sequencing."""
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory
from pathlib import Path
from scripts import run_daily_basis_route as H

class TestScheduledSameInstance(TestCase):
    def exercise(self, fail_probe=False, other_day=False, short_window=False, lost_memory_proof=False):
        now=datetime(2026,11,27,12,5,tzinfo=timezone.utc)
        head='d'*40; events=[]
        store=Mock(); store.head.return_value=head; store.lock.return_value=nullcontext()
        store.exists.return_value=False
        api=Mock(); api.has_recent_dispatch_proof.return_value=not lost_memory_proof; api.request.return_value={'id':33,'workflow_id':33,'private':True,'full_name':H.STORE,'owner':{'id':12}}
        updater=Mock(); updater.digest='e'*64; updater.authority.compute_sha="a"*40; updater.store=store
        slot={'slot_id':'friday','execution_day':'2026-11-27','valid_from':'2026-11-27T12:00:00Z','valid_until':'2026-11-27T12:12:00Z'}
        validated_runner=dict(runner_id=45,runner_name='home',labels=['self-hosted','macOS','ARM64','soxx-private-specific'],expires='2026-11-27T12:11:00Z')
        if short_window: validated_runner['expires']='2026-11-27T12:05:30Z'
        prior={'read_sha':head}; updater.preflight_slot.return_value=(slot,head,'claim',prior,{})
        updater.run_slot.side_effect=lambda *a,**k: events.append('capture') or Mock(selector=None)
        authority=dict(valid_until='2026-11-27T13:00:00Z',readiness={'commitment':'1'*64},consumer_sha='b'*40,workflow_source_sha='c'*40)
        raw=H.B.encode(dict(slots=[slot],workflow_id=415302427))
        def probe(*args, **kwargs):
            assert kwargs['api'] is api
            events.append('probe')
            if fail_probe: raise H.HostedDailyError(H.STOP)
            return 76, validated_runner
        def check(a,s,scope,*args,**kw):
            assert a is api and s is store and scope['readiness_run_id']==76
            events.append('verify_permission'); return {}
        def deliver(a,u,candidate,**kw):
            assert a is api and u is updater and kw['runner_expectation']==validated_runner
            events.append('verify_candidate'); return Mock()
        with TemporaryDirectory() as tmp, patch.object(H,'validate_runtime_surface'),patch.object(H,'validate_private_target',return_value={}),patch.object(H,'read_authority',return_value=(authority,raw,b'{}',{})),patch.object(H,'DailyUpdater',return_value=updater),patch.object(H,'wait_for_daily_budget_window'),patch.object(H,'scheduled_readiness_probe',side_effect=probe),patch.object(H,'check_private_readiness',side_effect=check),patch.object(H,'dispatch_verify_once',side_effect=deliver):
            out=Path(tmp).resolve()/'delegation.json'
            result=H.run_route(store=store,api=api,compute=Path(tmp),compute_sha='a'*40,commitment='1'*64,trade_date='2026-11-26' if other_day else '2026-11-27',env={'GITHUB_RUN_ID':'4'},key='unused',access={},delegation_output=out,clock=lambda:now,capture=Mock())
            return events,result,H.B._parse(out.read_bytes())['status']

    def test_valid_same_instance(self):
        self.assertEqual(self.exercise(),(['probe','verify_permission','capture','verify_candidate'],'FINITE_DAILY_BASIS_COMPLETE','OWNED_IMMUTABLE_DAILY'))

    def test_failed_ready_leaves_general_eod_available(self):
        self.assertEqual(self.exercise(fail_probe=True),(['probe'],'FINITE_DAILY_BASIS_HELD','ACQUISITION_HELD_REVIEW_REQUIRED'))

    def test_late_proof_keeps_single_provider_request_unspent(self):
        self.assertEqual(self.exercise(short_window=True),(['probe','verify_permission'],'FINITE_DAILY_BASIS_HELD','ACQUISITION_HELD_REVIEW_REQUIRED'))

    def test_same_instance_proof_not_present_keeps_soxx_held(self):
        self.assertEqual(self.exercise(lost_memory_proof=True),(['probe','verify_permission'],'FINITE_DAILY_BASIS_HELD','ACQUISITION_HELD_REVIEW_REQUIRED'))

    def test_unmatched_session_cannot_probe(self):
        self.assertEqual(self.exercise(other_day=True),([],'FINITE_DAILY_BASIS_HELD','ACQUISITION_HELD_REVIEW_REQUIRED'))

class TestReadinessRemoteFence(TestCase):
    def test_specific_readiness_ref_is_create_only(self):
        from scripts.daily_basis_remote import RemoteGitStore
        s=object.__new__(RemoteGitStore)
        with TemporaryDirectory() as t:
            s.g=Mock(gitdir=Path(t).resolve())
            self.assertEqual(s._ref_scope('refs/heads/basis-daily-readiness-attempt/'+'a'*40+'/'+'b'*64,write=True),'immutable')
            with self.assertRaises(Exception):
                s._ref_scope('refs/heads/basis-daily-readiness-attempt/test/'+'b'*64,write=True)

class TestPrivateWire(TestCase):
    def call(self, mode):
        from scripts import scheduled_basis_readiness as R
        from scripts.emit_basis_consumer_delivery import encode_delivery
        B=H.B; start=datetime(2026,11,27,12,4,tzinfo=timezone.utc); now=datetime(2026,11,27,12,5,tzinfo=timezone.utc)
        source='c'*40; compute='a'*40; head='b'*40; receipt_commit='e'*40
        prior=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id='1'*64,
                   snapshot_blob_sha='2'*40,admission_blob_sha='3'*40,read_sha=head)
        receipt=d=dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=compute,
            selector=prior,admission_blob_sha=prior['admission_blob_sha'],nonce='4'*64,
            valid_from='2026-11-27T12:00:00Z',valid_until='2026-11-27T12:12:00Z')
        raw=B.encode(receipt); commitment=B.receipt_commitment(raw); receipt_blob=B.blob_sha(raw)
        ref=f'refs/heads/basis-read-receipts/{compute}/{commitment}'
        reserved=f'refs/heads/basis-daily-readiness-attempt/{compute}/{commitment}'
        scope=dict(commitment=commitment,runner_id=45,runner_name='home',
            labels=['self-hosted','macOS','ARM64','soxx-private-specific'], expires='2026-11-27T12:11:00Z')
        grant=dict(workflow_source_sha=source,workflow_id=R.WORKFLOW_ID,repository_id=11,owner_id=12)
        repo=dict(id=11,private=True,full_name=R.STORE,owner={'id':12})
        branch='basis-consumer-delivery/'+source
        run=dict(id=76,workflow_id=R.WORKFLOW_ID,head_sha=source,path=R.WORKFLOW,event='workflow_dispatch',
            run_attempt=1,status='completed',conclusion='success',head_branch=branch,repository={'id':11},
            head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},created_at='2026-11-27T12:03:00Z' if mode=='stale-run' else '2026-11-27T12:04:00Z',run_started_at='2026-11-27T12:04:00Z')
        job=dict(id=65,run_id=76,name='verify-basis-consumer',head_sha=source,status='completed',conclusion='success',
            completed_at='2026-11-27T12:04:30Z',runner_id=99 if mode=='wrong-runner' else 45,runner_name='home',labels=scope['labels'],
            steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])
        context=dict(repository=R.STORE,repository_id=11,owner_id=12,private=True,workflow_source_sha=source,run_id=76,
            run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path=R.WORKFLOW,job='verify-basis-consumer')
        attested=dict(schema_version=1,kind='private_basis_consumer_delivery',compute_sha=compute,receipt_commitment=commitment,
            context=context,receipt_ref=ref,receipt_commit=receipt_commit,receipt_blob_sha=receipt_blob,
            selector=prior,verified_at='2026-11-27T12:04:25Z',valid_until=d['valid_until'])
        store=Mock()
        store.head.return_value=head; store.materialize_ref.return_value=receipt_commit
        store.g.normal_blob_identity.return_value=receipt_blob
        store.commit.return_value='f'*40
        store.remote_ref.side_effect=['f'*40] if mode=='consumed' else [None,'f'*40]
        posts=[]
        def clock(): return now if posts else start
        def request(method,path,payload=None):
            if method=='POST':
                store.cas.assert_called_once_with(reserved,'f'*40,'0'*40)
                self.assertTrue(path.endswith(f'/workflows/{R.WORKFLOW_ID}/dispatches'))
                self.assertEqual(payload,{'ref':branch,'inputs':{'compute_sha':compute,'receipt_commitment':commitment}})
                posts.append(payload)
                if mode=='lost-response': raise TimeoutError()
                return dict(workflow_run_id=76,run_url=f'https://api.github.com/repos/{R.STORE}/actions/runs/76',
                            html_url=f'https://github.com/{R.STORE}/actions/runs/76')
            if path.endswith('/76'): return run
            if path.endswith('/76/jobs?per_page=2'): return {'total_count':1,'jobs':[job]}
            raise AssertionError('unexpected request')
        api=Mock(request=request,job_log=Mock(return_value={'job_id':65,'text':encode_delivery(attested)}))
        args=dict(api=api,store=store,scope=scope,grant=grant,slot={'valid_from':d['valid_from'],'valid_until':d['valid_until']},
            prior=prior,head=head,request_id='47-1',repository=repo,compute_sha=compute,clock=clock)
        with patch.object(B,'_blob',return_value=raw):
            if mode=='valid':
                self.assertEqual(R.probe(**args),(76,{k:scope[k] for k in ('runner_id','runner_name','labels','expires')}))
                self.assertEqual(len(posts),1); api.job_log.assert_called_once_with(65)
            else:
                with self.assertRaises((B.BasisError,ValueError,TimeoutError)): R.probe(**args)
                self.assertEqual(len(posts),0 if mode=='consumed' else 1); api.job_log.assert_not_called()
                if mode=='consumed': store.cas.assert_not_called()

    def test_complete_attestation(self): self.call('valid')
    def test_prior_run_cannot_be_reused(self): self.call('stale-run')
    def test_wrong_runner_cannot_authorize(self): self.call('wrong-runner')
    def test_lost_reply_not_retried(self): self.call('lost-response')
    def test_receipt_claim_is_not_replayed(self): self.call('consumed')

class TestReadOnlyProofAfterRunnerCleanup(TestCase):
    def test_approved_runner_attestation_remains_readable_for_consumers(self):
        scope={'runner_id':45,'runner_name':'home','labels':['self-hosted','macOS','ARM64','soxx-private-specific'],
               'expires':'2026-11-27T12:11:00Z'}
        job=dict(id=65,runner_id=45,runner_name='home',labels=scope['labels'],completed_at='2026-11-27T12:10:00Z')
        api=Mock(); api.request.side_effect=[{},dict(total_count=1,jobs=[job])]
        with patch.object(H,'verify_daily_delivery',return_value='attested'):
            self.assertEqual(H.fetch_delivery(api,Mock(),Mock(),76,now='2026-11-27T12:30:00Z',repository={},
                runner_expectation=scope,read_only=True),'attested')

    def test_late_completed_runner_job_never_passes_reader(self):
        scope={'runner_id':45,'runner_name':'home','labels':['self-hosted','macOS','ARM64','soxx-private-specific'],
               'expires':'2026-11-27T12:11:00Z'}
        job=dict(id=65,runner_id=45,runner_name='home',labels=scope['labels'],completed_at='2026-11-27T12:11:00Z')
        api=Mock(); api.request.side_effect=[{},dict(total_count=1,jobs=[job])]
        with self.assertRaises(H.HostedDailyError):
            H.fetch_delivery(api,Mock(),Mock(),76,now='2026-11-27T12:30:00Z',repository={},
                runner_expectation=scope,read_only=True)
        api.job_log.assert_not_called()

def _scoped_git_case(tmp_path, mode):
    """Synthetic GitHub jobs and transport; real Store Git, claims and artifact verification."""
    from datetime import timedelta
    from test_daily_basis_hosted import hosted_case
    from scripts.scheduled_basis_readiness import WORKFLOW_ID
    from scripts.emit_basis_consumer_delivery import encode_delivery
    B=H.B
    _,store,old_api,env,a,_,_,_,now,capture=hosted_case(tmp_path)
    a=dict(a)
    grant=B._parse(store.read(a['grant_ref']['commit'],a['grant_ref']['path']))
    grant['workflow_id']=WORKFLOW_ID
    grant['slots']=grant['slots'][:1]
    for key in ('request_limit','credit_limit','verification_limit'): grant[key]=1
    cal=B._parse(store.read(a['calendar_ref']['commit'],a['calendar_ref']['path']))
    cal['sessions']=[r for r in cal['sessions'] if r['trade_date']<=grant['slots'][0]['cutoff']]
    raw_cal=B.encode(cal); grant['calendar_sha256']=B.sha(raw_cal)
    raw_grant=B.encode(grant)
    root=store.head()
    compute=a['compute_sha']; source=a['workflow_source_sha']
    prior=grant['initial_selector']
    expiry=now+timedelta(minutes=6); receipt_expiry=now+timedelta(minutes=7)
    receipt=dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=compute,
        selector=prior,admission_blob_sha=prior['admission_blob_sha'],nonce='9'*64,
        valid_from=now.isoformat(),valid_until=receipt_expiry.isoformat())
    receipt_raw=B.encode(receipt); receipt_id=B.receipt_commitment(receipt_raw)
    labels=['self-hosted','macOS','ARM64','soxx-private-synthetic']
    with store.lock():
        path='basis-authority/scoped-grant.json'
        cal_path='basis-authority/scoped-calendar.json'
        pin=store.commit(root,{path:raw_grant,cal_path:raw_cal},replace=set())
        a['grant_ref']=dict(commit=pin,path=path,blob_sha=B.blob_sha(raw_grant),sha256=B.sha(raw_grant))
        a['calendar_ref']=dict(commit=pin,path=cal_path,blob_sha=B.blob_sha(raw_cal),sha256=B.sha(raw_cal))
        a['readiness_run_id']=None
        a['readiness']=dict(commitment=receipt_id,runner_id=45,runner_name='home',labels=labels,expires=expiry.isoformat())
        raw=B.encode(a); digest=H.authority_commitment(raw)
        auth_commit=store.commit(pin,{H.AUTH_PATH:raw},replace=set())
        store.cas(f'refs/heads/basis-daily-authority/{compute}/{digest}',auth_commit,'0'*40)
        receipt_pin=store.commit(root,{'basis-authority/receipt.json':receipt_raw},replace=set())
        store.cas(f'refs/heads/basis-read-receipts/{compute}/{receipt_id}',receipt_pin,'0'*40)

    posted=[]
    proof_checks=[]
    class Api:
        def has_recent_dispatch_proof(self,workflow):
            proof_checks.append(workflow)
            return workflow==WORKFLOW_ID and len(posted)==1 and (mode!='late' or len(proof_checks)<4)
        dispatch_capability=has_recent_dispatch_proof
        def request(self,method,path,payload=None):
            if method=='POST':
                assert path.endswith(f'/workflows/{WORKFLOW_ID}/dispatches')
                posted.append(payload['inputs']['receipt_commitment'])
                if mode=='denied':
                    from urllib.error import HTTPError
                    raise HTTPError('https://api.github.com',403,'forbidden',{},None)
                if mode=='uncertain': raise TimeoutError()
                i=555+len(posted)
                return dict(workflow_run_id=i,run_url=f'https://api.github.com/repos/{H.STORE}/actions/runs/{i}',
                    html_url=f'https://github.com/{H.STORE}/actions/runs/{i}')
            if path.endswith(f'/workflows/{WORKFLOW_ID}'): return {'id':WORKFLOW_ID,'state':'active','path':H.VERIFIER}
            if path.endswith(('/actions/runs/556','/actions/runs/557')):
                run=old_api.request('GET','/repos/'+H.STORE+'/actions/runs/555')
                return dict(run,id=int(path.rsplit('/',1)[-1]),workflow_id=WORKFLOW_ID,created_at=now.isoformat())
            if path.endswith(('/556/jobs?per_page=2','/557/jobs?per_page=2')):
                jobs=old_api.request('GET','/repos/'+H.STORE+'/actions/runs/555/jobs?per_page=2')
                i=int(path.split('/')[-2])
                jobs['jobs'][0].update(id=i+10,run_id=i,runner_id=45,runner_name='home',labels=labels)
                return jobs
            return old_api.request(method,path,payload)
        def job_log(self,job_id):
            i=job_id-10; commitment=posted[i-556]
            ref=f'refs/heads/basis-read-receipts/{compute}/{commitment}'
            pin=store.ref(ref); raw=store.read(pin,'basis-authority/receipt.json'); r=B._parse(raw)
            context=dict(repository=H.STORE,repository_id=a['repository_id'],owner_id=a['owner_id'],private=True,
                workflow_source_sha=source,run_id=i,run_attempt=1,event='workflow_dispatch',actor_id=a['owner_id'],
                actor='yuatom',triggering_actor='yuatom',workflow_path=H.VERIFIER,job='verify-basis-consumer')
            return dict(job_id=job_id,text=encode_delivery(dict(schema_version=1,kind='private_basis_consumer_delivery',
                compute_sha=compute,receipt_commitment=commitment,context=context,receipt_ref=ref,receipt_commit=pin,
                receipt_blob_sha=B.blob_sha(raw),selector=r['selector'],verified_at=now.isoformat(),valid_until=r['valid_until'])))
    api=Api(); calls=[]
    out=tmp_path.resolve()/'owner.json'
    result=H.run_route(store=store,api=api,compute=tmp_path,compute_sha=compute,commitment=digest,
        trade_date=grant['slots'][0]['execution_day'],env=env,key='unused',access={},delegation_output=out,
        clock=lambda:now,capture=capture(calls=calls))
    if mode!='valid':
        import os
        import market_data_collectors as C
        from scripts import daily_basis_delegation as DD
        assert result=='FINITE_DAILY_BASIS_HELD' and not calls and posted==[receipt_id]
        assert store.ref(f'refs/heads/basis-daily-readiness-attempt/{compute}/{receipt_id}')
        observed=store.head()
        if mode=='late':
            s=grant['slots'][0]['slot_id']; gh=B.sha(raw_grant)
            assert observed!=root and len(proof_checks)==4
            path=f'capture-evidence/daily-basis/{gh}/{s}'
            claim=B._parse(store.read(observed,path+'/claim.json'))
            assert claim['status']=='CLAIMED_NO_REPLAY'
            attempt=store.ref(f'refs/heads/basis-daily-attempts/{gh}/{s}')
            terminal=B._parse(store.read(attempt,path+'/terminal.json'))
            assert terminal['status']=='CAPTURE_STOPPED_INSPECT_NO_RETRY'
            assert not store.exists(attempt,path+'/response.raw')
            assert not store.exists(attempt,path+'/envelope.json')
        else:assert observed==root
        owner=B._parse(out.read_bytes()); assert owner['store_sha']==observed
        config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                            'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':256}}}
        env.update(DAILY_BASIS_DELEGATION_FILE=str(out),DAILY_BASIS_GRANT_COMMITMENT=digest,
                   MARKET_DATA_COMPUTE_SHA=compute,TWELVE_DATA_API_KEY='synthetic-only')
        with patch.dict(os.environ,env),patch.object(DD,'datetime') as clock,patch.object(C,'_daily_universe',return_value=[('SOXX','etf'),('OTHER','etf')]),patch.object(C,'wait_for_twelve_budget'),patch.object(C,'consume_twelve_credit'),patch.object(C,'fetch_twelve_daily',return_value=[]) as fetch:
            clock.now.return_value=now
            collected=C.collect_previous_session_eod(trade_date=grant['slots'][0]['execution_day'],
                store_root=store.repo/'data/market-data',watchlist={},completeness={},config=config,access={})
            assert fetch.call_count==1 and fetch.call_args.args[0]=='OTHER'
            assert collected['delegated_daily_symbols']==[{'symbol':'SOXX','status':'ACQUISITION_HELD_REVIEW_REQUIRED'}]
        return
    assert result=='FINITE_DAILY_BASIS_COMPLETE'
    assert len(calls)==1 and len(posted)==2 and posted[0]==receipt_id
    assert store.ref(f'refs/heads/basis-daily-readiness-attempt/{compute}/{receipt_id}')
    assert store.head()!=root
    # Read-only at a later time after the runner cleaned up. No new POST or credential promotion.
    v=H.current_delivery(store=store,api=api,compute_sha=compute,commitment=digest,clock=lambda:expiry+timedelta(minutes=1))
    assert len(calls)==1 and len(posted)==2 and v['verified_run_id']==557 and v['volume_policy']=='BLOCKED_UNRESOLVED'


def test_scoped_owner_real_git_and_reader(tmp_path):
    _scoped_git_case(tmp_path,'valid')


def test_denied_private_workflow_does_not_stop_ordinary_eod(tmp_path):
    _scoped_git_case(tmp_path,'denied')


def test_uncertain_private_workflow_does_not_fall_through_to_legacy_soxx(tmp_path):
    _scoped_git_case(tmp_path,'uncertain')


def test_confirmed_late_guard_uses_post_debit_store_head_and_keeps_ordinary_eod(tmp_path):
    _scoped_git_case(tmp_path,'late')

class TestLastProviderBoundary(TestCase):
    def test_scope_rechecked_after_debit_before_provider_transport(self):
        from scripts import daily_basis_updates as U
        u=object.__new__(U.DailyUpdater)
        u.store=Mock(); u.store.head.return_value='c'*40; u.store.g.normal_blob_identity.return_value='b'*40
        u.clock=lambda:datetime(2026,11,27,12,5,tzinfo=timezone.utc)
        u.grant_raw=U.B.encode({'request_template':{}})
        slot={'cutoff':'2026-11-25','valid_from':'2026-11-27T12:00:00Z','cleanup_until':'2026-11-27T12:18:00Z'}
        u.preflight_slot=Mock(return_value=(slot,'a'*40,'claim',{},{}))
        u._debit=Mock(return_value=('c'*40,'2026-11-27T12:05Z','budget','b'*40))
        u._fresh=Mock();u._validate_grant_unchanged=Mock();u._attempt_ref=Mock()
        sent=[]
        def capture(**kwargs):
            kwargs['before_request']()
            sent.append(True)
        def late():
            if u._debit.called: raise ValueError('synthetic scope expiration')
        with self.assertRaises(U.DailyBasisError):
            u.run_slot('friday','45-1',capture=capture,request_guard=late)
        u._debit.assert_called_once();self.assertEqual(sent,[])

class TestScopedTerminalAtRealCapture(TestCase):
    def exercise(self, uncertain=False, already_late=False):
        from scripts import daily_basis_updates as U
        from test_capture_only_response import access,request
        u=object.__new__(U.DailyUpdater)
        u.store=Mock(); u.store.head.return_value='c'*40; u.store.g.normal_blob_identity.return_value='b'*40
        u.clock=lambda:datetime(2026,11,27,12,5,tzinfo=timezone.utc)
        u.grant_raw=U.B.encode({'request_template':request()})
        u.authority=Mock(grant_sha256='1'*64)
        slot={'cutoff':request()['end_date'],'valid_from':'2026-11-27T12:00:00Z','cleanup_until':'2026-11-27T12:18:00Z'}
        u.preflight_slot=Mock(return_value=(slot,'a'*40,'claim',{},{}))
        u._debit=Mock(return_value=('c'*40,'2026-11-27T12:05Z','budget','b'*40))
        u._fresh=Mock(); u._validate_grant_unchanged=Mock(); u._attempt_ref=Mock(return_value='refs/heads/durable-attempt')
        u.store.commit.return_value='d'*40; u.store.assert_locked=Mock()
        if uncertain:u.store.cas.side_effect=TimeoutError()
        checks=[]
        def late():
            checks.append(u._debit.called)
            if already_late or u._debit.called:raise U.ScopedRequestExpired()
        with patch('market_data_collectors.urllib.request.build_opener') as opener:
            cls=U.DailyBasisError if uncertain else U.ScopedProviderHold
            with self.assertRaises(cls) as error:
                u.run_slot('friday','45-1',capture=U.provider_capture('synthetic-only',access()),request_guard=late)
            if uncertain:self.assertNotIsInstance(error.exception,U.ScopedProviderHold)
            opener.assert_not_called()
        if already_late:
            self.assertEqual(checks,[False]);u._debit.assert_not_called();u.store.cas.assert_not_called()
        else:
            self.assertEqual(checks,[False,True]);u._debit.assert_called_once()
            u.store.cas.assert_called_once_with('refs/heads/durable-attempt','d'*40,'0'*40)
            terminal=H.B._parse(u.store.commit.call_args.args[1]['claim/terminal.json'])
            self.assertEqual(terminal['status'],'CAPTURE_STOPPED_INSPECT_NO_RETRY')

    def test_late_after_confirmed_debit_keeps_claim_but_emits_typed_hold(self):self.exercise()
    def test_uncertain_terminal_never_emits_typed_hold(self):self.exercise(uncertain=True)
    def test_late_before_debit_does_not_spend_it(self):self.exercise(already_late=True)
