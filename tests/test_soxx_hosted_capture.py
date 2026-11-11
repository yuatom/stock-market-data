"""Official route exercised with temporary Git, synthetic approval and fake HTTP."""
import hashlib,importlib,json,time,unittest
from datetime import datetime,timedelta
from unittest.mock import patch
import test_guarded_history_recovery as f
from test_raw_response_evidence import request,body
from test_capture_only_response import access

class HostedCaptureTests(f.RecoveryIntegrationFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue((f.ROOT/'scripts/run_soxx_capture.py').exists(),'hosted capture absent')
        self.m=importlib.import_module('run_soxx_capture')
        self.approval=dict(schema_version=1,mode='soxx_capture_only',compute_sha=self.compute_sha,
            baseline_store_sha=self.base,execution_day=f.DAY,request=dict(request(),symbol='SOXX',mic_code='XNMS',start_date='2026-10-13',end_date='2026-10-16'),
            valid_from=(f.NOW-timedelta(seconds=30)).isoformat(),valid_until=(f.NOW+timedelta(seconds=30)).isoformat(),
            request_limit=1,credit_limit=1,evidence_repository='yuatom/stock-market-data-store',evidence_repository_id=1234567,
            evidence_prefix='capture-evidence',market_fact_write=False)
        self.raw_approval=self.m.encode(self.approval)
        self.approval_sha=hashlib.sha256(self.raw_approval).hexdigest()
        path=self.repo/'capture-approvals'/f'{self.approval_sha}.json';path.parent.mkdir();path.write_bytes(self.raw_approval)
        f.git(self.repo,'add','capture-approvals');f.git(self.repo,'commit','-m','synthetic approval');f.git(self.repo,'push','origin','main')
        self.approval_commit=f.git(self.repo,'rev-parse','HEAD')
        payload=body();payload['meta'].update(symbol='SOXX',mic_code='XNMS');payload['values'][0]['datetime']='2026-10-15'
        self.raw=json.dumps(payload).encode()

    def call(self,**changes):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return f.NOW if tz is None else f.NOW.astimezone(tz)
        args=dict(compute=self.compute,compute_sha=self.compute_sha,store=self.repo,
            approval_commit=self.approval_commit,approval_sha256=self.approval_sha,
            request_id='995-1',h=self.h,config=self.config,access=access(),key='synthetic-provider-key',
            store_token='synthetic-store-token',repository_identity=dict(id=1234567,full_name='yuatom/stock-market-data-store',private=True,default_branch='main'),clock=lambda:f.NOW,deadline=time.monotonic()+60)
        args.update(changes)
        with patch.object(self.h.collectors,'datetime',Clock):return self.m.execute(**args)

    def test_one_capture_immutable_approval_and_only_budget_evidence_changes(self):
        calls=[]
        def http(*args,**kwargs):
            calls.append(1)
            self.assertEqual(self.current_budget()['credits'],1)
            return self.raw
        with patch.object(self.h.collectors,'_http_raw',side_effect=http):result=self.call()
        self.assertEqual(len(calls),1)
        self.assertEqual(result['status'],'CAPTURE_RETAINED_UNQUALIFIED')
        self.assertFalse(result['production_activation'])
        prefix=self.repo/'capture-evidence'/self.approval_sha/'SOXX'
        self.assertEqual((prefix/'response.raw').read_bytes(),self.raw)
        self.assertTrue((prefix/'terminal.json').exists())
        changes=f.git(self.repo,'diff','--name-only',self.approval_commit,'HEAD').splitlines()
        self.assertTrue(all(p.startswith('capture-evidence/') or p.endswith('-twelve-data-budget.json') for p in changes))
        with patch.object(self.h.collectors,'_http_raw',side_effect=AssertionError('no retry')):
            with self.assertRaises(self.m.HostedCaptureError):self.call()
        self.assertEqual(self.current_budget()['credits'],1)

    def test_approval_digest_or_commit_drift_stops_before_debit(self):
        with self.assertRaises(self.m.HostedCaptureError):self.call(approval_sha256='0'*64)
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.approval_commit)

    def test_provider_failure_no_second_request_no_refund(self):
        with patch.object(self.h.collectors,'_http_raw',side_effect=RuntimeError('private')) as http:
            with self.assertRaises(self.m.HostedCaptureError):self.call()
            self.assertEqual(http.call_count,1)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assertFalse((self.repo/'capture-evidence'/self.approval_sha/'SOXX/response.raw').exists())

    def test_malformed_body_is_durably_retained_before_envelope_rejection(self):
        with patch.object(self.h.collectors,'_http_raw',return_value=b'{bad'):
            with self.assertRaises(self.m.HostedCaptureError):self.call()
        self.assertEqual((self.repo/'capture-evidence'/self.approval_sha/'SOXX/response.raw').read_bytes(),b'{bad')
        self.assertFalse((self.repo/'capture-evidence'/self.approval_sha/'SOXX/envelope.json').exists())

    def test_visibility_and_surface_are_closed(self):
        good=dict(id=1234567,full_name='yuatom/stock-market-data-store',private=True,default_branch='main')
        self.m.validate_repository(good)
        for changed in ({'private':False},{'id':True},{'full_name':'other/store'}):
            with self.assertRaises(Exception):self.m.validate_repository(dict(good,**changed))
        with self.assertRaises(Exception):self.m.validate_surface({},self.compute_sha,'995-1')

    def test_expired_approval_stops_before_claim(self):
        with self.assertRaises(self.m.HostedCaptureError):self.call(clock=lambda:f.NOW+timedelta(seconds=31))
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.approval_commit)
        self.assertFalse((self.repo/'capture-evidence').exists())

    def test_raw_retention_unknown_stops_without_envelope_or_terminal(self):
        real=self.m.retain_capture_object
        writes=[]
        def retain(**kwargs):
            writes.append(kwargs['name'])
            if kwargs['name']=='response.raw':raise RuntimeError('unknown private publication')
            return real(**kwargs)
        with patch.object(self.m,'retain_capture_object',side_effect=retain),patch.object(self.h.collectors,'_http_raw',return_value=self.raw) as http:
            with self.assertRaises(self.m.HostedCaptureError):self.call()
            self.assertEqual(http.call_count,1)
        self.assertEqual(writes,['claim.json','request.json','attempt.json','response.raw'])
        self.assertEqual(self.current_budget()['credits'],1)

    def test_terminal_failure_cannot_report_success(self):
        real=self.m.retain_capture_object
        def retain(**kwargs):
            if kwargs['name']=='terminal.json':raise RuntimeError('unknown')
            return real(**kwargs)
        with patch.object(self.m,'retain_capture_object',side_effect=retain),patch.object(self.h.collectors,'_http_raw',return_value=self.raw):
            with self.assertRaises(self.m.HostedCaptureError):self.call()
        self.assertTrue((self.repo/'capture-evidence'/self.approval_sha/'SOXX/envelope.json').exists())
        self.assertEqual(self.current_budget()['credits'],1)

    def test_changed_store_after_debit_prevents_http(self):
        real=self.a.debit_before_request
        def debit(*args,**kwargs):
            result=real(*args,**kwargs)
            (self.repo/'drift.txt').write_text('synthetic')
            return result
        with patch.object(self.a,'debit_before_request',side_effect=debit),patch.object(self.h.collectors,'_http_raw',side_effect=AssertionError('no http')) as http:
            with self.assertRaises(self.m.HostedCaptureError):self.call()
            self.assertEqual(http.call_count,0)
        self.assertEqual(self.current_budget()['credits'],1)

    def test_no_credentials_forwarded_on_github_redirect(self):
        with self.assertRaises(ValueError):self.m._NoRedirect().redirect_request(None,None,302,'moved',{},'https://other.invalid')

    def test_rerun_attempt_rejected(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY='yuatom/stock-market-data',
            GITHUB_REF='refs/heads/main',GITHUB_SHA=self.compute_sha,GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='995',
            GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/soxx-capture-only.yml@refs/heads/main',
            GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom')
        self.m.validate_surface(env,self.compute_sha,'995-1')
        with self.assertRaises(Exception):self.m.validate_surface(dict(env,GITHUB_RUN_ATTEMPT='2'),self.compute_sha,'995-2')

    def test_extra_approval_commit_or_dirty_scope_stops_before_claim(self):
        (self.repo/'unrelated.txt').write_text('unexpected')
        f.git(self.repo,'add','unrelated.txt');f.git(self.repo,'commit','-m','unapproved extra');f.git(self.repo,'push','origin','main')
        head=f.git(self.repo,'rev-parse','HEAD')
        with self.assertRaises(self.m.HostedCaptureError):self.call(approval_commit=head)
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),head)
        self.assertFalse((self.repo/'capture-evidence').exists())

    def test_cli_wrong_surface_stops_before_identity_request(self):
        import os,contextlib,io
        args=['--compute-sha',self.compute_sha,'--store-repo',str(self.repo),'--approval-commit',self.approval_commit,
              '--approval-sha256',self.approval_sha,'--request-id','995-1']
        with patch.dict(os.environ,{},clear=True),patch.object(self.m,'private_identity') as identity,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.m.main(args),2)
            identity.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['status'],'BLOCKED')

    def test_private_visibility_failure_stops_cli_before_coordinator(self):
        import os,contextlib,io
        env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY='yuatom/stock-market-data',
            GITHUB_REF='refs/heads/main',GITHUB_SHA=self.compute_sha,GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='995',
            GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/soxx-capture-only.yml@refs/heads/main',
            GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',MARKET_DATA_STORE_TOKEN='synthetic-store-token')
        args=['--compute-sha',self.compute_sha,'--store-repo',str(self.repo),'--approval-commit',self.approval_commit,
              '--approval-sha256',self.approval_sha,'--request-id','995-1']
        def git(repo,*args):return 'https://github.com/'+('yuatom/stock-market-data-store' if Path(repo)==self.repo else 'yuatom/stock-market-data')
        from pathlib import Path
        with patch.dict(os.environ,env,clear=True),patch.object(self.m,'_git',side_effect=git),patch.object(self.m,'private_identity',side_effect=ValueError('private-secret')),patch.object(self.m,'execute') as execute,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.m.main(args),2);execute.assert_not_called()
            self.assertNotIn('private-secret',output.getvalue())

    def test_private_approval_repository_id_must_match_fresh_identity(self):
        with self.assertRaises(self.m.HostedCaptureError):
            self.call(repository_identity=dict(id=7654321,full_name='yuatom/stock-market-data-store',private=True,default_branch='main'))
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.approval_commit)

    def test_cli_success_omits_private_commit_and_evidence_hashes(self):
        import os,contextlib,io
        from pathlib import Path
        env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY='yuatom/stock-market-data',
            GITHUB_REF='refs/heads/main',GITHUB_SHA=self.compute_sha,GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='995',
            GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/soxx-capture-only.yml@refs/heads/main',
            GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',MARKET_DATA_STORE_TOKEN='synthetic-store-token')
        args=['--compute-sha',self.compute_sha,'--store-repo',str(self.repo),'--approval-commit',self.approval_commit,
              '--approval-sha256',self.approval_sha,'--request-id','995-1']
        def git(repo,*args):return 'https://github.com/'+('yuatom/stock-market-data-store' if Path(repo)==self.repo else 'yuatom/stock-market-data')
        result=dict(status='CAPTURE_RETAINED_UNQUALIFIED',requests_attempted=1,budget_debits=1,
            production_activation=False,market_qualified=False,store_commit_sha='PRIVATE-SYNTHETIC',
            approval_sha256='PRIVATE-SYNTHETIC',raw_response_sha256='PRIVATE-SYNTHETIC')
        with patch.dict(os.environ,env,clear=True),patch.object(self.m,'_git',side_effect=git),patch.object(self.m,'private_identity',return_value=dict(id=1234567,full_name='yuatom/stock-market-data-store',private=True,default_branch='main')),patch.object(self.m,'execute',return_value=result),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.m.main(args),0)
            actual=json.loads(output.getvalue())
            self.assertEqual(set(actual),{'status','requests_attempted','budget_debits','production_activation','market_qualified'})
            self.assertNotIn('PRIVATE-SYNTHETIC',output.getvalue())

    def test_replace_metadata_is_rejected_before_claim(self):
        # Even a replace ref unrelated to this capture must not alter Git's
        # object-identity semantics in the authorized checkout.
        tree=f.git(self.repo,'rev-parse',self.base+'^{tree}')
        a=f.git(self.repo,'commit-tree',tree,'-m','synthetic detached A')
        b=f.git(self.repo,'commit-tree',tree,'-m','synthetic detached B')
        f.git(self.repo,'replace',a,b)
        with patch.object(self.h.collectors,'_http_raw',return_value=self.raw):
            with self.assertRaises(self.m.HostedCaptureError):self.call()
        self.assertFalse((self.repo/'capture-evidence').exists())

    def test_graft_metadata_is_rejected_before_claim(self):
        (self.repo/'.git/info/grafts').write_text('')
        with patch.object(self.h.collectors,'_http_raw',return_value=self.raw):
            with self.assertRaises(self.m.HostedCaptureError):self.call()
        self.assertFalse((self.repo/'capture-evidence').exists())
