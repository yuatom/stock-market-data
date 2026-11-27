import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"scripts")]
from scripts import market_data_store as store
import backfill_market_data_tail as native_tail
import collection_universe
import maintain_fixed_symbols as native
import fixed_baseline_targets as fixed

def deny_network(event, args):
    if event.startswith("socket."):
        raise AssertionError("network forbidden")
sys.addaudithook(deny_network)
try:
    from scripts import private_fixed_tail_20261120 as route
except ImportError as e:
    if 'private_fixed_tail_20261120' not in str(e):
        raise
    route=None

BASE="2026-11-19"; END="2026-11-20"; EXEC="2026-11-23"; SHA="a"*40
STOP="PRIVATE_FIXED_TAIL_BLOCKED"; DONE="PRIVATE_FIXED_TAIL_COMPLETE"
UNKNOWN="PRIVATE_FIXED_TAIL_OUTCOME_UNKNOWN_NO_RETRY"
PRICE_ONLY="PRIVATE_FIXED_TAIL_PRICE_ONLY_VOLUME_UNRESOLVED_NO_RETRY"
SCOPE=("AMD","META","MU","NVDA","ORCL","QQQ","SPCX","SPY","TME","TSM")
NOW=datetime.fromisoformat("2026-11-23T13:00:00+00:00")
ENV=dict(GITHUB_ACTIONS="true",GITHUB_EVENT_NAME="workflow_dispatch",GITHUB_REPOSITORY="yuatom/stock-market-data",GITHUB_REF="refs/heads/main",
    GITHUB_REPOSITORY_OWNER="yuatom",GITHUB_WORKFLOW_SHA=SHA,GITHUB_SHA=SHA,GITHUB_RUN_ATTEMPT="1",GITHUB_ACTOR="yuatom",GITHUB_TRIGGERING_ACTOR="yuatom",GITHUB_RUN_ID="42",
    GITHUB_WORKFLOW_REF="yuatom/stock-market-data/.github/workflows/private-fixed-daily-tail.yml@refs/heads/main")
UNIVERSE=collection_universe.load_collection_universe(ROOT/"config/collection-universe.json")
CONFIG=yaml.safe_load((ROOT/"config/market-data-store.yaml").read_text())
PROVIDER="twelve_data_basic"
def git(repo,*args):
    return subprocess.run(["git","-C",str(repo),*args],check=True,capture_output=True,text=True,
        env={**os.environ,"GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"}).stdout.strip()
def rows(day, high=102.,volume=5):
    return dict(trade_date=day,open=100.,close=100.,low=99.,high=high,volume=volume)
def receipt(value):
    raw=(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True)+"\n").encode("ascii")
    return raw,hashlib.sha256(b"private-fixed-daily-tail-receipt:v1\x00"+raw).hexdigest()

class LocalBackend:
    def __init__(self,value,repo):
        self.raw,self.commitment=receipt(value)
        self.repo=repo;self.calls=[];self.native_args=None
        self.override_native=None
        self.claimed=set()
        self.verified=True
        self.observation_override={}
    def fetch_private_grant(self, commitment, compute_sha):
        self.calls.append("receipt")
        assert compute_sha==SHA and hashlib.sha256(b"private-fixed-daily-tail-receipt:v1\x00"+self.raw).hexdigest()==commitment
        return self.raw
    def materialize_exact_store(self, sha):
        self.calls.append("store")
        assert git(self.repo,"rev-parse","HEAD")==sha
        return self.repo
    def preclaim_state(self, *, repo, store_sha, execution_date):
        self.calls.append("preclaim")
        assert repo==self.repo.resolve() and execution_date==EXEC
        value=dict(compute_repository="yuatom/stock-market-data",lock_group="market-data-collector",
                   lock_held=True,cancel_in_progress=False,queue="max",
                   store_sha=git(repo,"rev-parse","HEAD"),available_credits=10)
        value.update(self.observation_override)
        return value
    def claim_once(self, commitment, run_id):
        self.calls.append("claim")
        assert commitment==self.commitment and run_id.isdecimal()
        if commitment in self.claimed:
            return False
        self.claimed.add(commitment)
        return route.ClaimHandoff(git(self.repo,"rev-parse","HEAD"),"e"*40)
    def invoke_native(self, **kwargs):
        self.calls.append("native");self.native_args=kwargs
        if self.override_native:
            return self.override_native(**kwargs)
        return self.qualified_result()
    def qualified_result(self, volume_policy="PRESERVED_PROVIDER_VOLUME"):
        cursor="1"*40
        transactions=[]
        for j,symbol in enumerate(SCOPE):
            native_commit=f"{2+j*2:040x}"; checkpoint=f"{3+j*2:040x}"
            transactions.append(dict(symbol=symbol,status="PASS",volume_policy=volume_policy,store_start_sha=cursor,
                store_commit_sha=native_commit,checkpoint_commit_sha=checkpoint,result_state_path="private-fixture/result",
                checkpoint_path="private-fixture/checkpoint"))
            cursor=checkpoint
        return dict(status="PASS",requests_attempted=10,budget_debits_confirmed=10,budget_debits_unconfirmed=0,
                    published_count=10,not_attempted_symbols=[],automatic_retry_allowed=False,
                    last_verified_commit_sha=cursor,batch_manifest_commit_sha="1"*40,transactions=transactions)
    def verify_native_receipts(self, *, result, compute_sha, store_sha, execution_date, historical_cutoff, request_id):
        self.calls.append("verify")
        assert compute_sha==SHA and historical_cutoff==END and execution_date==EXEC and request_id=="42-1"
        return self.verified

