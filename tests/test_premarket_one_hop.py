"""Exercise the exact private premarket advance verifier against actual local Git."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import soxx_appendix_exact_preflight as P
from test_private_profile import fixture

class PremarketOneHop(unittest.TestCase):
    def build(self, actual_date, declared_date='2035-01-25'):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        def git(*args):
            return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()
        def write(path,obj):
            f=root/path
            f.parent.mkdir(parents=True,exist_ok=True)
            f.write_bytes(json.dumps(obj).encode())
        def commit(msg):
            git('add','.')
            git('commit','-m',msg)
            return git('rev-parse','HEAD')
        git('init','-b','main')
        git('config','user.name','Fixture')
        git('config','user.email','fixture@example.invalid')
        doc=fixture()
        inv='data/market-data/collector-state/market-data-inventory.json'
        write(doc['native_path'],{'synthetic_native':True})
        write(inv,{'old':True})
        doc['evidence_base']=commit('Historical evidence')
        doc['native_blob']=git('rev-parse',doc['evidence_base']+':'+doc['native_path'])
        syn=f'data/market-data/sessions/2035-01/{declared_date}/premarket/0400-0930/nasdaq_public_extended/premarket-test-073500-et.json'
        snap=f'data/market-data/snapshots/2035-01/{declared_date}/premarket/latest.json'
        pub=f'data/market-data/collector-state/2035-01/{declared_date}-premarket-ffeeddccbbaa1234-publication.json'
        paths={inv:'M',syn:'A',snap:'A',pub:'A'}
        for path in (inv,syn,snap): write(path,{'new_contents':True})
        git('add','.')
        out=[dict(path=p.removeprefix('data/market-data/'),blob_sha=git('rev-parse',':'+p)) for p in (inv,syn,snap)]
        write(pub,dict(request_id='synthetic',trade_date=actual_date,stage='premarket',schema_version=1,required_outputs=out))
        doc['base']=commit('Approved premarket outputs; no native changes')
        doc['candidate_parents']=[doc['base']]
        doc['advance']=dict(tree=git('rev-parse',doc['base']+'^{tree}'),trade_date=declared_date,manifest=pub,
                            paths={p:{'status':flag,'blob':git('rev-parse',doc['base']+':'+p)} for p,flag in paths.items()})
        return root,P.load_profile(json.dumps(doc).encode())

    def verify(self,root,plan):
        P._verify_base_advance(root,plan.evidence_base,plan.native_blob,plan.base,plan.advance,plan.native_path)

    def test_matching_one_hop_and_manifest_pass(self):
        self.verify(*self.build('2035-01-25'))

    def test_wrong_manifest_date_rejected_with_scope_reason(self):
        with self.assertRaisesRegex(ValueError,'STORE_PREMARKET_CLOSURE_SCOPE'):
            self.verify(*self.build('2035-01-26'))
