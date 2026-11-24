"""Synthetic/offline registration of a separately scoped no-provider role."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import private_authority_input as authority
from scripts import private_authority_receipt as receipts
from scripts import private_authority_receipt_transport as transport

ROUTE="ordinary_retained_price_tail"
SCOPE={"TEST1":["2041-06-03"],"TEST2":["2041-06-03","2041-06-04"]}
OTHER_SCOPE={"TEST8":["2041-06-04"]}
COMPUTE="a"*40

def enc(v): return (json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=True)+"\n").encode()
def sha(b): return hashlib.sha256(b).hexdigest()


class Registration(unittest.TestCase):
    def test_existing_quiet_reader_consumes_one_namespaced_receipt(self):
        raw=receipts.canonical_receipt(dict(schema_version=1,kind="capture_authority_receipt",route=ROUTE,
            compute_sha=COMPUTE,authority_store_sha="1"*40,descriptor_commit="2"*40,nonce="3"*64))
        commitment=receipts.receipt_commitment(raw)
        path=transport.receipt_sidecar_ref(ROUTE,COMPUTE,commitment)
        base="https://api.github.com/repos/"+transport.STORE_REPOSITORY
        url=base+"/contents/"+transport.RECEIPT_PATH+"?ref="+transport.urllib.parse.quote(path,safe="")
        response_urls=[]
        class Response(io.BytesIO):
            status=200
            def geturl(self): return self.url
        class Opener:
            def open(self,req,timeout):
                got=req.full_url
                response_urls.append(got)
                if got==base:
                    value=dict(private=True,full_name=transport.STORE_REPOSITORY)
                elif got==url:
                    oid=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
                    value=dict(path=transport.RECEIPT_PATH,encoding="base64",type="file",size=len(raw),sha=oid,
                               content=base64.b64encode(raw).decode())
                else:
                    raise AssertionError("unexpected synthetic endpoint")
                result=Response(json.dumps(value).encode());result.url=got
                return result
        with patch.object(transport.urllib.request,"build_opener",return_value=Opener()):
            got=transport.fetch_private_receipt("DUMMY_FIXTURE_TOKEN",ROUTE,COMPUTE,commitment)
        self.assertEqual(got,raw)
        self.assertEqual(response_urls,[base,url])
        for role in ("soxx_capture_only","eleven_symbol_conditional_append"):
            old=receipts.canonical_receipt(dict(schema_version=1,kind="capture_authority_receipt",route=role,
                  compute_sha=COMPUTE,authority_store_sha="1"*40,descriptor_commit="2"*40,nonce="3"*64))
            receipt=receipts.verify_receipt(old,receipts.receipt_commitment(old),role,COMPUTE)
            self.assertFalse(receipt.production_activation)
            self.assertEqual(transport.receipt_sidecar_ref(role,COMPUTE,commitment),
                             f"refs/heads/capture-receipts/{role}/{COMPUTE}")

    def test_exact_distinct_immutable_selections_without_authorization(self):
        self.assertIn(ROUTE, receipts.ROUTES)
        self.assertNotIn(ROUTE, authority.ROUTES) # existing provider dispatcher remains closed
        self.assertTrue(callable(getattr(authority,"load_ordinary_retained_selection",None)))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            env={"PATH":os.defpath,"HOME":d,"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null",
                 "GIT_ALLOW_PROTOCOL":"","GIT_AUTHOR_NAME":"fixture","GIT_AUTHOR_EMAIL":"fixture@example.invalid",
                 "GIT_COMMITTER_NAME":"fixture","GIT_COMMITTER_EMAIL":"fixture@example.invalid"}
            def git(*args,raw=b""):
                return subprocess.check_output(["git","-C",d,*args],input=raw,env=env,
                                               stderr=subprocess.DEVNULL).decode().strip()
            def obj(kind, data): return git("hash-object","-w","-t",kind,"--stdin",raw=data)
            def blob(data): return obj("blob",data)
            def tree(rec): return git("mktree",raw=rec.encode())
            git("init","-q","-b","main")
            marker=blob(b"synthetic repository marker\n")
            base_tree=tree(f"100644 blob {marker}\tmarker\n")
            base=git("commit-tree",base_tree,raw=b"synthetic base\n")
            git("update-ref","refs/heads/main",base)
            def make(nonce, overrides=None, create_ref=True, scope=SCOPE):
                review=dict(schema_version=1,mode="retained_price_tail_draft",base=base,source="b"*40,
                    source_input="c"*40,compute="d"*40,run="123-1",observed_at="2041-06-05T23:35:01Z",
                    not_before="2041-06-05T12:00:00Z",execution_day="2041-06-05",
                    manifest_path="collector-state/2041-06/2041-06-05-previous_session_eod-"+"6"*16+"-publication.json",manifest_blob="e"*40,
                    targets={s:dict(dates=days,index_blob="9"*40,tail="2041-06-01",packet_path="collector-state/2041-06/price-only-"+"4"*64+".json",
                           packet_blob="8"*40,rows_sha256="7"*64,conflict_fields=[]) for s,days in scope.items()})
                approval=dict(schema_version=1,mode=ROUTE,compute_sha=COMPUTE,expected_store_sha=base,
                    execution_day="2041-06-06",valid_from="2041-06-06T10:00:00Z",valid_until="2041-06-06T10:30:00Z",
                    request_limit=0,credit_limit=0,max_attempts=1,
                    write_policy="append_ohlc_only_preserve_history_volume_and_budget",
                    review=review,review_sha256=sha(enc(review)[:-1]))
                if overrides: overrides(approval)
                approval["review_sha256"]=sha(enc(review)[:-1])
                desc=dict(schema_version=1,kind="capture_authority_selection",route=ROUTE,compute_sha=COMPUTE,
                          authority_store_sha=base,approval_sha256=sha(enc(approval)),approval=approval)
                sub=tree(f"100644 blob {blob(enc(desc))}\tselection.json\n")
                root_tree=tree(f"040000 tree {sub}\tcapture-authority\n100644 blob {marker}\tmarker\n")
                desc_commit=git("commit-tree",root_tree,"-p",base,raw=b"synthetic private selection\n")
                if create_ref:
                    git("update-ref",f"refs/heads/capture-authority/{ROUTE}/{COMPUTE}/{nonce}",desc_commit)
                raw=receipts.canonical_receipt(dict(schema_version=1,kind="capture_authority_receipt",route=ROUTE,
                   compute_sha=COMPUTE,authority_store_sha=base,descriptor_commit=desc_commit,nonce=nonce))
                return raw, receipts.receipt_commitment(raw), approval["review_sha256"]
            def select(raw, commitment, private_scope):
                return authority.load_ordinary_retained_selection(root,raw,commitment,COMPUTE,
                    expected_review_sha256=private_scope)
            a, b=make("1"*64),make("2"*64,scope=OTHER_SCOPE)
            self.assertNotEqual(a[1],b[1])
            self.assertNotEqual(transport.receipt_sidecar_ref(ROUTE,COMPUTE,a[1]),
                                transport.receipt_sidecar_ref(ROUTE,COMPUTE,b[1]))
            self.assertEqual(transport.receipt_sidecar_ref("soxx_capture_only",COMPUTE,a[1]),
                             f"refs/heads/capture-receipts/soxx_capture_only/{COMPUTE}")
            for v in (a,b):
                selected=select(*v)
                self.assertFalse(selected.authorization_authenticated)
                self.assertFalse(selected.production_activation)
            unapproved=make("5"*64,scope={"TEST9":["2041-06-04"]})
            for v in ((a[0],b[1],a[2]), (b[0],b[1],a[2]), (unapproved[0],unapproved[1],a[2]),
                      make("3"*64,create_ref=False), make("4"*64,lambda x:x.update(request_limit=1)),
                      make("6"*64,scope={"TEST1":["2041-06-06"]}),
                      make("7"*64,lambda x:x.update(expected_store_sha="f"*40)),
                      make("8"*64,scope={"TEST1":["2041-06-03"]*2}),
                      make("9"*64,lambda x:x["review"].update(run="x")),
                      make("a"*64,lambda x:x["review"].update(observed_at="2041-06-05T12:00:00+00:60")),
                      make("b"*64,lambda x:x["review"].update(manifest_path="data/other.json")),
                      make("c"*64,lambda x:x["review"]["targets"]["TEST1"].update(conflict_fields=["other"])),):

                with self.assertRaises(authority.PrivateAuthorityError): select(*v)


if __name__=="__main__":unittest.main()
