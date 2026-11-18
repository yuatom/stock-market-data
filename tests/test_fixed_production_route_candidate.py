"""No provider, Git push, or network calls. Route and construction contract tests."""
import unittest
from unittest.mock import patch
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
class ProductionRouteTests(unittest.TestCase):
    def test_workflow_routes_all_legacy_publication_steps_exclusively(self):
        data=yaml.safe_load((ROOT/'.github/workflows/market-data-collector-runtime.yml').read_text())
        steps=data['jobs']['collect']['steps'];byname={x['name']:x for x in steps}
        for name in ['Collect qualified facts into private Store checkout','Rebuild private Store inventory','Build Store publication manifest','Persist private Store only','Verify durable Store publication']:
            self.assertIn("steps.invocation.outputs.publication_route == 'legacy'",byname[name]['if'])
        guarded=byname['Collect and publish guarded fixed windows']
        self.assertEqual(guarded['if'],"steps.invocation.outputs.publication_route == 'guarded'")
        self.assertIn('run_fixed_guarded_candidate.py',guarded['run'])
        self.assertEqual(data['jobs']['collect']['timeout-minutes'],12)
    def test_route_preserves_nonfixed_modes(self):
        from run_fixed_guarded_candidate import publication_route
        for mode in ['open_15m','open_30m','open_60m','close','close_retry','close_final']:
            self.assertEqual(publication_route(mode),'guarded')
        for mode in ['previous_session_eod','historical_context_repair','premarket','smoke_readonly']:
            self.assertEqual(publication_route(mode),'legacy')
    def test_production_publisher_uses_existing_remote_publisher_not_local_transport(self):
        from fixed_stage_publisher_candidate import FixedStagePublisher
        from admit_market_data_baseline import StorePublisher
        self.assertIs(FixedStagePublisher.publisher_type,StorePublisher)
        from types import SimpleNamespace
        factory=SimpleNamespace(repo=Path('/synthetic'),run_id='123-1')
        with patch('fixed_stage_publisher_candidate.check_store_remote'),patch('fixed_stage_publisher_candidate.git',return_value='a'*40),patch.object(FixedStagePublisher,'publisher_type') as constructor:
            publisher=FixedStagePublisher(factory=factory,day='2026-11-17')
            self.assertEqual(constructor.call_args.args[1],'a'*40)
    def test_remote_mismatch_refused_without_transport(self):
        from fixed_stage_publisher_candidate import check_store_remote
        with patch('fixed_stage_publisher_candidate.git',return_value='https://example.invalid/no') as git:
            with self.assertRaisesRegex(ValueError,'Store remote'):check_store_remote(Path('/synthetic'))
            self.assertTrue(all(call.args[1:3]==('remote','get-url') for call in git.call_args_list))
    def test_runner_passes_existing_identity_and_no_legacy_fallback(self):
        import os
        import run_fixed_guarded_candidate as module
        metadata=dict(mode='close_retry',trade_date='2026-11-17',logical_trigger='schedule',request_id=None,transaction_id=None,control_ref=None,market_data_contract_sha='a'*40)
        args=['--mode=close_retry','--trade-date','2026-11-17','--store-root','/synthetic/data/market-data','--universe','synthetic.json']
        before=dict(os.environ);seen=[]
        def execute(args,**kwargs):
            self.assertEqual(set(kwargs),{'owner_factory','stage_publish'})
            self.assertEqual(os.environ['TWELVE_DATA_API_KEY'],'SYNTHETIC_KEY')
            self.assertEqual(os.environ['MARKET_DATA_COMPUTE_SHA'],'a'*40)
            self.assertNotIn('MARKET_DATA_STORE_TOKEN',os.environ)
            seen.append(True);return 0
        with patch.object(module,'git_environment',return_value={'GIT_CONFIG_COUNT':'0'}),patch.object(module,'check_store_remote'),patch.object(module,'git',return_value='a'*40),patch.object(module,'WorkflowFixedOwnerFactory') as factory,patch.object(module,'FixedStagePublisher'),patch.object(module.entry.collection,'load_collection_universe',return_value={}),patch.object(module.entry,'_live_close_universe',return_value=[]),patch.object(module,'execute',side_effect=execute):
            self.assertEqual(module.run(args,metadata,repo=Path('/synthetic'),compute=ROOT,run_id='123-2',env=dict(MARKET_DATA_STORE_TOKEN='SYNTHETIC_TOKEN',TWELVE_DATA_API_KEY='SYNTHETIC_KEY',MARKET_DATA_STORE_INPUT_SHA='a'*40)),0)
            self.assertEqual(factory.call_args.kwargs['run_id'],'123-2')
            self.assertIsNone(factory.call_args.kwargs['invocation']['request_id'])
            self.assertEqual(factory.call_args.kwargs['invocation']['trigger'],'schedule')
        self.assertEqual(seen,[True]);self.assertEqual(dict(os.environ),before)
    def test_runner_stops_on_compute_drift_before_factory(self):
        import run_fixed_guarded_candidate as module
        metadata=dict(mode='open_15m',trade_date='2026-11-17',market_data_contract_sha='b'*40)
        args=['--mode','open_15m','--trade-date','2026-11-17']
        with patch.object(module,'git_environment',return_value={}),patch.object(module,'check_store_remote'),patch.object(module,'git',return_value='a'*40),patch.object(module,'WorkflowFixedOwnerFactory') as factory:
            with self.assertRaisesRegex(ValueError,'compute input drift'):
                module.run(args,metadata,repo=Path('/synthetic'),compute=ROOT,run_id='1-1',env=dict(MARKET_DATA_STORE_TOKEN='SYNTHETIC',MARKET_DATA_STORE_INPUT_SHA='a'*40))
            factory.assert_not_called()
    def test_workflow_resolver_mode_set_equals_runner(self):
        import ast
        from run_fixed_guarded_candidate import publication_route
        data=yaml.safe_load((ROOT/'.github/workflows/market-data-collector-runtime.yml').read_text())
        resolver=next(s for s in data['jobs']['collect']['steps'] if s.get('id')=='invocation')
        tree=ast.parse(resolver['run'])
        assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='fixed_modes' for t in n.targets))
        modes=ast.literal_eval(assignment.value)
        self.assertEqual(modes,{'open_15m','open_30m','open_60m','close','close_retry','close_final'})
        self.assertTrue(all(publication_route(m)=='guarded' for m in modes))
