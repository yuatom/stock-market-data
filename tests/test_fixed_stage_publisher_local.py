"""Synthetic local Git publication; no network or providers."""
import json,subprocess,unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_fixed_stage_compatibility as fixture
from fixed_stage_publisher_local import LocalStagePublisher
from test_dynamic_daily_owner_integration import git
class StagePublisherTests(unittest.TestCase):
    prepare=fixture.StageCompatibilityTests.prepare
    commit=fixture.StageCompatibilityTests.commit
    def setUp(self):
        fixture.StageCompatibilityTests.setUp(self)
        self.remote=self.repo.parent/'remote.git'
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        git(self.repo,'remote','add','origin',str(self.remote));git(self.repo,'push','origin','HEAD:main')
        self.factory=SimpleNamespace(repo=self.repo,run_id=fixture.RUN,compute_sha=fixture.COMPUTE,invocation={},latest_owner=None)
        self.publisher=LocalStagePublisher(factory=self.factory,day=fixture.DAY)
        self.argv=['--mode','open_15m','--trade-date',fixture.DAY,'--store-root',str(self.store)]
    def test_zero_paid_stage_real_retention_and_exact_manifest(self):
        self.prepare();head=self.publisher(self.argv)
        manifest=json.loads(git(self.remote,'show',head+':'+self.publisher.last_manifest))
        for ref in manifest['required_outputs']:
            self.assertEqual(git(self.remote,'rev-parse',head+':data/market-data/'+ref['path']),ref['blob_sha'])
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
    def test_pushurl_changed_to_network_refused_before_publication(self):
        self.prepare();git(self.repo,'remote','set-url','--push','origin','https://example.invalid/no')
        with self.assertRaisesRegex(ValueError,'local bare'):self.publisher(self.argv)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_unexpected_file_refused_without_commit(self):
        self.prepare();(self.repo/'unexpected').write_text('x')
        with self.assertRaisesRegex(Exception,'outside Store'):self.publisher(self.argv)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_unknown_verification_stops_all_future_publication(self):
        self.prepare()
        with patch.object(self.publisher.publisher,'verify',side_effect=RuntimeError('synthetic retention unknown')):
            with self.assertRaisesRegex(RuntimeError,'retention unknown'):self.publisher(self.argv)
        remote=git(self.remote,'rev-parse','refs/heads/main')
        self.assertNotEqual(remote,self.start)
        self.assertEqual(self.publisher.publisher.head,self.start)
        with self.assertRaisesRegex(ValueError,'stopped'):self.publisher(self.argv)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),remote)
    def test_nothing_missing_uses_existing_close_snapshot_without_owner(self):
        self.prepare('close');self.commit();git(self.repo,'push','origin','HEAD:main')
        publisher=LocalStagePublisher(factory=self.factory,day=fixture.DAY,eligible_universe=[('AAA','stocks')])
        args=['--mode','close_retry','--trade-date',fixture.DAY,'--store-root',str(self.store)]
        result=publisher(args)
        self.assertEqual(result,git(self.remote,'rev-parse','refs/heads/main'))
        manifest=json.loads(git(self.remote,'show',result+':'+publisher.last_manifest))
        self.assertFalse(any('fixed-intraday-attempt-' in r['path'] for r in manifest['required_outputs']))
    def test_pushurl_drift_after_finalizer_refused_before_push(self):
        import fixed_stage_publisher_candidate as module
        original=module.build_candidate;self.prepare()
        def drift(**kwargs):
            out=original(**kwargs)
            git(self.repo,'remote','set-url','--push','origin','https://example.invalid/blocked')
            return out
        with patch.object(module,'build_candidate',side_effect=drift),patch('admit_market_data_baseline.git',wraps=__import__('admit_market_data_baseline').git) as calls:
            # Intercept the pre-fix push rather than attempting any network.
            original_git=calls._mock_wraps
            def guarded(repo,*args):
                if args and args[0]=='push':raise AssertionError('unsafe push reached')
                return original_git(repo,*args)
            calls.side_effect=guarded
            with self.assertRaisesRegex(ValueError,'local bare'):self.publisher(self.argv)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_network_scheme_changed_after_check_is_blocked_by_git_protocol(self):
        import fixed_stage_publisher_local as module
        self.prepare();original=module.subprocess.run;seen=[]
        def race(cmd,*args,**kwargs):
            if cmd[:3]==['git','-C',str(self.repo)] and cmd[3:4]==['push']:
                self.assertEqual(kwargs['env']['GIT_ALLOW_PROTOCOL'],'file')
                original(['git','-C',str(self.repo),'remote','set-url','--push','origin','https://example.invalid/blocked'],check=True,capture_output=True)
                result=original(cmd,*args,**kwargs)
                self.assertIn("transport 'https' not allowed",result.stderr)
                seen.append(True)
                return result
            return original(cmd,*args,**kwargs)
        with patch.object(module.subprocess,'run',side_effect=race):
            with self.assertRaisesRegex(ValueError,'local-only Git command failed'):self.publisher(self.argv)
        self.assertEqual(seen,[True]);self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
