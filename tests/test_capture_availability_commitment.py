"""Local-only native Verify CLI and synthetic bare Store; no network."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import sys
import tempfile
import unittest

OWNER = Path(__file__).resolve().parents[1] / "scripts/verify_store_publication.py"
DAY = "2001-01-08"
REQUEST = "synthetic-opaque-pre-request"
SNAP = f"snapshots/2001-01/{DAY}/premarket/premarket-061500-et.json"
MANIFEST = (f"data/market-data/collector-state/2001-01/{DAY}-premarket-"
            + hashlib.sha256(REQUEST.encode()).hexdigest()[:16] + "-publication.json")
TAG = "refs/tags/market-data-read/"


class AvailabilityCommitmentTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo, self.remote = root/"store", root/"remote.git"
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_"))}
        self.env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="file",
                        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        self.host = dict(GITHUB_ACTIONS="true", GITHUB_REPOSITORY="yuatom/stock-market-data",
                         GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/collector-requests",
                         GITHUB_SHA="1"*40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1",
                         GITHUB_JOB="collect")
        self.git("init", "-q", "--bare", str(self.remote), in_repo=False)
        self.git("init", "-q", "-b", "main", str(self.repo), in_repo=False)
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo/"data/market-data").mkdir(parents=True)
        (self.repo/"data/market-data/base.json").write_text("{}\n")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic baseline")
        self.compute=root/"compute"
        self.owner=self.compute/"scripts/verify_store_publication.py"
        self.owner.parent.mkdir(parents=True)
        for filename in ("verify_store_publication.py","push_proof_store_commit.py"):
            shutil.copyfile(OWNER.parent/filename,self.owner.parent/filename)
        self.git("init","-q","-b","main",str(self.compute),in_repo=False)
        self.git("-C",str(self.compute),"add",".",in_repo=False)
        self.git("-C",str(self.compute),"commit","-qm","synthetic compute checkout",in_repo=False)
        self.compute_sha=self.git("-C",str(self.compute),"rev-parse","HEAD",in_repo=False)


    def git(self, *args, in_repo=True):
        prefix=("git","-C",str(self.repo)) if in_repo else ("git",)
        return subprocess.check_output((*prefix,*args), env=self.env, stderr=subprocess.PIPE).decode().strip()

    def write(self, rel, obj):
        file=self.repo/rel
        file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text(json.dumps(obj,separators=(",",":"),sort_keys=True)+"\n")
        return self.git("hash-object", rel)

    def publish(self, *, bad=False, snapshot=SNAP, with_manifest=True):
        digest=self.write("data/market-data/"+snapshot, {"synthetic":True})
        if with_manifest:
            self.write(MANIFEST, dict(schema_version=1,request_id=REQUEST,trade_date=DAY,stage="premarket",
                         required_outputs=[dict(path=snapshot,blob_sha="0"*40 if bad else digest)]))
        self.git("add", ".")
        self.git("commit","-qm","synthetic stage")
        self.git("push","-q","origin","HEAD:main")
        return self.git("rev-parse","HEAD")

    def verify(self, commit, *, enabled=True, manifest=MANIFEST, status="store_persisted", **host):
        argv=[sys.executable,"-B",str(self.owner),"--store-root",str(self.repo),
              "--expected-commit",commit,"--writer-status",status]
        if manifest is not None:argv+=["--required-manifest",manifest]
        if enabled:argv+=["--emit-capture-availability-commitment"]
        return subprocess.run(argv,env=dict(self.env,**{**self.host,**host}),
                              text=True,capture_output=True,timeout=30)

    def test_only_opaque_causal_line_after_exact_durable_verify(self):
        commit=self.publish()
        one=self.verify(commit)
        self.assertEqual((one.returncode,one.stderr),(0,""))
        self.assertRegex(one.stdout,r"\ACAPTURE_AVAILABILITY_V2 [0-9a-f]{64}\n\Z")
        for private in (commit,REQUEST,MANIFEST,SNAP,TAG):
            self.assertNotIn(private,one.stdout)
        self.assertEqual(self.git("ls-remote","origin",TAG+commit).split()[0],commit)
        manifest_raw=(self.repo/MANIFEST).read_bytes()
        private_tuple={
            "compute_checkout":self.compute_sha,
            "manifest_blob":self.git("rev-parse",commit+":"+MANIFEST),
            "manifest_path":MANIFEST,"manifest_sha256":hashlib.sha256(manifest_raw).hexdigest(),
            "request_id":REQUEST,"retention_ref":TAG+commit,
            "store_commit":commit,"snapshot":{
                "path":"data/market-data/"+SNAP,
                "blob_sha":self.git("rev-parse",commit+":data/market-data/"+SNAP)},
            "workflow_attempt":{k:v for k,v in self.host.items() if k != "GITHUB_ACTIONS"},
        }
        def marker(value):
            normalized=json.dumps(value,sort_keys=True,separators=(",",":")).encode("ascii")
            value=hashlib.sha256(b"stock-market-data/capture-availability/v2\0"+normalized).hexdigest()
            return "CAPTURE_AVAILABILITY_V2 "+value+"\n"
        self.assertEqual(marker(private_tuple),one.stdout)
        for field in ("compute_checkout","manifest_blob","manifest_sha256","store_commit","retention_ref","request_id"):
            self.assertNotEqual(marker({**private_tuple,field:"synthetic tamper"}),one.stdout)
        self.assertEqual(self.verify(commit).stdout,one.stdout)
        self.assertNotEqual(self.verify(commit,GITHUB_RUN_ATTEMPT="2").stdout,one.stdout)
        self.assertNotEqual(self.verify(commit,GITHUB_SHA="2"*40).stdout,one.stdout)

    def test_off_or_unproven_conditions_never_emit(self):
        commit=self.publish()
        off=self.verify(commit,enabled=False)
        self.assertEqual(off.returncode,0)
        self.assertEqual(json.loads(off.stdout)["status"],"durability_verified")
        self.assertNotIn("CAPTURE_AVAILABILITY",off.stdout)
        for kwargs in ({"manifest":None},{"status":"unknown"},
                       {"GITHUB_EVENT_NAME":"schedule"},{"GITHUB_RUN_ATTEMPT":""}):
            with self.subTest(kwargs=kwargs):
                result=self.verify(commit,**kwargs)
                self.assertEqual((result.returncode,result.stdout,result.stderr),(0,"",""))

    def test_modified_compute_checkout_has_no_commitment(self):
        commit=self.publish()
        with self.owner.open("a") as stream:
            stream.write("\n# uncommitted synthetic change\n")
        result=self.verify(commit)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,"",""))

    def test_native_failed_verify_never_emits(self):
        commit=self.publish(bad=True)
        result=self.verify(commit)
        self.assertEqual((result.returncode,result.stdout),(1,""))
        self.assertNotIn("CAPTURE_AVAILABILITY",result.stderr)
        self.assertNotIn(REQUEST,result.stderr)

    def test_latest_alias_only_is_not_capture_availability(self):
        commit=self.publish(snapshot=f"snapshots/2001-01/{DAY}/premarket/latest.json")
        result=self.verify(commit)
        self.assertEqual((result.returncode,result.stdout),(0,""))


if __name__ == "__main__":
    unittest.main()
