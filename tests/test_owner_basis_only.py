from datetime import datetime
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]

class BasisOnlyWorkflow(unittest.TestCase):
    def test_canonical_existing_caller_and_exclusion(self):
        caller = yaml.safe_load((ROOT/'.github/workflows/market-data-collector.yml').read_text())
        inputs = caller[True]['workflow_dispatch']['inputs']
        self.assertIn('daily_basis_only', inputs['mode']['options'])
        self.assertNotIn('basis_commitment', inputs)
        runtime = yaml.safe_load((ROOT/'.github/workflows/market-data-collector-runtime.yml').read_text())
        self.assertEqual(runtime['concurrency']['group'], 'market-data-collector')
        for gate in (caller['jobs']['run']['if'], runtime['jobs']['collect']['if']):
            self.assertIn('github.actor_id', gate)
            self.assertIn('github.run_attempt', gate)
            self.assertIn('github.triggering_actor', gate)
        steps = {s.get('name'): s for s in runtime['jobs']['collect']['steps']}
        for prefix in ('Materialize verified daily Store base', 'Collect qualified facts',
                       'Rebuild private Store inventory', 'Build Store publication',
                       'Persist private Store only', 'Verify durable Store publication'):
            matches = [v for k,v in steps.items() if k and k.startswith(prefix)]
            self.assertEqual(len(matches), 1)
            self.assertIn("!= 'daily_basis_only'", matches[0]['if'])
        self.assertIn("inputs.mode != 'daily_basis_only'", steps['Quietly clone private Market Data Store']['if'])
        self.assertIn('vars.DAILY_BASIS_GRANT_COMMITMENT', steps['Run finite daily basis owner']['if'])

class BasisOnlyGate(unittest.TestCase):
    def setUp(self):
        from scripts import run_daily_basis_route as H
        self.H = H
        self.slot = {'cutoff':'2026-01-16','execution_day':'2026-01-20'}
        self.grant = dict(slots=[self.slot],request_limit=1,credit_limit=1,verification_limit=1)
        self.key='synthetic-key-not-a-live-provider-key'
        self.scope=dict(self.slot,policy_id=H.BASIC_US_EOD_POLICY_ID,availability_semantics='eligibility_only_one_sample',
            provider='twelve_data_basic',symbol='SOXX',article='https://support.twelvedata.com/en/articles/9935903-us-equities-market-data',
            account_plan='Basic',included_credit_only=True,account_checked_at='2026-01-20T04:45:00+00:00',
            account_valid_until='2026-01-20T18:00:00+00:00',key_fingerprint=H.B.sha(b'twelve-data-basic-owner-key:v1\0'+self.key.encode()))
        self.authority = {'early_owner_policy':self.scope}

    def test_before_and_after_publication_and_cron(self):
        def run(now, manual=True, grant=None, authority=None, key=None):
            self.H.validate_early_scope(authority or self.authority, grant or self.grant, self.slot,
                trade_date=self.slot['execution_day'], now=datetime.fromisoformat(now), manual=manual,
                key=self.key if key is None else key, access={'twelve_data_basic':{
                  'provider_identity':'twelve_data_basic','base_url':'https://api.twelvedata.com','api_key_env':'TWELVE_DATA_API_KEY'}})
        for now in ('2026-01-20T04:59:59+00:00','2026-01-20T05:00:00+00:00',
                    '2026-01-20T12:05:00+00:00'):
            with self.subTest(now=now), self.assertRaises(self.H.HostedDailyError):
                run(now)
        run('2026-01-20T05:00:02+00:00')
        run('2026-01-20T12:05:00+00:00', manual=False)

        for altered in ({'article':'https://example.invalid/not-official'},
                        {'policy_id':'unsupported-revision'},
                        {'account_plan':'Other'}, {'included_credit_only':False},
                        {'account_valid_until':'2026-01-20T05:00:01+00:00'}):
            with self.subTest(altered=altered),self.assertRaises(self.H.HostedDailyError):
                run('2026-01-20T05:00:02+00:00',authority={'early_owner_policy':self.scope|altered})

        with self.assertRaises(self.H.HostedDailyError):
            run('2026-01-20T05:00:02+00:00', key='different-account')
        with self.assertRaises(self.H.HostedDailyError):
            run('2026-01-20T05:00:02+00:00', authority={'early_owner': dict(self.slot,vendor_publish_observed_at='2026-01-20T00:00:01-05:00')})
        self.H.validate_early_scope({},self.grant,self.slot,trade_date=self.slot['execution_day'],
            now=datetime.fromisoformat('2026-01-20T12:05:00+00:00'),manual=False,key='',access={})
        with self.assertRaises(self.H.HostedDailyError):
            run('2026-01-20T05:00:02+00:00', grant=self.grant|{'credit_limit':2})
        with self.assertRaises(self.H.HostedDailyError):
            run('2026-01-20T05:00:02+00:00', authority={'early_owner_policy':self.scope|{'cutoff':'2026-01-15'}})

    def test_actual_owner_first_attempt_and_trigger(self):
        H=self.H; sha='1'*40; actor={'id':10399225,'login':'yuatom'}
        env=dict(DAILY_BASIS_MODE='daily_basis_only',GITHUB_ACTIONS='true',GITHUB_REPOSITORY=H.COMPUTE,
                GITHUB_SHA=sha,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',
                GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',
                GITHUB_JOB='collect',DAILY_BASIS_TRIGGER_KIND='workflow_dispatch',DAILY_BASIS_SCHEDULE='',
                GITHUB_WORKFLOW_REF=H.COMPUTE+'/'+H.CALLER+'@refs/heads/main',GITHUB_RUN_ID='44')
        run=dict(id=44,workflow_id=77,path=H.CALLER,head_sha=sha,event='workflow_dispatch',run_attempt=1,
                 head_branch='main',status='in_progress',repository={'full_name':H.COMPUTE},
                 head_repository={'full_name':H.COMPUTE},actor=actor,triggering_actor=actor)
        auth=dict(runtime_workflow_id=77,owner_id=10399225)
        def check(actual):
            H.validate_runtime_surface(env,auth,actual,compute_sha=sha,trade_date='2026-01-20')
        check(run)
        for change in ({'run_attempt':2},{'event':'schedule'},{'triggering_actor':dict(actor,id=10399226)},
                       {'actor':dict(actor, login='not-owner')},{'head_branch':'off-main'}):
            with self.subTest(change=change),self.assertRaises(H.HostedDailyError):check(run|change)

class BasisOnlyResolver(unittest.TestCase):
    def test_resolver_does_not_require_ordinary_store_or_write_pin(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        import os
        runtime = yaml.safe_load((ROOT/'.github/workflows/market-data-collector-runtime.yml').read_text())
        snippet = next(s['run'] for s in runtime['jobs']['collect']['steps'] if s.get('id')=='invocation')
        with TemporaryDirectory() as scratch:
            outputs = Path(scratch)/'outputs'
            env=dict(EVENT_NAME='workflow_dispatch',MANUAL_MODE='daily_basis_only',
                     GITHUB_OUTPUT=str(outputs),RUNNER_TEMP=scratch)
            with patch.dict(os.environ,env,clear=True),patch('subprocess.check_output',return_value='1'*40) as command,patch('builtins.print'):
                exec(compile(snippet,'<workflow resolver>','exec'),{})
            command.assert_called_once_with(['git','-C','compute','rev-parse','HEAD'],text=True)
            self.assertFalse((Path(scratch)/'market-data-store-input-sha').exists())
            self.assertIn('mode=daily_basis_only',outputs.read_text())
