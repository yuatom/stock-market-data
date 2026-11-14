"""Synthetic authority/platform tests, no credentials or external side effects."""
from contextlib import nullcontext
from datetime import datetime,timezone,timedelta
import copy
import unittest
from unittest.mock import Mock,patch
from scripts import run_one_shot_soxx_basis_gap as M,provider_basis_snapshot as B

class OneShotScope(unittest.TestCase):
    def fixture(self):
        now=datetime(2026,11,14,13,0,tzinfo=timezone.utc)
        calendar=dict(sessions=[dict(trade_date=f'synthetic-{i}') for i in range(318)]+[dict(trade_date='2026-11-12'),dict(trade_date='2026-11-13')])
        grant=dict(compute_sha='a'*40,consumer_sha='b'*40,workflow_source_sha='c'*40,repository_id=11,owner_id=12,request_limit=1,credit_limit=1,verification_limit=1,initial_selector={'basis_id':M.BASIS},baseline_ref={'sha256':M.DIAGNOSTIC},request_template={'start_date':'2025-08-08','end_date':'2026-11-11'},slots=[dict(slot_id='soxx-nov14-gap',execution_day='2026-11-14',cutoff='2026-11-13',valid_from=now.isoformat(),receipt_until=(now+timedelta(hours=2)).isoformat())])
        scope=dict(schema_version=1,kind='one_shot_soxx_gap_authority',compute_sha='a'*40,consumer_sha='b'*40,workflow_source_sha='c'*40,repository_id=11,owner_id=12,runtime_workflow_id=13,readiness_run_id=14,valid_from=now.isoformat(),valid_until=(now+timedelta(hours=3)).isoformat(),actions_free_until=(now+timedelta(hours=4)).isoformat())
        return scope,grant,calendar,now
    def validate(self,scope,grant,calendar,now):
        for key,data in (('grant_ref',B.encode(grant)),('calendar_ref',B.encode(calendar))):scope[key]=dict(commit='d'*40,path='basis-authority/'+key+'.json',blob_sha=B.blob_sha(data),sha256=B.sha(data))
        raw=B.encode(scope);return M.validate_scope(raw,B.sha(raw),B.encode(grant),B.encode(calendar),compute_sha='a'*40,now=now)
    def test_bounded_manual_scope(self):self.validate(*self.fixture())
    def test_extra_request_rejected(self):
        s,g,c,n=self.fixture();g['request_limit']=2
        with self.assertRaises(ValueError):self.validate(s,g,c,n)
    def test_old_basis_rejected(self):
        s,g,c,n=self.fixture();g['initial_selector']['basis_id']='e'*64
        with self.assertRaises(ValueError):self.validate(s,g,c,n)
    def test_other_date_rejected(self):
        s,g,c,n=self.fixture();g['slots'][0]['cutoff']='2026-11-12'
        with self.assertRaises(ValueError):self.validate(s,g,c,n)
    def test_expired_scope_rejected(self):
        s,g,c,n=self.fixture()
        with self.assertRaises(ValueError):self.validate(s,g,c,n+timedelta(hours=4))
    def test_wrong_diagnostic_rejected(self):
        s,g,c,n=self.fixture();g['baseline_ref']['sha256']='e'*64
        with self.assertRaises(ValueError):self.validate(s,g,c,n)
    def surface(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_REPOSITORY=M.COMPUTE,GITHUB_SHA='a'*40,GITHUB_REF='refs/heads/main',GITHUB_JOB='soxx_gap',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_WORKFLOW_REF=M.COMPUTE+'/'+M.WORKFLOW+'@refs/heads/main',GITHUB_RUN_ID='100')
        repo=dict(full_name=M.COMPUTE,id=11,owner={'id':12});run=dict(id=100,repository={'id':11},head_repository={'id':11},head_sha='a'*40,path=M.WORKFLOW,event='workflow_dispatch',run_attempt=1,head_branch='main',status='in_progress',actor={'id':12},triggering_actor={'id':12})
        return env,run,repo
    def test_actual_manual_surface(self):M.validate_surface(*self.surface(),'a'*40)
    def test_schedule_cannot_impersonate_manual(self):
        e,r,p=self.surface();e['GITHUB_EVENT_NAME']='schedule';r['event']='schedule'
        with self.assertRaises(ValueError):M.validate_surface(e,r,p,'a'*40)
    def test_wrong_owner_rejected(self):
        e,r,p=self.surface();r['actor']['id']=99
        with self.assertRaises(ValueError):M.validate_surface(e,r,p,'a'*40)
    def hosted_mocks(self):
        scope,grant,calendar,now=self.fixture();self.validate(scope,grant,calendar,now)
        raw=B.encode(scope);store=Mock();store.lock.return_value=nullcontext();store.head.return_value='f'*40;store.materialize_ref.return_value='d'*40;store.g.text.return_value='d'*40
        store.read.side_effect=lambda commit,path: raw if path==M.SCOPE_PATH else B.encode(grant if 'grant_ref' in path else calendar)
        env,run,repo=self.surface();run['workflow_id']=13;api=Mock();api.request.side_effect=lambda method,path:run if '/actions/runs/' in path else repo
        args=dict(store=store,api=api,scope_commit='d'*40,scope_sha=B.sha(raw),expected_store_sha='f'*40,compute_sha='a'*40,env=env,clock=lambda:now,key='',access={},source_check=lambda:None,capture=Mock())
        return args
    def test_readiness_failure_stops_before_debit(self):
        args=self.hosted_mocks()
        with patch.object(M,'check_private_readiness',side_effect=ValueError('not ready')),patch.object(M,'DailyUpdater') as updater,self.assertRaises(ValueError):M.run_once(**args)
        updater.assert_not_called();args['capture'].assert_not_called()
    def test_unknown_verification_does_not_promote_or_repeat(self):
        args=self.hosted_mocks();updater=Mock()
        with patch.object(M,'check_private_readiness',return_value={}),patch.object(M,'DailyUpdater',return_value=updater),patch.object(M,'wait_for_daily_budget_window'),patch.object(M,'dispatch_verify_once',side_effect=TimeoutError()) as dispatch,self.assertRaises(TimeoutError):M.run_once(**args)
        updater.run_slot.assert_called_once();dispatch.assert_called_once();updater.promote.assert_not_called()
    def test_drift_rejected_before_scope_or_capture(self):
        store=Mock();store.lock.return_value=nullcontext();store.head.return_value='x'*40;capture=Mock()
        with self.assertRaises(ValueError):M.run_once(store=store,api=Mock(),scope_commit='d'*40,scope_sha='e'*64,expected_store_sha='f'*40,compute_sha='a'*40,env={},clock=lambda:None,key='',access={},source_check=lambda:None,capture=capture)
        store.materialize_ref.assert_not_called();capture.assert_not_called()

if __name__=='__main__':unittest.main()