class ExactPrivateTail(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(route,"missing inert 11/20 private profile module")
    def fixture(self,root,*,wrong_tail=None,eod_first=None):
        repo=Path(root)
        git(repo,"init","-q")
        targets=[]; assets=dict(collection_universe.daily_universe(UNIVERSE))
        for sym in SCOPE:
            effective=collection_universe.ticker_effective_at(UNIVERSE,sym)
            store.append_daily_bars(repo/"data/market-data",provider=PROVIDER,symbol=sym,asset_class=assets[sym],
                bars=[rows("2026-11-18" if sym==wrong_tail else END if sym==eod_first else BASE)],series_semantics="daily_regular_ohlcv",identity_effective_from=effective)
            rel=f"{fixed.INDEX_PREFIX}/{sym}/_index.json"
            targets.append(dict(symbol=sym,index_blob_sha=git(repo,"hash-object",rel)))
        git(repo,"add","data")
        git(repo,"-c","user.name=Fixture","-c","user.email=fixture@example.invalid","-c","commit.gpgsign=false","commit","-qm","fabricated history")
        value=dict(schema_version=1,kind="private_fixed_tail_20261120",nonce="b"*64,compute_sha=SHA,
            store_sha=git(repo,"rev-parse","HEAD"),execution_date=EXEC,historical_cutoff=END,retained_through=BASE,
            credit_limit=10,request_limit=10,valid_from="2026-11-23T12:55:00+00:00",valid_until="2026-11-23T14:00:00+00:00",targets=targets)
        return repo,value
    def call(self,b,env=ENV,commitment=None,now=NOW):
        return route.run_profile(env=env,compute_sha=SHA,expected_commitment=commitment or b.commitment,backend=b,now=now)
    def test_unwired_and_wrong_identity_never_touch_private_backend(self):
        self.assertEqual(route.run_profile(env=ENV,compute_sha=SHA,expected_commitment="c"*64,now=NOW),STOP)
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d); b=LocalBackend(record,root)
            for key,value in [("GITHUB_ACTOR","other"),("GITHUB_TRIGGERING_ACTOR","other"),("GITHUB_EVENT_NAME","push"),("GITHUB_REF","refs/heads/dev"),("GITHUB_RUN_ATTEMPT","2"),("GITHUB_WORKFLOW_REF","other"),("GITHUB_SHA","d"*40),("GITHUB_RUN_ID","0")]:
                self.assertEqual(self.call(b,dict(ENV,**{key:value})),STOP)
            self.assertEqual(b.calls,[])
    def test_exact_receipt_scope_rejects_substitution_dates_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d); b=LocalBackend(record,root)
            b.raw=b.raw.replace(b'"credit_limit":10',b'"credit_limit":11')
            self.assertEqual(self.call(b),STOP)
            self.assertEqual(b.calls,["receipt"])
            for key,new in [("credit_limit",11),("request_limit",11),("historical_cutoff","2026-11-21"),("retained_through","2026-11-18"),("execution_date","2026-11-22"),("valid_until","2026-11-23T12:59:00+00:00")]:
                altered=dict(record,**{key:new}); b=LocalBackend(altered,root)
                self.assertEqual(self.call(b),STOP); self.assertEqual(b.calls,["receipt"])
            for symbol in ("PLTR","TSLA"):
                changed=copy.deepcopy(record);changed["targets"][-1]["symbol"]=symbol
                b=LocalBackend(changed,root);self.assertEqual(self.call(b),STOP);self.assertEqual(b.calls,["receipt"])
    def test_real_git_binding_and_verified_retained_tail_precede_native(self):
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d,wrong_tail="AMD")
            b=LocalBackend(record,root)
            self.assertEqual(self.call(b),STOP)
            self.assertEqual(b.calls,["receipt","store"])
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d)
            bad=copy.deepcopy(record);bad["targets"][0]["index_blob_sha"]="f"*40
            b=LocalBackend(bad,root);self.assertEqual(self.call(b),STOP);self.assertNotIn("native",b.calls)
            b=LocalBackend(record,root)
            shard=next(root.glob("data/**/000000.json"));shard.write_text("{}\n")
            self.assertEqual(self.call(b),STOP);self.assertNotIn("native",b.calls)
    def test_exact_adapter_to_actual_native_prewrite_with_no_publication(self):
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d);b=LocalBackend(record,root)
            plan=[]
            def stop_prewrite(*args,**kw):
                plan.append(args[5]);raise AssertionError("synthetic prewrite cutoff")
            def synthetic_claim(commitment,run):
                git(root,"-c","user.name=Fixture","-c","user.email=fixture@example.invalid",
                    "-c","commit.gpgsign=false","commit","--allow-empty","-qm","synthetic operational head")
                return route.ClaimHandoff(record["store_sha"],git(root,"rev-parse","HEAD"))
            b.claim_once=synthetic_claim
            def tested_native(**kw):
                self.assertEqual(kw["historical_cutoff"],END)
                self.assertNotEqual(kw["store_sha"],record["store_sha"])
                self.assertEqual(kw["store_sha"],git(root,"rev-parse","HEAD"))
                self.assertEqual(kw["request_id"],"42-1")
                with patch.object(native.r,"source_guard"), patch.object(native,"_remote_exact"), patch.object(native,"_budget",return_value={"credits":190}), patch.object(native,"_publish_metadata",side_effect=stop_prewrite), patch.object(native_tail,"backfill",side_effect=AssertionError("no request")) as child:
                    result=native.maintain_append(h=SimpleNamespace(collection=collection_universe),compute=ROOT,config=CONFIG,
                        access={},universe=UNIVERSE,key="fake_fixture_key",clock=lambda:NOW,**kw)
                    self.assertEqual(child.call_count,0)
                return result
            b.override_native=tested_native
            self.assertEqual(self.call(b),UNKNOWN)
            self.assertEqual(len(plan),1)
            self.assertEqual(plan[0]["maximum_provider_requests"],10)
            self.assertEqual(plan[0]["fixed_selection"],{"schema_version":1,"targets":record["targets"]})
            self.assertEqual(git(root,"rev-parse","HEAD^"),record["store_sha"])
    def test_output_contains_no_private_values_and_partial_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            root,record=self.fixture(d);b=LocalBackend(record,root)
            def fake_private_result(**kw):
                print("fixture private selector: never disclose")
                os.write(2,b"fixture child fd receipt: never disclose\n")
                return {"status":"PARTIAL","store_commit_sha":record["store_sha"]}
            b.override_native=fake_private_result
            self.assertEqual(self.call(b),UNKNOWN)
            b.override_native=None
            self.assertEqual(self.call(b,dict(ENV,GITHUB_RUN_ID="43")),UNKNOWN)
            self.assertEqual(b.calls.count("native"),1)
            fresh=LocalBackend(record,root)
            self.assertEqual(self.call(fresh),DONE)

    def test_et_midnight_stops_before_claim_or_native_writes(self):
        late=datetime.fromisoformat("2026-11-24T04:59:00+00:00") # 23:59 ET on 11/23.
        boundary=datetime.fromisoformat("2026-11-24T04:30:00+00:00")
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d)
            record["valid_from"]="2026-11-24T04:00:00+00:00"
            record["valid_until"]="2026-11-24T06:00:00+00:00"
            for timestamp in (late,boundary):
                b=LocalBackend(record,repo)
                self.assertEqual(self.call(b,now=timestamp),STOP)
                self.assertEqual(b.calls,["receipt"])
            b=LocalBackend(record,repo)
            with patch.object(route,"_bound_series"):
                self.assertEqual(self.call(b,now=datetime.fromisoformat("2026-11-24T04:29:59+00:00")),DONE)
    def test_systemexit_is_value_free_and_parent_cancel_still_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d)
            b=LocalBackend(record,repo)
            def unsafe(*args,**kw):
                print("TEST_PRIVATE_EXIT_STDOUT_SENTINEL")
                os.write(2,b"TEST_PRIVATE_EXIT_FD_SENTINEL\n")
                raise SystemExit("TEST_PRIVATE_EXIT_MESSAGE_SENTINEL")
            with patch.object(b,"fetch_private_grant",side_effect=unsafe):
                try: result=self.call(b)
                except SystemExit: self.fail("private backend SystemExit escaped")
            self.assertEqual(result,STOP)
            b.override_native=unsafe
            with patch.object(route,"_bound_series"):
                try: result=self.call(b)
                except SystemExit: self.fail("private native SystemExit escaped")
            self.assertEqual(result,UNKNOWN)
            fresh=LocalBackend(record,repo)
            def cancel(**kw):
                raise KeyboardInterrupt("TEST_PRIVATE_CANCEL_SENTINEL")
            fresh.override_native=cancel
            with patch.object(route,"_bound_series"),self.assertRaises(KeyboardInterrupt) as cm:
                self.call(fresh)
            self.assertEqual(cm.exception.args,())
    def test_price_only_needs_verified_receipts_and_never_claims_full(self):
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d)
            with patch.object(route,"_bound_series"):
                for mode,want in (("price_only",PRICE_ONLY),("unverified",UNKNOWN),("missing_checkpoint",UNKNOWN)):
                    b=LocalBackend(record,repo)
                    result=b.qualified_result("BLOCKED_UNRESOLVED")
                    result["transactions"][0]["volume_policy"]="PRESERVED_PROVIDER_VOLUME"
                    b.verified=mode!="unverified"
                    if mode=="missing_checkpoint":
                        result["transactions"][-1].pop("checkpoint_commit_sha")
                    b.override_native=lambda **kw:result
                    self.assertEqual(self.call(b),want)
                    self.assertEqual(b.calls.count("native"),1)
                    if mode=="missing_checkpoint":
                        self.assertNotIn("verify",b.calls)
                    else:
                        self.assertEqual(b.calls.count("verify"),1)

    def test_eod_first_or_fresh_preclaim_drift_never_charges(self):
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d,eod_first="AMD")
            b=LocalBackend(record,repo)
            self.assertEqual(self.call(b),STOP)
            self.assertEqual(b.calls,["receipt","store"])
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d)
            with patch.object(route,"_bound_series"):
                for failure in (dict(store_sha="f"*40),dict(available_credits=9),
                                dict(compute_repository="yuatom/stock-market-data-store"),
                                dict(lock_held=False),dict(queue="single"),dict(cancel_in_progress=True)):
                    b=LocalBackend(record,repo)
                    b.observation_override=failure
                    self.assertEqual(self.call(b),STOP)
                    self.assertEqual(b.calls,["receipt","store","preclaim"])

    def test_native_settlement_boundary_precedes_claim(self):
        # 11/21 is the next ET calendar day even though it is Saturday.
        before=datetime.fromisoformat("2026-11-21T06:59:59-05:00")
        at=datetime.fromisoformat("2026-11-21T07:00:00-05:00")
        with tempfile.TemporaryDirectory() as d:
            repo,record=self.fixture(d)
            record["execution_date"]="2026-11-21"
            record["valid_from"]="2026-11-21T11:00:00+00:00"
            record["valid_until"]="2026-11-21T14:00:00+00:00"
            for when,calls,proof_calls in ((before,[],0),(at,["receipt","store"],1)):
                b=LocalBackend(record,repo)
                # Stop at first retained-history gate: eligibility is not
                # claim authority, and no provider/publication may run.
                with patch.object(route,"_bound_series",side_effect=AssertionError("offline preflight cutoff")) as verify:
                    self.assertEqual(self.call(b,now=when),STOP)
                self.assertEqual(b.calls,calls)
                self.assertEqual(verify.call_count,proof_calls)
                self.assertFalse(b.claimed)

if __name__=="__main__":
    unittest.main()
