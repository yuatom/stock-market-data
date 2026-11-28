from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from daily_basis_hosted_fixture import seed,git
from scripts import provider_basis_snapshot as B
from scripts import run_daily_basis_route as H
from scripts.daily_basis_updates import DailyUpdater,GrantAuthority
from scripts.daily_basis_delivery import DeliveryExpectation,WORKFLOW
from scripts.daily_basis_store import LocalGitStore
from scripts.emit_basis_consumer_delivery import build_delivery,encode_delivery
from scripts.raw_response_evidence import build_envelope
from scripts.hosted_v2_durable_candidate import record_preproof


class FixtureStore(LocalGitStore):
    remote_ref=LocalGitStore.ref


class FakeApi:
    def __init__(self,store,grant):self.store=store;self.grant=grant;self.runs={};self.jobs={};self.logs={};self.posts=[];self.live=False;self.clock=None
    def request(self,method,path,payload=None):
        g=self.grant
        if method=='POST':
            assert self.live,'reconciliation must never dispatch'
            assert path=='/repos/'+H.STORE+'/actions/workflows/415302427/dispatches'
            assert payload['ref']=='basis-consumer-delivery/'+g['workflow_source_sha']
            assert payload['inputs']['compute_sha']==g['compute_sha']
            assert len(self.posts)<2
            rid=51+10*len(self.posts);self.posts.append(rid)
            self.emit(rid,rid+1,payload['inputs']['receipt_commitment'],self.clock())
            return dict(workflow_run_id=rid,run_url=f'https://api.github.com/repos/{H.STORE}/actions/runs/{rid}',
                html_url=f'https://github.com/{H.STORE}/actions/runs/{rid}')
        assert method=='GET'
        if path=='/repos/'+H.COMPUTE+'/actions/runs/51':
            return dict(id=51,workflow_id=33,path=H.CALLER,head_sha=g['compute_sha'],event='schedule',run_attempt=1,
                head_branch='main',status='in_progress',repository={'full_name':H.COMPUTE},head_repository={'full_name':H.COMPUTE},
                actor={'id':12},triggering_actor={'id':12})
        if path=='/user':return {'id':12}
        if path=='/repos/'+H.STORE:return dict(id=11,private=True,full_name=H.STORE,owner={'id':12})
        if path=='/repos/'+H.STORE+'/actions/workflows/415302427':return dict(id=415302427,path=WORKFLOW,state='active')
        if path==H.REPORT_RELEASE_PATH:
            from base64 import b64encode
            raw=B.encode(dict(repository_commit_sha=g['consumer_sha']))
            return dict(type='file',path='state/runtime-release.json',encoding='base64',sha=B.blob_sha(raw),content=b64encode(raw).decode())
        rid=int(path.split('/runs/')[1].split('/')[0])
        return self.jobs[rid] if 'jobs?' in path else self.runs[rid]
    def dispatch_capability(self,workflow_id):return workflow_id==415302427
    def has_recent_dispatch_proof(self,workflow_id):return workflow_id==415302427
    def job_log(self,j):return self.logs[j]
    def emit(self,run_id,job_id,commitment,at):
        g=self.grant;t=at.isoformat();source=g['workflow_source_sha']
        context=dict(repository=H.STORE,repository_id=11,owner_id=12,private=True,workflow_source_sha=source,run_id=run_id,
            run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer')
        run=dict(id=run_id,workflow_id=415302427,head_sha=source,path=WORKFLOW,event='workflow_dispatch',run_attempt=1,
            status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+source,repository={'id':11},head_repository={'id':11},
            actor={'id':12},triggering_actor={'id':12},created_at=t,run_started_at=t)
        jobs=dict(total_count=1,jobs=[dict(id=job_id,run_id=run_id,labels=['ubuntu-latest'],name='verify-basis-consumer',
            head_sha=source,status='completed',conclusion='success',completed_at=t,
            steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
        suffix=f"{g['compute_sha']}/{commitment}"; git(self.store.repo,"update-ref","refs/remotes/origin/basis-read-receipts/"+suffix,self.store.ref("refs/heads/basis-read-receipts/"+suffix))
        value=build_delivery(self.store.repo,commitment,g['compute_sha'],context,now=t)
        log=dict(job_id=job_id,text=encode_delivery(value))
        self.runs[run_id]=run;self.jobs[run_id]=jobs;self.logs[job_id]=log
        exp=DeliveryExpectation(11,12,415302427,source,g['compute_sha'],commitment,run_id)
        return (exp,run,jobs,log,(value['receipt_commit'],value['receipt_blob_sha'],value['selector']))


class HostedReconciliation(TestCase):
    def test_real_candidate_debit_publication_and_restart_read_only(self):
        with TemporaryDirectory() as scratch:
            repo,g,cal,_,values,response=seed(Path(scratch).resolve()/'test')
            workflow=repo/WORKFLOW;workflow.parent.mkdir(parents=True)
            workflow.write_text('name: synthetic\npermissions:\n  contents: read\njobs:\n  verify-basis-consumer:\n    runs-on: ubuntu-latest\n')
            git(repo,'add','.');git(repo,'commit','-qm','synthetic approved hosted workflow')
            source=git(repo,'rev-parse','HEAD');blob=git(repo,'rev-parse','HEAD:'+WORKFLOW)
            git(repo,'update-ref','refs/heads/basis-consumer-delivery/'+source,source)
            g.update(workflow_source_sha=source,workflow_id=415302427,slots=g['slots'][:1],request_limit=1,credit_limit=1,verification_limit=1)
            store=FixtureStore(repo);api=FakeApi(store,g);slot=g['slots'][0];take=[]
            to=lambda minute,second=0:datetime.fromisoformat(f'2026-09-10T12:{minute:02}:{second:02}+00:00')
            now=to(0,5); key='fixture-provider-key'; api.clock=lambda:now
            cal['sessions']=[item for item in cal['sessions'] if item['trade_date']<=slot['cutoff']]
            g['calendar_sha256']=B.sha(B.encode(cal))
            gr,cr=B.encode(g),B.encode(cal)
            def capture(*,request,before_request,retain_raw,retain_envelope,clock):
                before_request();take.append(request)
                raw=response(request,[v for v in values if v['datetime']<=request['end_date']]);retain_raw(raw)
                enc=build_envelope(raw,request,clock().isoformat());retain_envelope(enc)
                return dict(status='CAPTURE_RETAINED_UNQUALIFIED',envelope_sha256=B.sha(enc),raw_response_sha256=B.sha(raw))
            U=lambda s:DailyUpdater(s,gr,cr,GrantAuthority(B.sha(gr),g['compute_sha'],g['consumer_sha'],source,B.sha(cr)),clock=lambda:now)
            with store.lock():
                scope=store.publish(store.head(),{'basis-authority/grant.json':gr,'basis-authority/calendar.json':cr},replace=set())
                raw=B.encode(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=g['compute_sha'],selector=g['initial_selector'],
                    admission_blob_sha=g['initial_selector']['admission_blob_sha'],nonce='f'*64,valid_from=to(0).isoformat(),valid_until=to(10).isoformat()))
                commitment=B.receipt_commitment(raw);base=f'{g["compute_sha"]}/{commitment}'
                rc=store.commit(scope,{'basis-authority/receipt.json':raw},replace=set())
                store.cas('refs/heads/basis-read-receipts/'+base,rc,'0'*40)
                profile=dict(kind='private_github_hosted_v1',workflow_blob=blob,expires=to(9).isoformat())
                ref=lambda path,data:dict(commit=scope,path=path,blob_sha=B.blob_sha(data),sha256=B.sha(data))
                a=dict(schema_version=1,kind='finite_daily_basis_authority',compute_sha=g['compute_sha'],consumer_sha=g['consumer_sha'],
                  workflow_source_sha=source,grant_ref=ref('basis-authority/grant.json',gr),calendar_ref=ref('basis-authority/calendar.json',cr),
                  nonce='e'*64,valid_from=slot['valid_from'],valid_until=slot['valid_until'],
                  actions_free_until=slot['valid_until'],readiness_run_id=None,repository_id=11,owner_id=12,runtime_workflow_id=33,
                  trigger_kind='schedule',schedule='5 7 * * 1-5',old_writers_drained=True,readiness=profile|{'commitment':commitment})
                a[H.EARLY_POLICY_KEY]=dict(policy_id=H.BASIC_US_EOD_POLICY_ID,availability_semantics='eligibility_only_one_sample',
                    provider='twelve_data_basic',symbol='SOXX',article=H.BASIC_US_EOD_ARTICLE,account_plan='Basic',included_credit_only=True,
                    account_checked_at=to(0).isoformat(),account_valid_until=to(25).isoformat(),key_fingerprint=B.sha(H.BASIC_OWNER_KEY_DOMAIN+key.encode()),
                    cutoff=slot['cutoff'],execution_day=slot['execution_day'])
                ar=B.encode(a);authority=H.authority_commitment(ar)
                ac=store.commit(scope,{H.AUTH_PATH:ar},replace=set())
                store.cas(f'refs/heads/basis-daily-authority/{g["compute_sha"]}/{authority}',ac,'0'*40)
                legacy=dict(a,readiness_run_id=51);del legacy['readiness'];del legacy[H.EARLY_POLICY_KEY]
                lr=B.encode(legacy);lc=H.authority_commitment(lr)
                la=store.commit(scope,{H.AUTH_PATH:lr},replace=set())
                store.cas(f'refs/heads/basis-daily-authority/{g["compute_sha"]}/{lc}',la,'0'*40)
            now=to(2)
            env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=H.COMPUTE,GITHUB_SHA=g['compute_sha'],
                GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='schedule',GITHUB_RUN_ATTEMPT='1',
                GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='collect',
                DAILY_BASIS_TRIGGER_KIND='schedule',DAILY_BASIS_SCHEDULE='5 7 * * 1-5',
                GITHUB_WORKFLOW_REF=H.COMPUTE+'/'+H.CALLER+'@refs/heads/main',GITHUB_RUN_ID='51')
            access={'twelve_data_basic':dict(provider_identity='twelve_data_basic',base_url='https://api.twelvedata.com',api_key_env='TWELVE_DATA_API_KEY')}
            kwargs=dict(store=store,api=api,compute=repo,compute_sha=g['compute_sha'],commitment=authority,
                trade_date=slot['execution_day'],env=env,key=key,access=access,delegation_output=Path(scratch).resolve()/'delegation.json',
                clock=lambda:now,capture=capture,source_check=lambda:None)
            api.live=True
            self.assertEqual(H.run_route(**kwargs),'FINITE_DAILY_BASIS_COMPLETE')
            self.assertEqual(H.run_route(**(kwargs|{'delegation_output':Path(scratch).resolve()/'delegation-restart.json'})),'FINITE_DAILY_BASIS_HELD')
            self.assertEqual((api.posts,len(take)),([51,61],1))
            api.live=False
            ref=f'refs/heads/basis-daily-hosted-deliveries/{B.sha(gr)}/{slot["slot_id"]}'
            side_ref=ref;so=store.ref(ref)
            restart=FixtureStore(repo)
            now=to(11) # pre proof expired as live authority, final receipt still current
            args=dict(store=restart,api=api,compute_sha=g['compute_sha'],commitment=authority,clock=lambda:now)
            main=restart.head()
            # Absence must never fall through to the generic V1 consumer.
            git(repo,'update-ref','-d',side_ref,so)
            with self.assertRaises(B.BasisError):H.current_delivery(**args)
            # A separately valid legacy authority for the exact SAME grant and
            # slot must not downgrade a hosted debit when its sidecar is absent.
            with self.assertRaises(H.HostedDailyError):
                H.current_delivery(**(args|{'commitment':lc}))
            git(repo,'update-ref',side_ref,so)
            delivered=H.current_delivery(**args)
            self.assertEqual((delivered['verified_run_id'],restart.head(),len(take)),(61,main,1))
            for mutate,reset in (
                    (lambda:api.jobs[61]['jobs'][0].update(labels=['self-hosted','macOS','ARM64']),
                     lambda:api.jobs[61]['jobs'][0].update(labels=['ubuntu-latest'])),
                    (lambda:api.jobs[51]['jobs'][0].update(completed_at=to(2,1).isoformat()),
                     lambda:api.jobs[51]['jobs'][0].update(completed_at=to(2).isoformat()))):
                mutate()
                with self.assertRaises((B.BasisError,ValueError)):H.current_delivery(**args)
                reset()
                self.assertEqual((restart.head(),len(take)),(main,1))

    def test_supplied_hosted_profile_cannot_approve_a_different_workflow(self):
        from scripts.scheduled_basis_readiness import probe
        with TemporaryDirectory() as scratch:
            from test_hosted_verifier_candidate import fixture
            x=fixture(scratch);repo=Path(scratch).resolve()
            git(repo,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
            git(repo,'branch','-M','main')
            wf=repo/WORKFLOW
            wf.write_text('name: synthetic\npermissions:\n  contents: read\njobs:\n  verify-basis-consumer:\n    runs-on: ubuntu-latest\n')
            git(repo,'add','.');git(repo,'commit','-qm','independent synthetic default')
            store=FixtureStore(repo);head=store.head()
            cutoff='2026-01-07T14:00:00Z';until='2026-01-07T14:06:00Z'
            api=FakeApi(store,{})
            with store.lock(),self.assertRaises(B.BasisError):
                probe(api=api,store=store,scope=dict(kind='private_github_hosted_v1',commitment='b'*64,
                    workflow_blob='c'*40,expires=until),grant=dict(workflow_source_sha=head,workflow_id=415302427),
                    slot=dict(valid_from=cutoff,valid_until=until),prior=None,head=head,request_id='51-1',
                    repository=None,compute_sha='a'*40,clock=lambda:B._stamp(cutoff))
            self.assertEqual(api.posts,[])
            self.assertEqual(store.head(),head)
            self.assertIsNone(store.ref('refs/heads/basis-daily-readiness-attempt/'+'a'*40+'/'+'b'*64))

    def test_new_remote_names_have_no_broader_namespace(self):
        from types import SimpleNamespace
        from scripts.daily_basis_remote import RemoteGitStore, RemoteStoreError
        with TemporaryDirectory() as scratch:
            guard=SimpleNamespace(g=SimpleNamespace(gitdir=Path(scratch).resolve()))
            for ref in ('refs/heads/basis-daily-readiness-proof/'+('a'*40)+'/'+('b'*64),
                        'refs/heads/basis-daily-hosted-deliveries/'+('b'*64)+'/slot-1'):
                self.assertEqual(RemoteGitStore._ref_scope(guard,ref),'immutable')
                for altered,write in ((ref+'/else',True),(ref+'/else',False),(ref.replace('refs/heads/','refs/tags/'),False)):
                    with self.assertRaises(RemoteStoreError):
                        RemoteGitStore._ref_scope(guard,altered,write=write)

    def test_late_final_dispatch_ack_never_writes_mapping_or_sidecar(self):
        from unittest.mock import Mock
        candidate=Mock(slot_id='synthetic')
        start=B._stamp('2026-09-10T12:02:00Z'); limit=B._stamp('2026-09-10T12:05:00Z')
        now=start
        updater=Mock(grant=dict(compute_sha='a'*40, workflow_source_sha='b'*40,workflow_id=415302427))
        updater._fresh.return_value=start
        api=Mock()
        def delayed(*args):
            nonlocal now
            now=limit
            return dict(workflow_run_id=61,run_url=f'https://api.github.com/repos/{H.STORE}/actions/runs/61',
                html_url=f'https://github.com/{H.STORE}/actions/runs/61')
        api.request.side_effect=delayed
        candidate.receipt_commitment='c'*64
        with self.assertRaises(H.HostedDailyError):
            H.dispatch_verify_once(api,updater,candidate,repository=None,clock=lambda:now,
                runner_expectation=dict(kind='private_github_hosted_v1',expires=limit.isoformat()))
        updater.reserve_verification.assert_called_once_with(candidate)
        api.request.assert_called_once()
        updater.store.commit.assert_not_called()
        updater.store.cas.assert_not_called()

    def test_expiry_after_last_remote_read_stops_actual_main_cas(self):
        from test_daily_basis_remote import setup, MAIN
        from unittest.mock import patch
        with TemporaryDirectory() as scratch:
            R, store, repo, remote, transport, state, create=setup(Path(scratch).resolve())
            deadline=B._stamp('2026-09-10T12:05:00Z')
            before=B._stamp('2026-09-10T12:04:59Z')
            old=store.head();current=before;last_read=store._remote_ref;checks=0
            def delayed(ref):
                nonlocal current,checks
                found=last_read(ref)
                if ref==MAIN:
                    checks+=1
                    if checks==2:current=deadline
                return found
            def guard():
                if current>=deadline:
                    raise H.HostedDailyError('expired before publication')
            with store.lock():
                new=store.commit(old,{'new.json':b'{}\n'},replace=set())
                self.assertLess(current,deadline)
                with patch.object(store,'_remote_ref',side_effect=delayed),self.assertRaises(R.RemoteStoreError):
                    store.cas(MAIN,new,old,write_guard=guard)
            self.assertEqual(current,deadline)
            self.assertEqual(transport.pushes,[])
            self.assertEqual((git(repo,'rev-parse',MAIN),git(remote,'rev-parse',MAIN)),(old,old))
            self.assertEqual(git(remote,'for-each-ref','--format=%(refname)','refs/tags/market-data-read/'+new),'')

    def test_hosted_runway_expiry_before_debit_changes_neither_budget_nor_provider(self):
        from test_daily_basis_remote import setup, MAIN
        from unittest.mock import Mock,patch
        from scripts.daily_basis_updates import ScopedRequestExpired,ScopedProviderHold
        with TemporaryDirectory() as scratch:
            _,store,repo,remote,transport,_,_=setup(Path(scratch).resolve())
            old=store.head()
            current=B._stamp('2026-09-10T12:02:00Z')
            deadline=B._stamp('2026-09-10T12:04:01Z')
            u=object.__new__(DailyUpdater)
            slot=dict(slot_id='sep10',execution_day='2026-09-10')
            u.grant_raw=B.encode(dict(slots=[slot],request_limit=1))
            u.authority=GrantAuthority(B.sha(u.grant_raw),"a"*40,"b"*40,"c"*40,"d"*64)
            u.store=store;u.clock=lambda:current;u._fresh=lambda _:current
            u.preflight_slot=lambda *_:(slot,old,u._prefix(slot),{},None)
            capture=Mock();last_read=store._remote_ref;reads=0
            def delay(ref):
                nonlocal current,reads
                found=last_read(ref)
                if ref==MAIN:
                    reads+=1
                    if reads==3: current=B._stamp('2026-09-10T12:02:01Z')
                return found
            def guard():
                from datetime import timedelta
                if current+timedelta(seconds=120)>=deadline:
                    raise ScopedRequestExpired()
            with store.lock(),patch.object(store,'_remote_ref',side_effect=delay),self.assertRaises(ScopedProviderHold):
                u.run_slot(slot['slot_id'],'51-1',capture=capture,request_guard=guard,
                    hosted_binding=dict(kind='private_github_hosted_v1',scope_commitment='a'*64,pin='b'*40))
            self.assertEqual(reads,3)
            capture.assert_not_called()
            self.assertEqual(transport.pushes,[])
            self.assertEqual((git(repo,'rev-parse',MAIN),git(remote,'rev-parse',MAIN)),(old,old))
            self.assertFalse(store.exists(old,'data/market-data/collector-state/2026-09/2026-09-10-twelve-data-budget.json'))
