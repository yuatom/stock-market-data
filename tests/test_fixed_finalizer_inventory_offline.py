"""Local integration of exact inventory bytes and attempt output closure."""
import json
import unittest
from unittest.mock import patch
import test_fixed_finalizer_offline as fixture
import test_fixed_intraday_offline as f
import fixed_inventory_offline as inventory
from fixed_finalizer_offline import build_candidate,FinalizerError
from dynamic_daily_owner import blob
from market_data_store import write_snapshot

class IntegratedInventoryTests(unittest.TestCase):
    setUp=fixture.FinalizerTests.setUp
    make_owner=fixture.FinalizerTests.make_owner
    collect=fixture.FinalizerTests.collect
    paths=fixture.FinalizerTests.paths
    def responses(self):
        stage=self.owner.request['stage']
        hour,first,count={'open_15m':(9,30,15),'open_30m':(9,45,15),'open_60m':(10,0,30)}[stage]
        rows=[dict(datetime=f'2026-11-14 {hour:02d}:{first+i:02d}:00',open='10',high='12',low='9',close='11',volume='100') for i in range(count)]
        return {s:json.dumps(dict(meta=dict(symbol=s,interval='1min'),values=rows)).encode() for s in ['AAA','BBB']}
    def prepare(self,stage='open_15m'):
        self.owner=self.make_owner(stage=stage);self.collect()
        refs=[dict(path=p.removeprefix('data/market-data/'),blob_sha=blob((self.repo/p).read_bytes())) for p in self.paths() if '/sessions/' in p]
        end={'open_15m':'09:45','open_30m':'10:00','open_60m':'10:30'}[stage]
        snap,_=write_snapshot(self.store,stage=stage,trade_date=f.fixture.DAY,snapshot_id=stage+'-synthetic',generated_at='2026-11-14T11:00:00-05:00',data_refs=refs,coverage={},missing=[],target_window=dict(start='09:30',end=end))
        self.args=dict(repo=self.repo,start_sha=self.start,confirmed_head=self.owner.head,day=f.fixture.DAY,
            stage=stage,request_id='collector-fixed-synthetic-1',transaction_id='open15-synthetic-1',compute_sha=self.sha,snapshot_path=snap)
        if stage!='open_15m':
            from build_deterministic_metric_proof import build_proof,_write_json,_proof_pointer
            proof,path=build_proof(self.store,trade_date=f.fixture.DAY,stage=stage,data_plane_commit_sha=self.sha)
            proof_blob=_write_json(self.store/path,proof)
            self.proof_path=path;self.proof_subject_count=len(proof['subjects'])
            latest=self.store/f'proofs/deterministic-metrics/2026-11/{f.fixture.DAY}/{stage}/latest.json'
            _write_json(latest,_proof_pointer(proof,rel=path,blob=proof_blob))
    def assert_bundle(self):
        captured=[];original=inventory.build_candidate
        def observe(**kw):
            result=original(**kw);captured.append(result);return result
        with patch.object(inventory,'build_candidate',side_effect=observe):manifest=build_candidate(**self.args)
        refs={r['path']:r['blob_sha'] for r in json.loads(manifest.read_bytes())['required_outputs']}
        path='collector-state/market-data-inventory.json'
        self.assertIn(path,refs)
        self.assertEqual(len(captured),1)
        self.assertEqual((self.store/path).read_bytes(),captured[0]['inventory_bytes'])
        self.assertEqual(refs[path],captured[0]['inventory_blob_sha'])
        self.assertEqual(refs[self.args['snapshot_path']],captured[0]['input_blobs'][self.args['snapshot_path']])
        self.assertTrue(any(p.endswith('.raw') for p in refs))
        for p,sha in refs.items():self.assertEqual(blob((self.store/p).read_bytes()),sha)
        if self.args['stage']!='open_15m':
            self.assertIn(self.proof_path,refs);self.assertEqual(self.proof_subject_count,2)
    def test_open15_inventory_exact_bytes_in_manifest(self):
        self.prepare();self.assert_bundle()
    def test_open30_inventory_and_proof_share_view(self):
        self.prepare('open_30m');self.assert_bundle()
    def test_open60_inventory_and_proof_share_view(self):
        self.prepare('open_60m');self.assert_bundle()
    def test_drift_after_frozen_inventory_rejected_before_outputs(self):
        self.prepare();original=inventory.build_candidate
        def drift(**kw):
            result=original(**kw)
            target=self.store/self.args['snapshot_path'];target.write_bytes(target.read_bytes()+b' ')
            return result
        with patch.object(inventory,'build_candidate',side_effect=drift),self.assertRaisesRegex(FinalizerError,'frozen view changed'):build_candidate(**self.args)
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
    def test_proof_drift_after_frozen_view_rejected(self):
        self.prepare('open_30m');original=inventory.build_candidate
        def drift(**kw):
            result=original(**kw);p=self.store/self.proof_path;p.write_bytes(p.read_bytes()+b' ');return result
        with patch.object(inventory,'build_candidate',side_effect=drift),self.assertRaisesRegex(FinalizerError,'frozen view changed'):build_candidate(**self.args)
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
    def test_untrusted_inventory_worktree_input_rejected(self):
        self.prepare();path=self.store/'collector-state/market-data-inventory.json';path.write_bytes(b'{"untrusted":true}')
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
        self.assertEqual(path.read_bytes(),b'{"untrusted":true}')

    def test_open60_proof_drift_after_frozen_view_rejected(self):
        self.prepare('open_60m');original=inventory.build_candidate
        def drift(**kw):
            result=original(**kw);p=self.store/self.proof_path;p.write_bytes(p.read_bytes()+b' ');return result
        with patch.object(inventory,'build_candidate',side_effect=drift),self.assertRaisesRegex(FinalizerError,'frozen view changed'):build_candidate(**self.args)
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
    def test_new_untracked_output_after_freeze_rejected(self):
        self.prepare();original=inventory.build_candidate
        def drift(**kw):
            result=original(**kw);(self.store/'unexpected.json').write_bytes(b'{}');return result
        with patch.object(inventory,'build_candidate',side_effect=drift),self.assertRaisesRegex(FinalizerError,'output set changed'):build_candidate(**self.args)
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
    def test_tracked_prior_inventory_replaced_by_exact_generated_bytes(self):
        from test_dynamic_daily_owner_integration import git
        path=self.store/'collector-state/market-data-inventory.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'{"prior":true}')
        git(self.repo,'add','data/market-data/collector-state/market-data-inventory.json')
        git(self.repo,'commit','-qm','synthetic prior inventory');git(self.repo,'push','origin','HEAD:main')
        self.start=git(self.repo,'rev-parse','HEAD')
        self.prepare();self.assert_bundle()
        self.assertNotEqual(path.read_bytes(),b'{"prior":true}')
