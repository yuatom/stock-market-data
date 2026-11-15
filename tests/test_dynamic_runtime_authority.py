import copy,unittest
import dynamic_runtime_authority as a
class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.env=dict(GITHUB_REPOSITORY=a.COMPUTE,GITHUB_REPOSITORY_ID='123',GITHUB_REPOSITORY_OWNER_ID=str(a.OWNER_ID),GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/discovery-requests',GITHUB_SHA='a'*40,GITHUB_WORKFLOW_REF=a.COMPUTE+'/'+a.CALLER+'@refs/heads/discovery-requests',GITHUB_WORKFLOW_SHA='a'*40,GITHUB_RUN_ID='42',GITHUB_RUN_ATTEMPT='1')
        self.env.update(GITHUB_ACTOR_ID=str(a.OWNER_ID),GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom')
        self.repo=dict(id=123,full_name=a.COMPUTE,owner=dict(id=a.OWNER_ID));self.store=dict(private=True,id=a.STORE_ID,full_name=a.STORE,owner=dict(id=a.OWNER_ID))
        self.run=dict(actor=dict(id=a.OWNER_ID,login='yuatom'),triggering_actor=dict(id=a.OWNER_ID,login='yuatom'),id=42,run_attempt=1,event='push',head_sha='a'*40,head_branch='discovery-requests',status='in_progress',repository=self.repo,head_repository=self.repo,workflow_id=8,referenced_workflows=[dict(path=a.COMPUTE+'/'+a.CALLEE+'@refs/heads/main',ref='refs/heads/main',sha='b'*40)])
        self.workflow=dict(id=8,path=a.CALLER)
    def check(self):return a.validate(self.env,self.repo,self.store,self.run,self.workflow,control_sha='a'*40,compute_sha='b'*40)
    def test_synthetic_bound_caller_and_callee(self):self.assertEqual(self.check()['compute_repository_id'],123)
    def test_literal_main_reference(self):
        self.run['referenced_workflows'][0]['path']=a.COMPUTE+'/'+a.CALLEE+'@main'
        self.check()
    def test_missing_or_duplicate_resolved_reference_rejected(self):
        for refs in (None,[],self.run['referenced_workflows']*2):
            with self.subTest(refs=refs):
                self.run['referenced_workflows']=refs
                with self.assertRaises(a.AuthorityError):self.check()
    def test_wrong_resolved_sha_not_caller_sha(self):
        self.run['referenced_workflows'][0]['sha']='a'*40
        with self.assertRaises(a.AuthorityError):self.check()
    def test_fork_repository_rejected(self):
        self.run['head_repository']=dict(id=124,full_name=a.COMPUTE)
        with self.assertRaises(a.AuthorityError):self.check()
    def test_wrong_store_identity(self):
        self.store['id']+=1
        with self.assertRaises(a.AuthorityError):self.check()
    def test_rerun_rejected(self):
        self.run['run_attempt']=2;self.env['GITHUB_RUN_ATTEMPT']='2'
        with self.assertRaises(a.AuthorityError):self.check()
    def test_wrong_caller_workflow_rejected(self):
        self.workflow['path']='.github/workflows/other.yml'
        with self.assertRaises(a.AuthorityError):self.check()
    def test_unknown_authority_never_falls_back_to_env(self):
        self.env['GH_TOKEN']='fake';self.env['MARKET_DATA_STORE_TOKEN']='fake'
        def fail(*args):raise a.AuthorityError('read unknown')
        with self.assertRaises(a.AuthorityError):a.attest(self.env,control_sha='a'*40,compute_sha='b'*40,read=fail)
    def test_store_must_remain_private(self):
        self.store['private']=False
        with self.assertRaises(a.AuthorityError):self.check()

    def test_nonowner_actor_rejected(self):
        self.run['actor']['id']=99
        with self.assertRaises(a.AuthorityError):self.check()
    def test_nonowner_triggering_actor_rejected(self):
        self.run['triggering_actor']['id']=99
        with self.assertRaises(a.AuthorityError):self.check()

    def test_observed_two_reference_shape(self):
        # Observed in runs 43516938981/43449413565/43109949331, reported by
        # coordinator from live API. Remaining test run fields are synthetic.
        sha='0d887d93f4ff89b3bf2ea2eb87dda69c6a217012'
        self.run['referenced_workflows']=[
            dict(path=a.COMPUTE+'/'+a.STANDARD+'@main',ref='refs/heads/main',sha=sha),
            dict(path=a.COMPUTE+'/'+a.CALLEE+'@main',ref='refs/heads/main',sha=sha)]
        a.validate(self.env,self.repo,self.store,self.run,self.workflow,control_sha='a'*40,compute_sha=sha)
        self.run['referenced_workflows'].reverse()
        a.validate(self.env,self.repo,self.store,self.run,self.workflow,control_sha='a'*40,compute_sha=sha)
    def test_standard_only_rejected(self):
        self.run['referenced_workflows'][0]['path']=a.COMPUTE+'/'+a.STANDARD+'@main'
        with self.assertRaises(a.AuthorityError):self.check()
    def test_optional_standard_requires_exact_scope(self):
        good=dict(path=a.COMPUTE+'/'+a.STANDARD+'@main',ref='refs/heads/main',sha='b'*40)
        for field,value in [('path',a.COMPUTE+'/.github/workflows/unknown.yml@main'),('path','other/repo/'+a.STANDARD+'@main'),('ref','refs/heads/other'),('sha','c'*40)]:
            with self.subTest(field=field,value=value):
                extra=dict(good);extra[field]=value
                self.run['referenced_workflows']=self.run['referenced_workflows'][:1]+[extra]
                with self.assertRaises(a.AuthorityError):self.check()
    def test_duplicate_standard_and_alias_dynamic_rejected(self):
        dynamic=self.run['referenced_workflows'][0]
        standard=dict(path=a.COMPUTE+'/'+a.STANDARD+'@main',ref='refs/heads/main',sha='b'*40)
        alias=dict(dynamic,path=a.COMPUTE+'/'+a.CALLEE+'@main')
        for refs in ([standard,standard],[dynamic,standard,standard],[dynamic,alias]):
            self.run['referenced_workflows']=refs
            with self.assertRaises(a.AuthorityError):self.check()

    def test_real_completed_run_evidence_and_simulated_active_copy(self):
        import json
        from pathlib import Path
        saved=json.loads((Path(__file__).parent/'fixtures/dynamic-runtime-run-43516938981.json').read_text())
        run=saved['run'];sha=run['referenced_workflows'][0]['sha'];control=run['head_sha']
        repo=dict(run['repository'],owner=dict(id=a.OWNER_ID))
        env=dict(self.env,GITHUB_REPOSITORY_ID=str(repo['id']),GITHUB_SHA=control,GITHUB_WORKFLOW_SHA=control,GITHUB_RUN_ID=str(run['id']))
        workflow=dict(id=run['workflow_id'],path=run['path'])
        self.assertEqual(run['status'],'completed')
        with self.assertRaises(a.AuthorityError):a.validate(env,repo,self.store,run,workflow,control_sha=control,compute_sha=sha)
        # Only this copy is a simulated active execution, not a live attestation.
        active=dict(run,status='in_progress')
        a.validate(env,repo,self.store,active,workflow,control_sha=control,compute_sha=sha)
