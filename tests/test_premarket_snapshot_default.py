from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from market_data_store import write_snapshot, git_blob_sha_bytes
from build_market_data_inventory import build_inventory
from build_store_publication_manifest import build_manifest
import json, subprocess, unittest
DAY="2026-09-21"
ALL="AMD META MU NVDA ORCL PLTR QQQ SPCX SPY TME TSLA TSM".split()
SIX="META ORCL PLTR TME TSLA TSM".split()

def emit(root, time, symbols, universe=ALL, stage="premarket"):
    sid=f"{stage}-{time.replace(':','')}-et"
    refs=[]
    for symbol in symbols:
        rel=f"fixture-captures/{sid}/{symbol}.json"
        p=root/rel
        p.parent.mkdir(parents=True, exist_ok=True)
        payload=(json.dumps({"symbol":symbol})+"\n").encode()
        p.write_bytes(payload)
        refs.append(dict(symbol=symbol,kind="premarket_trade_capture",provider="nasdaq_public_extended",
                         path=rel,blob_sha=git_blob_sha_bytes(payload),window="04:00-09:30"))
    missing=sorted(set(universe)-set(symbols))
    rel,promoted=write_snapshot(root,stage=stage,trade_date=DAY,snapshot_id=sid,generated_at=f"{DAY}T{time}-05:00",
        actual_data_cutoff=f"{DAY}T{min(time,'09:29:59')}-05:00",
        target_window={"start":"04:00","end":"09:30"},data_refs=refs,missing=missing,
        coverage=dict(symbols_requested_in_increment=len(universe),symbols_available_in_increment=len(refs),
                      symbols_missing_in_increment=len(missing),inherited_ref_count=0,total_ref_count=len(refs)))
    return rel,promoted

def git(repo,*args,data=None):
    return subprocess.run(["git","-C",str(repo),*args],input=data,check=True,capture_output=True).stdout

def commit_files(repo, message, parent=None):
    command=(f"commit refs/heads/main\ncommitter Fixture <test@example.invalid> 1700000000 +0000\n"
             f"data {len(message)}\n{message}\n").encode()
    if parent: command+=f"from {parent}\n".encode()
    for p in sorted((repo/"data").rglob("*")):
        if p.is_file():
            b=p.read_bytes()
            command+=f"M 100644 inline {p.relative_to(repo)}\ndata {len(b)}\n".encode()+b+b"\n"
    git(repo, "fast-import", "--quiet",data=command+b"\ndone\n")
    git(repo,"reset","--hard","refs/heads/main")
    return git(repo,"rev-parse","HEAD").decode().strip()

class PointerAcceptance(unittest.TestCase):
    def test_complete_then_late_subset_manifest_and_inventory(self):
        with TemporaryDirectory() as d:
            repo=Path(d); git(repo,"init","--quiet","-b","main")
            root=repo/"data/market-data"
            result=repo/"result.json"
            result.write_text('{"snapshot_written":true}')
            old,moved=emit(root,"06:40:07",ALL)
            self.assertTrue(moved)
            m0=build_manifest(store_root=root,result_file=result,request_id="original-request",trade_date=DAY,stage="premarket")
            old_entries={i["path"]:i["blob_sha"] for i in json.loads(m0.read_text())["required_outputs"]}
            self.assertIn(old,old_entries)
            base=commit_files(repo,"original")
            pointer=(root/old).with_name("latest.json")
            old_pointer=pointer.read_bytes()
            new,moved=emit(root,"17:12:18",SIX)
            self.assertFalse(moved)
            self.assertEqual(pointer.read_bytes(),old_pointer)
            inv=build_inventory(root)["stage_snapshot_summary"]
            self.assertEqual([(x["snapshot_path"],x["missing_count"]) for x in inv],[(old,0)])
            m1=build_manifest(store_root=root,result_file=result,request_id="late-request",trade_date=DAY,stage="premarket")
            doc=json.loads(m1.read_text())
            required={i["path"]:i["blob_sha"] for i in doc["required_outputs"]}
            self.assertEqual(doc["request_id"],"late-request")
            self.assertEqual(required[new],git_blob_sha_bytes((root/new).read_bytes()))
            self.assertNotIn(old,required)
            self.assertNotIn(str(pointer.relative_to(root)),required)
            second=commit_files(repo,"late",base)
            readiness=root/"collector-state/readiness-fixture.json"
            readiness.write_text("{}")
            commit_files(repo,"readiness",second)
            self.assertEqual(git(repo,"show",f"HEAD:data/market-data/{new}"),(root/new).read_bytes())
            self.assertEqual(git(repo,"show",f"HEAD:data/market-data/{old}"),(root/old).read_bytes())
            self.assertEqual(pointer.read_bytes(),old_pointer)
    def test_equal_or_different_scope_follows_chronology(self):
        for universe,rows in [(ALL,ALL), (ALL[:-1]+["NEW"],SIX[:-1])]:
            with self.subTest(universe=universe),TemporaryDirectory() as d:
                root=Path(d);emit(root,"06:40:07",ALL)
                _,moved=emit(root,"17:12:18",rows,universe)
                self.assertTrue(moved)
    def test_first_partial_and_other_stages_unchanged(self):
        with TemporaryDirectory() as d:
            root=Path(d);self.assertTrue(emit(root,"06:40:07",SIX)[1])
            self.assertTrue(emit(root,"06:40:07",ALL,stage="close")[1])
            self.assertTrue(emit(root,"17:12:18",SIX,stage="close")[1])
    def test_unverified_incumbent_does_not_claim_preservation(self):
        with TemporaryDirectory() as d:
            root=Path(d);old,_=emit(root,"06:40:07",ALL)
            with (root/old).open("ab") as f:f.write(b" ")
            self.assertTrue(emit(root,"17:12:18",SIX)[1])
