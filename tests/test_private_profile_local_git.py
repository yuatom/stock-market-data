import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_private_profile import fixture
import publish_soxx_web_appendix as W
import soxx_appendix_exact_preflight as P

class RealLocalPublication(unittest.TestCase):
    def test_private_plan_same_history_and_durable_new_commit_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            store,remote,compute=(root/n for n in ('store','origin.git','compute'))
            for x in (store,remote,compute): x.mkdir()
            def git(repo,*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
            def sha_commit(msg):
                git(store,'add','.')
                git(store,'commit','-m',msg)
                return git(store,'rev-parse','HEAD')
            def write(path,raw):
                f=store/path
                f.parent.mkdir(parents=True,exist_ok=True)
                f.write_bytes(raw)
            git(remote,'init','--bare','-b','main')
            git(store,'init','-b','main')
            git(store,'config','user.name','Fixture')
            git(store,'config','user.email','fixture@example.invalid')
            git(store,'remote','add','origin',str(remote))
            doc=fixture()
            write(doc['native_path'],b'{ synthetic native basis }\n')
            base=sha_commit('Fixture evidence')
            git(store,'push','origin','main',base+':refs/tags/market-data-read/'+base)
            git(compute,'clone',str(remote),str(compute))
            doc['evidence_base']=doc['base']=base
            doc['candidate_parents']=[base]
            doc['native_blob']=git(store,'rev-parse',base+':'+doc['native_path'])
            appendix={
                'kind':'soxx_public_web_prior_close_appendix','purpose':'prior_close_only',
                'base_store_commit':base,'base_snapshot_blob':doc['native_blob'],
                'provider_native_receipt':False,'market_qualified':False,'production_activation':False,
                'volume_policy':'BLOCKED_UNRESOLVED','bars':[dict(trade_date=d,open='1.00',high='1.00',low='1.00',close='1.00')
                                                           for d in doc['trade_dates']],
            }
            payloads={}
            for path,row in doc['outputs'].items():
                if path==doc['manifest']:
                    continue
                raw=json.dumps(appendix).encode() if path.endswith('/manifest.json') else b'{ synthetic source }\n'
                write(path, raw)
                doc['outputs'][path]['sha256']=hashlib.sha256(raw).hexdigest()
                doc['outputs'][path]['blob']=git(store,'hash-object',path)
                payloads[path]=raw
            write(doc['manifest'],json.dumps(dict(schema_version=1,request_id='fixture',trade_date=doc['trade_dates'][-1],
                    stage='soxx_public_web_price_appendix',required_outputs=[
                       dict(path=path.removeprefix('data/market-data/'),blob_sha=doc['outputs'][path]['blob'])
                       for path in sorted(payloads)])).encode())
            doc['outputs'][doc['manifest']]['blob']=git(store,'hash-object',doc['manifest'])
            doc['head']=sha_commit('Fixture append candidate')
            doc['tree']=git(store,'rev-parse',doc['head']+'^{tree}')
            plan=P.load_profile(json.dumps(doc).encode())

            def mock_cas(token,mutation):
                inp=mutation['variables']['input']
                self.assertEqual(inp['expectedHeadOid'],git(remote,'rev-parse','main'))
                oid=git(store,'commit-tree',plan.tree,'-p',base,'-m','GraphQL fixture publish')
                git(store,'push','origin',oid+':refs/heads/main')
                return {'data':{'createCommitOnBranch':{'clientMutationId':inp['clientMutationId'],'commit':{'oid':oid}}}}

            with patch.object(W,'_require_origins'),patch.object(W,'_graphql_one_post',side_effect=mock_cas) as cas:
                receipt=W._publish_checked(compute,store,base,'321','token',plan)
            cas.assert_called_once()
            oid=receipt['store_commit_sha']
            self.assertNotEqual(oid,plan.head)
            self.assertEqual(git(remote,'rev-parse',receipt['durability_ref']),oid)
            self.assertEqual(git(remote,'rev-parse',oid+'^{tree}'),plan.tree)
            self.assertEqual(git(remote,'rev-parse',oid+':'+plan.native_path),plan.native_blob)
