import copy, hashlib, json, unittest
import deployment_gate as gate

PATHS = ('scripts/validate_deterministic_metrics.py', 'config/calculation-policy.yaml', 'tests/test_deterministic_metric_finalization.py')
def fixture():
    files = {p: ('synthetic:'+p).encode() for p in PATHS}
    report = b'{"green":{"passed":42,"failed":0}}'
    manifest = dict(schema='consumer-candidate-offline-v1', provenance='local-synthetic',
        expected_consumer_commit='a'*40, lineage_kind='twelve_symbol_conditional_append',
        files={p:hashlib.sha256(b).hexdigest() for p,b in files.items()},
        verification_artifact_sha256=hashlib.sha256(report).hexdigest())
    return manifest, files, report

class GateTests(unittest.TestCase):
    def check(self, m, f, r, consistent):
        before = copy.deepcopy((m,f,r))
        out = gate.validate_manifest(m,f,r)
        self.assertEqual(out['local_manifest_consistent'], consistent)
        self.assertIs(out['production_ready'], False)
        self.assertEqual(out['deployment_status'], 'BLOCKED')
        self.assertIsNone(out['verified_effective_consumer_commit'])
        self.assertIn('effective_runtime_resolver_not_implemented', out['blockers'])
        self.assertIn('commit_tree_membership_not_verified', out['blockers'])
        self.assertEqual((m,f,r), before)
    def test_consistent_synthetic_never_deployable(self):
        self.check(*fixture(), True)
    def test_reject_mutations(self):
        mutations = [
            ('schema','other'), ('provenance','github-attested'),
            ('expected_consumer_commit','b7e288f'), ('expected_consumer_commit',None),
            ('expected_consumer_commit',True), ('expected_consumer_commit','A'*40),
            ('lineage_kind','eleven_symbol_conditional_append'),
            ('verification_artifact_sha256','0'*64),
            ('production_ready',True), ('effective_sha','a'*40),
            ('runtime_attestation',{'passed':True}), ('lineage_supported',True),
        ]
        for k,v in mutations:
            with self.subTest(k=k,v=v):
                m,f,r=fixture(); m[k]=v; self.check(m,f,r,False)
    def test_missing_fields(self):
        for k in fixture()[0]:
            with self.subTest(k=k):
                m,f,r=fixture(); del m[k]; self.check(m,f,r,False)
    def test_files_drift_and_scope(self):
        for action in ('changed','missing','extra','wrongtype','digesttype'):
            with self.subTest(action=action):
                m,f,r=fixture()
                if action=='changed': f[PATHS[0]]=b'other'
                if action=='missing': del f[PATHS[0]]
                if action=='extra': f['other']=b'x'
                if action=='wrongtype': f[PATHS[0]]='not bytes'
                if action=='digesttype': m['files'][PATHS[0]]=True
                self.check(m,f,r,False)
    def test_manifest_file_scope(self):
        for value in ([],{},dict(fixture()[0]['files'], other='a'*64)):
            m,f,r=fixture(); m['files']=value; self.check(m,f,r,False)
    def test_artifact_raw_bytes_required_and_exact(self):
        for r in (b'changed', '{"passed":true}', None, b''):
            m,f,_=fixture(); self.check(m,f,r,False)
    def test_non_dict_input(self):
        _,f,r=fixture()
        for m in (None,[],True): self.check(m,f,r,False)
    def test_digest_is_not_authenticity(self):
        m,f,r=fixture(); r=b'{"passed":true,"effective_version":"invented"}'
        m['verification_artifact_sha256']=hashlib.sha256(r).hexdigest()
        self.check(m,f,r,True)
if __name__=='__main__': unittest.main()
