"""Synthetic local object-only inventory candidate; no published authority."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from dynamic_daily_owner import blob, encode
from fixed_inventory_offline import build_candidate, InventoryError
from test_dynamic_daily_owner_integration import git

DAY='2026-11-17'
class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name).resolve()/'repo';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        git(self.repo,'config','user.name','synthetic');git(self.repo,'config','user.email','test@example.invalid')
        self.store=self.repo/'data/market-data'
        self.capture='sessions/2026-11/2026-11-17/regular/0930-0945/twelve_data_basic/aaa.json'
        self.raw=encode(dict(trade_date=DAY,session='regular',window=dict(start='09:30',end='09:45'),provider='twelve_data_basic',qualified_facts=[]))
        self.write(self.capture,self.raw);git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic exact source')
        self.head=git(self.repo,'rev-parse','HEAD')
        self.args=dict(repo=self.repo,confirmed_head=self.head,day=DAY,stage='open_15m',compute_sha='a'*40)
        self.set_snapshot('open_15m','09:45')
        net=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'));net.start();self.addCleanup(net.stop)
    def write(self,p,raw):
        target=self.store/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    def set_snapshot(self,stage,end):
        self.snapshot=f'snapshots/2026-11/{DAY}/{stage}/synthetic.json'
        snap=dict(schema_version=1,coverage={},actual_data_cutoff=None,snapshot_id='synthetic',trade_date=DAY,stage=stage,generated_at=DAY+'T10:00:00-05:00',
            target_window=dict(start='09:30',end=end),data_refs=[dict(path=self.capture,blob_sha=blob(self.raw))],missing=[])
        raw=encode(snap)
        pointer=dict(schema_version=1,snapshot_path=self.snapshot,snapshot_blob_sha=blob(raw),snapshot_id='synthetic',generated_at=snap['generated_at'])
        self.overlays={self.snapshot:raw,str(Path(self.snapshot).parent/'latest.json'):encode(pointer)}
        self.args.update(stage=stage,snapshot_path=self.snapshot,overlays=self.overlays)
    def test_exact_committed_capture_and_new_snapshot_are_both_summarized(self):
        result=build_candidate(**self.args);inv=json.loads(result['inventory_bytes'])
        self.assertEqual(inv['session_capture_summary'][0]['capture_count'],1)
        self.assertEqual(inv['stage_snapshot_summary'][0]['snapshot_id'],'synthetic')
        self.assertFalse(inv['market_fact_authority']);self.assertFalse(result['remote_authenticity_proven'])
        self.assertEqual(result['inventory_blob_sha'],blob(result['inventory_bytes']))
        self.assertEqual(result['input_blobs'][self.capture],blob(self.raw))
        self.assertFalse((self.store/'collector-state/market-data-inventory.json').exists())
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
    def test_dirty_worktree_does_not_change_frozen_input(self):
        self.write(self.capture,b'not json');self.write('sessions/untracked.json',b'{}')
        result=build_candidate(**self.args)
        self.assertEqual(result['input_blobs'][self.capture],blob(self.raw))
        self.assertEqual(json.loads(result['inventory_bytes'])['session_capture_summary'][0]['capture_count'],1)
    def test_wrong_head_rejected(self):
        self.args['confirmed_head']='b'*40
        with self.assertRaisesRegex(InventoryError,'HEAD'):build_candidate(**self.args)
    def test_unauthorized_overlay_rejected(self):
        self.overlays['collector-state/extra.json']=b'{}'
        with self.assertRaisesRegex(InventoryError,'overlay'):build_candidate(**self.args)
    def test_snapshot_ref_blob_mismatch_rejected(self):
        doc=json.loads(self.overlays[self.snapshot]);doc['data_refs'][0]['blob_sha']='0'*40
        self.overlays[self.snapshot]=encode(doc);latest=str(Path(self.snapshot).parent/'latest.json')
        pointer=json.loads(self.overlays[latest]);pointer['snapshot_blob_sha']=blob(self.overlays[self.snapshot]);self.overlays[latest]=encode(pointer)
        with self.assertRaisesRegex(InventoryError,'reference blob'):build_candidate(**self.args)
    def test_pointer_mismatch_rejected(self):
        latest=str(Path(self.snapshot).parent/'latest.json');doc=json.loads(self.overlays[latest]);doc['snapshot_blob_sha']='0'*40;self.overlays[latest]=encode(doc)
        with self.assertRaisesRegex(InventoryError,'snapshot/latest'):build_candidate(**self.args)
    def commit_extra(self):
        git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic extra source');self.args['confirmed_head']=git(self.repo,'rev-parse','HEAD')
    def test_committed_symlink_rejected(self):
        (self.store/'escape').symlink_to('/tmp');self.commit_extra()
        with self.assertRaisesRegex(InventoryError,'non-regular'):build_candidate(**self.args)
    def test_historical_snapshot_escape_rejected_before_builder(self):
        self.write('snapshots/2020-01/2020-01-01/open_15m/latest.json',encode(dict(snapshot_path='../../outside.json')));self.commit_extra()
        with self.assertRaisesRegex(InventoryError,'unsafe'):build_candidate(**self.args)
    def test_daily_shard_escape_rejected_before_builder(self):
        self.write('series/daily/provider/AAA/_index.json',encode(dict(shards=[dict(path='../../../../../outside.json')])));self.commit_extra()
        with self.assertRaisesRegex(InventoryError,'unsafe'):build_candidate(**self.args)
    def test_existing_malformed_session_skip_semantics_preserved(self):
        self.write('sessions/2026-11/2026-11-17/regular/0930-0945/twelve_data_basic/bad.json',b'bad');self.commit_extra()
        result=build_candidate(**self.args)
        self.assertEqual(json.loads(result['inventory_bytes'])['session_capture_summary'][0]['capture_count'],1)
    def test_open30_missing_proof_rejected(self):
        self.set_snapshot('open_30m','10:00')
        with self.assertRaisesRegex(InventoryError,'proof'):build_candidate(**self.args)
    def test_open30_existing_proof_verified_in_same_view(self):
        from build_deterministic_metric_proof import build_proof,_proof_pointer,canonical_bytes
        self.set_snapshot('open_30m','10:00')
        for p,raw in self.overlays.items():self.write(p,raw)
        proof,rel=build_proof(self.store,trade_date=DAY,stage='open_30m',data_plane_commit_sha='a'*40)
        raw=canonical_bytes(proof);self.overlays[rel]=raw
        self.overlays[f'proofs/deterministic-metrics/2026-11/{DAY}/open_30m/latest.json']=canonical_bytes(_proof_pointer(proof,rel=rel,blob=blob(raw)))
        result=build_candidate(**self.args)
        self.assertEqual(result['verified_proof_path'],rel)
    def test_git_commands_disable_transport_and_do_not_stage(self):
        original=subprocess.run
        def inspect(argv,*args,**kwargs):
            self.assertFalse(set(argv)&{'push','fetch','pull','clone','ls-remote','add','commit'})
            self.assertEqual(kwargs['env']['GIT_ALLOW_PROTOCOL'],'');self.assertEqual(kwargs['env']['GIT_NO_LAZY_FETCH'],'1')
            return original(argv,*args,**kwargs)
        with patch.object(subprocess,'run',side_effect=inspect):build_candidate(**self.args)
    def test_head_changes_during_build_rejected(self):
        import fixed_inventory_offline as m
        original=m.build_inventory
        def race(root):
            result=original(root)
            git(self.repo,'commit','--allow-empty','-qm','synthetic concurrent commit')
            return result
        with patch.object(m,'build_inventory',side_effect=race),self.assertRaisesRegex(InventoryError,'HEAD'):build_candidate(**self.args)
    def test_existing_inventory_schema_and_semantics_match_builder(self):
        from build_market_data_inventory import build_inventory
        for p,raw in self.overlays.items():self.write(p,raw)
        expected=build_inventory(self.store)
        actual=json.loads(build_candidate(**self.args)['inventory_bytes'])
        expected.pop('generated_at');actual.pop('generated_at')
        self.assertEqual(actual,expected)
    def test_missing_historical_snapshot_target_preserves_existing_status(self):
        self.write('snapshots/2020-01/2020-01-01/open_15m/latest.json',encode(dict(snapshot_path='snapshots/2020-01/2020-01-01/open_15m/missing.json')));self.commit_extra()
        result=build_candidate(**self.args)
        rows=json.loads(result['inventory_bytes'])['stage_snapshot_summary']
        self.assertEqual(rows[0]['status'],'missing_snapshot_target')
    def test_open60_existing_proof_verified_in_same_view(self):
        from build_deterministic_metric_proof import build_proof,_proof_pointer,canonical_bytes
        self.set_snapshot('open_60m','10:30')
        for p,raw in self.overlays.items():self.write(p,raw)
        proof,rel=build_proof(self.store,trade_date=DAY,stage='open_60m',data_plane_commit_sha='a'*40)
        raw=canonical_bytes(proof);self.overlays[rel]=raw
        self.overlays[f'proofs/deterministic-metrics/2026-11/{DAY}/open_60m/latest.json']=canonical_bytes(_proof_pointer(proof,rel=rel,blob=blob(raw)))
        result=build_candidate(**self.args)
        self.assertEqual(result['verified_proof_path'],rel)
    def test_immutable_snapshot_conflict_rejected(self):
        old=json.loads(self.overlays[self.snapshot]);old['generated_at']=DAY+'T09:59:00-05:00'
        self.write(self.snapshot,encode(old));self.commit_extra()
        with self.assertRaisesRegex(InventoryError,'immutable snapshot'):build_candidate(**self.args)
    def test_immutable_proof_conflict_rejected(self):
        self.test_open30_existing_proof_verified_in_same_view()
        proof_path=next(p for p in self.overlays if p.startswith('proofs/') and not p.endswith('latest.json'))
        old=json.loads(self.overlays[proof_path]);old['formula_contract']='synthetic old conflict'
        self.write(proof_path,encode(old));self.commit_extra()
        with self.assertRaisesRegex(InventoryError,'immutable proof'):build_candidate(**self.args)
    def old_latest(self,generated_at):
        old=json.loads(self.overlays[self.snapshot]);old.update(snapshot_id='other',generated_at=generated_at)
        path=str(Path(self.snapshot).parent/'other.json');raw=encode(old)
        pointer=encode(dict(schema_version=1,snapshot_id='other',snapshot_path=path,snapshot_blob_sha=blob(raw),generated_at=generated_at))
        latest=str(Path(self.snapshot).parent/'latest.json')
        self.write(path,raw);self.write(latest,pointer);self.commit_extra()
        return latest,pointer
    def test_latest_rollback_overlay_rejected(self):
        self.old_latest(DAY+'T10:01:00-05:00')
        with self.assertRaisesRegex(InventoryError,'latest must remain'):build_candidate(**self.args)
    def test_older_snapshot_can_keep_existing_latest(self):
        latest,pointer=self.old_latest(DAY+'T10:01:00-05:00');self.overlays[latest]=pointer
        result=build_candidate(**self.args)
        self.assertEqual(json.loads(result['inventory_bytes'])['stage_snapshot_summary'][0]['snapshot_id'],'other')
        self.assertIn(self.snapshot,result['input_blobs'])
    def test_equal_time_different_snapshot_identity_rejected(self):
        self.old_latest(DAY+'T10:00:00-05:00')
        with self.assertRaisesRegex(InventoryError,'share generated_at'):build_candidate(**self.args)

    def test_same_snapshot_replay_preserves_head_bytes(self):
        for p,raw in self.overlays.items():self.write(p,raw)
        self.commit_extra();original=self.overlays[self.snapshot]
        self.overlays[self.snapshot]=json.dumps(json.loads(original),indent=2).encode()
        result=build_candidate(**self.args)
        self.assertEqual(result['input_blobs'][self.snapshot],blob(original))
    def test_nonadvancing_behavior_matches_original_snapshot_writer(self):
        from market_data_store import write_snapshot
        latest,pointer=self.old_latest(DAY+'T10:01:00-05:00')
        snap=json.loads(self.overlays[self.snapshot]);kwargs={k:snap[k] for k in ('stage','trade_date','snapshot_id','generated_at','data_refs','coverage','missing','target_window','actual_data_cutoff')}
        path,advanced=write_snapshot(self.store,**kwargs)
        self.assertEqual(path,self.snapshot);self.assertFalse(advanced)
        self.assertEqual((self.store/latest).read_bytes(),pointer)
        self.overlays[latest]=pointer
        result=build_candidate(**self.args)
        self.assertEqual(result['input_blobs'][latest],blob(pointer))
