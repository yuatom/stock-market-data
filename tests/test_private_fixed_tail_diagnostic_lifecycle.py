"""Portable offline, file-only regression for post-stop native lifecycle evidence."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

def _exercise(scenario):
    source=Path(__file__).resolve().parents[1]
    fixture=source/"tests/fixtures/private_fixed_tail_native_lifecycle.py"
    env=dict(os.environ,GIT_ALLOW_PROTOCOL="file",GIT_CONFIG_NOSYSTEM="1",
             GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_SYSTEM="/dev/null")
    env.pop("PYTHONPATH",None)
    with tempfile.TemporaryDirectory(prefix="private-fixed-tail-lifecycle-") as d:
        subprocess.run([sys.executable,"-B",str(fixture),d,str(source),str(source/"scripts"),scenario],
                       cwd=source,env=env,check=True,timeout=120)
        subprocess.run([sys.executable,"-B",__file__,"--assert-worker",d,str(source),str(source/"scripts"),scenario],
                       cwd=source,env=env,check=True,timeout=120)

def test_native_lifecycle_partial_diagnostic():
    _exercise("terminal-before-checkpoint")

def test_stopped_child_exact_debit_only():
    _exercise("debit-exact")

def test_stopped_child_debit_rejects_wrong_report_blob():
    _exercise("debit-wrong-report-blob")

def test_stopped_child_debit_without_tag():
    _exercise("debit-no-tag")

def _assert_worker():
    del sys.argv[1]
    from types import SimpleNamespace
    from unittest.mock import patch
    import json

    source=Path(sys.argv[2]).resolve()
    helpers=Path(sys.argv[3]).resolve()
    sys.path[:0]=[str(source),str(source/"scripts"),str(helpers)]
    sys.addaudithook(lambda event,args: (_ for _ in ()).throw(AssertionError("offline")) if event.startswith("socket.") else None)
    os.environ.update(GIT_ALLOW_PROTOCOL="file",GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_NOSYSTEM="1")
    from private_fixed_tail_diagnostic import diagnose,DIAGNOSTIC,native
    from scripts import market_data_store as store
    import collection_universe as collection
    import rebase_market_data_baseline as rebaser
    import market_data_collectors as collectors

    fixture=Path(sys.argv[1]).resolve()
    v=json.loads((fixture/"LOCATORS.json").read_text())
    repo=fixture/"synthetic-work"
    h=SimpleNamespace(store=store,collection=collection,collectors=collectors,rebaser=rebaser)
    fields=dict(observed_main=v["later_et_main"],approval_store_sha=v["reviewed"],operational_store_sha=v["claim"],
        commitment=v["claim_path"].split("/")[1],plan_sha=v["plan"],compute_sha=v["compute_sha"],execution_date=v["execution_date"],
        historical_cutoff=v["historical_cutoff"],request_id=v["request_id"],fixed_selector=v["selector"])
    def git(*args):
        return subprocess.check_output(["git","-C",str(repo),*args],text=True).strip()

    before=git("rev-parse","HEAD")
    assert git("status","--porcelain")==""
    result=diagnose(h,repo,**fields)
    scenario=sys.argv[4]
    if scenario.startswith("debit-"):
        assert v["stopped"]
        expected=4 if scenario=="debit-exact" else 3
        actual=result.get("verified_project_credit_debits_lower_bound",result["verified_price_rows"])
        assert actual==expected,(scenario,expected,result)
        assert result["verified_price_rows"]==3 and result["verified_checkpoints"]==3,result
        assert result["verified_volume_unresolved"]==1 and result["status"]==DIAGNOSTIC,result
        assert result["provider_requests"]==result["provider_charges"]=="UNKNOWN",result
        assert not result["automatic_retry_allowed"],result
        assert git("rev-parse","HEAD")==before==fields["observed_main"] and git("status","--porcelain")==""
        print(f"PASS {scenario}: 3 prices/3 checkpoints, {expected} proven project credits, provider requests and charges unknown; EOD ledger=5 excluded")
        return
    if scenario=="noncanonical":
        assert result["verified_price_rows"]==0 and not result["prefix_evidence_available"] and result["status"]==DIAGNOSTIC,result
        print("PASS noncanonical claim bytes rejected before attributing any child evidence")
        sys.exit(0)
    assert result==dict(status=DIAGNOSTIC,verified_price_rows=3,verified_checkpoints=2,verified_volume_unresolved=1,
                       prefix_evidence_available=True,later_main_observed=True,automatic_retry_allowed=False,
                       verified_project_credit_debits_lower_bound=3,provider_requests="UNKNOWN",provider_charges="UNKNOWN"),result
    print("PASS 3 durable actual native price appends, 2 checkpoints, 1 unresolved volume; later unrelated EOD excludes its ledger effects")
    actual=native.a.git
    third=v["children"][2]["row"]["store_commit_sha"]
    def no_third_tag(repo,*args):
        if len(args)>=3 and args[0]=="ls-remote" and args[-1]==f"refs/tags/market-data-read/{third}":
            return ""
        return actual(repo,*args)
    with patch.object(native.a,"git",side_effect=no_third_tag):
        missing=diagnose(h,repo,**fields)
    assert missing["status"]==DIAGNOSTIC and missing["verified_price_rows"]==2 and missing["verified_checkpoints"]==2 and not missing["automatic_retry_allowed"],missing
    print("PASS missing third retention tag leaves just 2 proven; unknown/no-retry remains")
    third_path=v["children"][2]["row"]["result_state_path"]
    def add_post_terminal_mutation(repo,*args):
        actual_result=actual(repo,*args)
        if args and args[0]=="diff-tree" and fields["observed_main"] in args and third_path in args:
            return "\n".join(filter(None,(actual_result,f"M\t{third_path}")))
        return actual_result
    with patch.object(native.a,"git",side_effect=add_post_terminal_mutation):
        changed=diagnose(h,repo,**fields)
    assert changed["verified_price_rows"]==2 and changed["verified_checkpoints"]==2 and changed["status"]==DIAGNOSTIC,changed
    print("PASS unexpected fourth report version (post-terminal mutation) cannot count third")

    wrong=dict(fields,plan_sha=third)
    result=diagnose(h,repo,**wrong)
    assert result["verified_price_rows"]==0 and not result["prefix_evidence_available"] and result["status"]==DIAGNOSTIC,result
    print("PASS wrong run-plan identity yields no durable attribution")
    remote_exact=native._remote_exact
    observations=[]
    def moving_main(repo,sha):
        observations.append(1)
        if len(observations)>1:
            raise ValueError("synthetic concurrent main movement")
        return remote_exact(repo,sha)
    with patch.object(native,"_remote_exact",side_effect=moving_main):
        moved=diagnose(h,repo,**fields)
    assert moved["verified_price_rows"]==0 and not moved["prefix_evidence_available"],moved
    print("PASS possible further main movement during diagnostic has no attributable output")
    assert git("rev-parse","HEAD")==before==fields["observed_main"] and git("status","--porcelain")==""


if __name__=="__main__":
    if sys.argv[1:2]==["--assert-worker"]:
        _assert_worker()
    else:
        test_stopped_child_exact_debit_only()
        test_stopped_child_debit_rejects_wrong_report_blob()
        test_stopped_child_debit_without_tag()
        test_native_lifecycle_partial_diagnostic()
