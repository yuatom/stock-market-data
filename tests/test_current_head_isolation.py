"""No remote writes: synthetic Store drift with exact scoped history proof."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_private_profile import fixture
import soxx_appendix_exact_preflight as P

class CurrentHead(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        self.g('init','-b','main')
        self.g('config','user.name','Fixture')
        self.g('config','user.email','fixture@example.invalid')
        self.plan=fixture()
        self.active='data/market-data/series-active/daily/twelve_data_basic/SOXX.json'
        self.admission_dir='data/market-data/series-admissions/daily/twelve_data_basic/SOXX/'
        self.envelope='capture-evidence/fixture/source.json'
        self.terminal='capture-evidence/fixture/terminal.json'
        self.w(self.envelope,{'kind':'fixture envelope'})
        self.w(self.terminal,{'kind':'fixture terminal'})
        n=self.plan['native_path']
        self.w(n,{'records':[{'datetime':'2035-01-14'}]})
        self.plan['native_blob']=self.g('hash-object',n)
        def ref(p): return dict(path=p,blob_sha=self.g('hash-object',p))
        obj=dict(envelope_ref=ref(self.envelope),workflow_evidence={'terminal_ref':ref(self.terminal)})
        raw=json.dumps(obj).encode()
        a=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
        self.w(self.admission_dir+a+'.json',obj)
        self.plan['native_admission_blob']=a
        self.w(self.active,dict(symbol='SOXX',provider='twelve_data_basic',basis_id=n.split('/')[-2],
                                snapshot_blob_sha=self.plan['native_blob'],admission_blob_sha=a))
        self.plan['native_selector_blob']=self.g('hash-object',self.active)
        self.inv='data/market-data/collector-state/market-data-inventory.json'
        self.w(self.inv,self.inventory([]))
        self.plan['evidence_base']=self.commit('historical evidence')
        self.plan.update(schema_version=3,frozen_appendix_commit='c'*40)

    def inventory(self,series):
        return dict(schema_version=1, generated_at='fixture',market_fact_authority=False,purpose='fixture',
                    session_capture_summary=[],stage_snapshot_summary=[],extended_hours_probe_summary={},
                    daily_series_summary=dict(series_count=len(series),provider_series_counts={},series=series))

    def g(self,*a):
        return subprocess.check_output(['git','-C',str(self.root),*a],stderr=subprocess.DEVNULL,text=True).strip()
    def w(self,p,obj):
        f=self.root/p
        f.parent.mkdir(parents=True,exist_ok=True)
        f.write_text(json.dumps(obj))
    def commit(self,msg):
        self.g('add','.')
        self.g('commit','-m',msg)
        return self.g('rev-parse','HEAD')
    def evaluate(self,base):
        self.plan.update(base=base,candidate_parents=[base],advance={
            'kind':'exact_durable_current_head_isolation','tree':self.g('rev-parse',base+'^{tree}')})
        parsed=P.load_profile(json.dumps(self.plan).encode())
        P._verify_plan_advance(self.root,parsed)
        return parsed
    def test_distinct_unrelated_changes_need_no_writer_source_change(self):
        unrelated=(
            'collector-state/fixture/EOD.json',
            'collector-state/probes/fixture/probe.json',
            'snapshots/fixture/premarket/latest.json',
            'proofs/fixture/other.json',
            'sessions/fixture/regular/other-symbol.json',
        )
        for i,path in enumerate(unrelated):
            # Unknown producer/manifest-shaped names are opaque unrelated Store bytes,
            # not qualification of a terminal or path-based authority.
            self.w('data/market-data/'+path,{'opaque':i})
            inv=self.inventory([])
            inv['session_capture_summary']=[{'opaque_fixture':i}]
            self.w(self.inv,inv)
            base=self.commit('other independently durable history')
        self.assertEqual(self.evaluate(base).base,base)
    def test_native_aba_is_rejected(self):
        old=(self.root/self.envelope).read_bytes()
        self.w(self.envelope,{'changed':1})
        self.commit('change protected bytes')
        (self.root/self.envelope).write_bytes(old)
        base=self.commit('restore same protected bytes')
        with self.assertRaisesRegex(ValueError,'ISOLATED_PROTECTED_HISTORY_CHANGED'):
            self.evaluate(base)
    def test_occupied_appendix_path_is_rejected(self):
        self.w(next(iter(self.plan['outputs'])),{'unexpected overlap':1})
        base=self.commit('unrelated writer enters protected scope')
        with self.assertRaisesRegex(ValueError,'ISOLATED_APPENDIX_PATH_OCCUPIED'):
            self.evaluate(base)
    def test_divergent_base_is_rejected(self):
        self.g('checkout','--orphan','unrelated')
        self.w('foreign.json',{'unrelated':1})
        base=self.commit('different root')
        with self.assertRaisesRegex(ValueError,'STORE_LOCAL_OBJECT_UNREADABLE'):
            self.evaluate(base)

    def test_unknown_inventory_supplement_fails_even_if_target_path_absent(self):
        self.w(self.inv,self.inventory([{'symbol':'SOXX','index_path':'unresolved-index'}]))
        base=self.commit('uncertain supplement inventory')
        with self.assertRaisesRegex(ValueError,'ISOLATED_DAILY_TARGET_OR_UNCERTAIN_INDEX'):
            self.evaluate(base)

    def test_merge_is_ambiguous_even_with_no_native_changes(self):
        self.g('checkout','-b','other')
        self.w('unrelated-a.json',{'some':'bytes'})
        self.commit('side history')
        self.g('checkout','main')
        self.w('unrelated-b.json',{'some':'bytes'})
        self.commit('main history')
        self.g('merge','--no-ff','-m','merge other', 'other')
        with self.assertRaisesRegex(ValueError,'ISOLATED_NONLINEAR_HISTORY'):
            self.evaluate(self.g('rev-parse','HEAD'))

    def test_transient_supplement_index_is_not_ignored(self):
        self.w(self.inv,self.inventory([{'symbol':'SOXX','index_path':'unresolved-index'}]))
        self.commit('uncertain supplement inventory')
        self.w(self.inv,self.inventory([]))
        with self.assertRaisesRegex(ValueError,'ISOLATED_DAILY_TARGET_OR_UNCERTAIN_INDEX'):
            self.evaluate(self.commit('restore inventory'))

    def test_stale_head_stops_before_post(self):
        import publish_soxx_web_appendix as W
        from unittest.mock import patch
        self.w('other.json',{})
        plan=self.evaluate(self.commit('valid current tree fixture'))
        results=iter([b'7'*40,b'',b'7'*40+b'\trefs/heads/main',b'9'*40+b'\trefs/heads/main'])
        with patch.object(W,'_git',side_effect=lambda *args:next(results)),patch.object(W,'_graphql_one_post') as post:
            with self.assertRaisesRegex(ValueError,'EXACT_SOURCE_OR_STORE_BASE_DRIFT'):
                W._require_local_and_remote(self.root,self.root,'7'*40,plan)
        post.assert_not_called()

    def test_generic_leaf_under_soxx_named_recovery_sibling_cannot_aba(self):
        path='data/market-data/recovery/fixture/sibling-SoXx-repair/bytes.json'
        self.w(path,{'otherwise':'unindexed'})
        self.commit('unindexed SOXX recovery generic leaf')
        (self.root/path).unlink()
        latest=self.commit('restore tree before release')
        with self.assertRaisesRegex(ValueError,'ISOLATED_PROTECTED_HISTORY_CHANGED'):
            self.evaluate(latest)
