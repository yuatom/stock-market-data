"""Import isolation and shared-algorithm regression; no provider or network."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
class PublisherSplitTests(unittest.TestCase):
    def test_production_import_does_not_load_local_simulation(self):
        code='''
import importlib.abc,sys
class DenyLocal(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'fixed_stage_publisher_local','fixed_intraday_offline'}:
   raise AssertionError('production imported local simulation: '+fullname)
sys.meta_path.insert(0,DenyLocal())
from fixed_stage_publisher_candidate import FixedStagePublisher
from admit_market_data_baseline import StorePublisher
assert FixedStagePublisher.publisher_type is StorePublisher
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=dict(os.environ,PYTHONPATH=str(ROOT/'scripts')),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_local_transport_resides_only_in_test_support(self):
        import fixed_stage_publisher_local as local
        self.assertEqual(Path(local.__file__).resolve().parent,ROOT/'tests')
    def test_paths_share_publication_algorithm(self):
        from fixed_stage_publisher_candidate import FixedStagePublisher
        from fixed_stage_publisher_local import LocalStagePublisher,LocalOnlyStorePublisher
        self.assertIs(LocalStagePublisher._publish,FixedStagePublisher._publish)
        self.assertIs(LocalStagePublisher.__call__,FixedStagePublisher.__call__)
        self.assertIs(LocalStagePublisher.publisher_type,LocalOnlyStorePublisher)

class SimulatedProductionPathTests(unittest.TestCase):
    """Actual production class/StorePublisher, synthetic file-only remote.

    Only the production hostname admission is substituted. Child protocol policy
    forbids network, and no provider is present in this fixture.
    """
    def setUp(self):
        from unittest.mock import patch
        from test_fixed_stage_publisher_local import StagePublisherTests
        from fixed_stage_publisher_candidate import FixedStagePublisher
        from fixed_intraday_offline import require_local_bare
        self.fixture=StagePublisherTests(methodName='test_zero_paid_stage_real_retention_and_exact_manifest')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        guard=patch.dict(os.environ,{'GIT_ALLOW_PROTOCOL':'file','GIT_TERMINAL_PROMPT':'0'})
        guard.start();self.addCleanup(guard.stop)
        check=patch('fixed_stage_publisher_candidate.check_store_remote',side_effect=require_local_bare)
        check.start();self.addCleanup(check.stop)
        self.publisher=FixedStagePublisher(factory=self.fixture.factory,day='2026-11-17')
    def test_production_class_publishes_exact_manifest_through_existing_transport(self):
        import json
        from test_dynamic_daily_owner_integration import git
        from admit_market_data_baseline import StorePublisher
        f=self.fixture;f.prepare();head=self.publisher(f.argv)
        self.assertIs(type(self.publisher.publisher),StorePublisher)
        self.assertEqual(git(f.remote,'rev-parse','refs/heads/main'),head)
        manifest=json.loads(git(f.remote,'show',head+':'+self.publisher.last_manifest))
        for ref in manifest['required_outputs']:
            self.assertEqual(git(f.remote,'rev-parse',head+':data/market-data/'+ref['path']),ref['blob_sha'])
    def test_production_class_freezes_after_unknown_verification(self):
        from unittest.mock import patch
        from test_dynamic_daily_owner_integration import git
        f=self.fixture;f.prepare()
        with patch.object(self.publisher.publisher,'verify',side_effect=RuntimeError('synthetic unknown')):
            with self.assertRaisesRegex(RuntimeError,'synthetic unknown'):self.publisher(f.argv)
        head=git(f.remote,'rev-parse','refs/heads/main')
        self.assertNotEqual(head,f.start)
        with self.assertRaisesRegex(ValueError,'stopped'):self.publisher(f.argv)
        self.assertEqual(head,git(f.remote,'rev-parse','refs/heads/main'))
