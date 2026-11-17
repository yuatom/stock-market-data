"""Zero-network closure tests with actual local Git objects, no remote reads."""
import json, unittest
from pathlib import Path
from unittest.mock import patch
import test_fixed_intraday_offline as f
from test_dynamic_daily_owner_integration import git
from dynamic_daily_owner import blob, encode
from market_data_store import write_snapshot
from build_store_publication_manifest import build_manifest
from fixed_finalizer_offline import build_candidate, FinalizerError

class FinalizerTests(unittest.TestCase):
    setUp=f.FixedIntradayOfflineTest.setUp
    make_owner=f.FixedIntradayOfflineTest.make_owner
    responses=f.FixedIntradayOfflineTest.responses
    collect=f.FixedIntradayOfflineTest.collect
    paths=f.FixedIntradayOfflineTest.paths
    def prepare(self):
        self.collect()
        refs=[]
        for p in self.paths():
            if '/sessions/' in p:
                raw=(self.repo/p).read_bytes();refs.append(dict(path=p.removeprefix('data/market-data/'),blob_sha=blob(raw)))
        self.snapshot,_=write_snapshot(self.store,stage='open_15m',trade_date=f.fixture.DAY,
            snapshot_id='open_15m-synthetic',generated_at='2026-11-14T10:00:00-05:00',
            data_refs=refs,coverage={},missing=[],target_window={'start':'09:30','end':'09:45'},actual_data_cutoff='2026-11-14T09:44:00-05:00')
        self.args=dict(repo=self.repo,start_sha=self.start,confirmed_head=self.owner.head,
            day=f.fixture.DAY,stage='open_15m',request_id='collector-fixed-synthetic-1',
            transaction_id='open15-synthetic-1',compute_sha=self.sha,snapshot_path=self.snapshot)
    def test_committed_attempt_outputs_not_lost_from_final_manifest(self):
        self.prepare()
        manifest=build_candidate(**self.args)
        payload=json.loads(manifest.read_bytes());paths={x['path'] for x in payload['required_outputs']}
        attempts={p.removeprefix('data/market-data/') for p in self.paths() if 'fixed-intraday-attempt-' in p or p.endswith('.raw')}
        self.assertTrue(attempts);self.assertTrue(attempts<=paths)
        for ref in payload['required_outputs']:
            self.assertEqual(blob((self.store/ref['path']).read_bytes()),ref['blob_sha'])
    def test_existing_builder_omits_committed_attempts_reproduction(self):
        self.prepare()
        result=self.repo.parent/'synthetic-result.json';result.write_bytes(encode(dict(snapshot_path=self.snapshot)))
        manifest=build_manifest(store_root=self.store,result_file=result,request_id='synthetic',trade_date=f.fixture.DAY,stage='open_15m')
        paths={x['path'] for x in json.loads(manifest.read_bytes())['required_outputs']}
        self.assertFalse(any('fixed-intraday-attempt-' in p for p in paths))
    def test_committed_output_local_tamper_rejected_before_writes(self):
        self.prepare();raw=next(self.repo/p for p in self.paths() if p.endswith('.raw'));raw.write_bytes(b'tampered')
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
    def test_unclosed_attempt_rejected(self):
        from dynamic_daily_owner import DynamicPublicationUnknown
        with self.assertRaises(DynamicPublicationUnknown):self.collect({'AAA':None,'BBB':None})
        with self.assertRaisesRegex(FinalizerError,'unknown or mismatched attempt'):build_candidate(repo=self.repo,start_sha=self.start,confirmed_head=self.owner.head,day=f.fixture.DAY,stage='open_15m',request_id='r',transaction_id='open15-synthetic-1',compute_sha=self.sha,snapshot_path=f'snapshots/{f.fixture.DAY[:7]}/{f.fixture.DAY}/open_15m/missing.json')
    def test_wrong_transaction_rejected(self):
        self.prepare();self.args['transaction_id']='other'
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
    def test_extra_unrelated_worktree_file_rejected(self):
        self.prepare();(self.repo/'outside.txt').write_text('unrelated')
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
    def test_snapshot_ref_tamper_rejected(self):
        self.prepare();p=self.store/self.snapshot;v=json.loads(p.read_bytes());v['data_refs'][0]['blob_sha']='0'*40;p.write_bytes(encode(v))
        latest=p.parent/'latest.json';pointer=json.loads(latest.read_bytes());pointer['snapshot_blob_sha']=blob(p.read_bytes());latest.write_bytes(encode(pointer))
        with self.assertRaisesRegex(FinalizerError,'snapshot reference blob mismatch'):build_candidate(**self.args)
    def test_symlink_output_rejected(self):
        self.prepare();target=self.repo.parent/'outside';target.write_text('untouched')
        (self.store/'collector-state/market-data-inventory.json').symlink_to(target)
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
        self.assertEqual(target.read_text(),'untouched')
    def test_no_git_transport_or_provider_called(self):
        self.prepare()
        import subprocess
        original=subprocess.run
        def local_only(argv,*args,**kwargs):
            self.assertFalse(set(argv)&{'fetch','push','pull','ls-remote'})
            return original(argv,*args,**kwargs)
        with patch.object(subprocess,'run',side_effect=local_only):build_candidate(**self.args)
    def test_confirmed_head_mismatch_rejected(self):
        self.prepare();self.args['confirmed_head']=self.start
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
    def test_deleted_committed_output_rejected(self):
        self.prepare();next(self.repo/p for p in self.paths() if p.endswith('.raw')).unlink()
        with self.assertRaises(FinalizerError):build_candidate(**self.args)
    def test_git_reads_disable_lazy_fetch_and_all_transport_protocols(self):
        from fixed_finalizer_offline import local_git
        import subprocess
        original=subprocess.run
        def inspect(argv,*args,**kw):
            self.assertEqual(kw['env']['GIT_ALLOW_PROTOCOL'],'')
            self.assertEqual(kw['env']['GIT_NO_LAZY_FETCH'],'1')
            return original(argv,*args,**kw)
        with patch.object(subprocess,'run',side_effect=inspect):self.assertEqual(local_git(self.repo,'rev-parse','HEAD').decode().strip(),self.start)
    def test_open30_missing_required_proof_rejected(self):
        self.prepare()
        snap=json.loads((self.store/self.snapshot).read_bytes())
        path,_=write_snapshot(self.store,stage='open_30m',trade_date=f.fixture.DAY,
            snapshot_id='open_30m-synthetic',generated_at='2026-11-14T10:00:00-05:00',
            data_refs=snap['data_refs'],coverage={},missing=[],target_window={'start':'09:30','end':'10:00'})
        self.args.update(stage='open_30m',snapshot_path=path)
        with self.assertRaisesRegex(FinalizerError,'deterministic proof'):build_candidate(**self.args)

    def test_committed_raw_then_deleted_rejected(self):
        self.prepare()
        raws=[p for p in self.paths() if p.endswith('.raw')]
        git(self.repo,'rm',*raws);git(self.repo,'commit','-m','synthetic raw deletion')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'deleted or non-file output'):build_candidate(**self.args)
    def test_committed_raw_content_address_mismatch_rejected(self):
        self.prepare()
        path=next(p for p in self.paths() if p.endswith('.raw'))
        (self.repo/path).write_bytes(b'synthetic mutated raw')
        git(self.repo,'add',path);git(self.repo,'commit','-m','synthetic raw corruption')
        self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
        with self.assertRaisesRegex(FinalizerError,'raw content hash mismatch'):build_candidate(**self.args)
