"""Hosted route interface and synthetic exact Git tests; no credentials or network."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]

def test_manual_workflow_public_surface():
    path=ROOT/".github/workflows/private-fixed-daily-tail.yml"
    assert path.is_file(), "missing actual hosted entry"
    job_doc=yaml.safe_load(path.read_text())
    triggers=job_doc.get(True,job_doc.get("on"))
    assert set(triggers)=={"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"])=={"expected_compute_sha","expected_receipt_commitment"}
    job=job_doc["jobs"]["append"]
    assert job["concurrency"]==dict(group="market-data-collector",queue="max",**{"cancel-in-progress":False})
    assert "GITHUB_WORKFLOW_REF" in job["steps"][0]["run"]
    assert all("secrets." not in repr(s) for s in job["steps"][:-1])
    assert "scripts.run_private_fixed_tail_20261120" in job["steps"][-1]["run"]
    assert {"EXPECTED_COMPUTE_SHA","EXPECTED_RECEIPT_COMMITMENT","MARKET_DATA_STORE_TOKEN","TWELVE_DATA_API_KEY"}==set(job["steps"][-1]["env"])
    assert not {"store_sha","fixed_selection","descriptor_commit"} & set(triggers["workflow_dispatch"]["inputs"])


def _hosted_private_grant_claim_on_file_git(*, rewind_after_claim=False):
    from contextlib import nullcontext
    import os, sys, tempfile, json, hashlib
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import patch
    sys.path.insert(0,str(ROOT))
    from scripts import run_private_fixed_tail_20261120 as hosted
    from scripts.private_authority_input import load_authority
    from scripts.private_authority_receipt import canonical_receipt, receipt_commitment
    from test_private_fixed_tail_20261120 import ExactPrivateTail, ENV, NOW, SHA, EXEC, END, git, collection_universe
    from backfill_market_data_tail import backfill
    assert callable(backfill)  # Expected native child exists, but won't be called.
    # After imports, disallow underlying Python network calls too.
    def no_network(event,args):
        if event.startswith("socket."):
            raise AssertionError("fixture has no network")
    sys.addaudithook(no_network)
    guard=dict(GIT_ALLOW_PROTOCOL="file",GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_SYSTEM="/dev/null")
    with tempfile.TemporaryDirectory(prefix="fixed-hosted-fixture-") as root, patch.dict(os.environ,guard):
        root=Path(root).resolve()
        fixture_dir=root/"source";fixture_dir.mkdir()
        repo, private_grant=ExactPrivateTail().fixture(fixture_dir)
        old=private_grant["store_sha"]
        budget=f"data/market-data/collector-state/{EXEC[:7]}/{EXEC}-twelve-data-budget.json"
        path=repo/budget;path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(schema_version=1,trade_date=EXEC,credits=190,minutes={})))
        git(repo,"add","--",budget)
        git(repo,"-c","user.name=Fixture","-c","user.email=fixture@example.invalid","-c","commit.gpgsign=false","commit","-qm","fixture budget")
        old=git(repo,"rev-parse","HEAD")
        private_grant["store_sha"]=old
        branch=f"capture-authority/{hosted.ROUTE}/{SHA}"
        git(repo,"checkout","-qb",branch)
        def encode(value):return (json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode("ascii")
        grant_raw=encode(private_grant)
        descriptor=dict(schema_version=1,kind="capture_authority_selection",route=hosted.ROUTE,
                       compute_sha=SHA,authority_store_sha=old,approval=private_grant,
                       approval_sha256=hashlib.sha256(grant_raw).hexdigest())
        description=repo/"capture-authority/selection.json"
        description.parent.mkdir();description.write_bytes(encode(descriptor))
        git(repo,"add","--","capture-authority/selection.json")
        git(repo,"-c","user.name=Fixture","-c","user.email=fixture@example.invalid","-c","commit.gpgsign=false","commit","-qm","fabricated off-main descriptor")
        desc_sha=git(repo,"rev-parse","HEAD")
        bare=root/"fake.git";bare.mkdir()
        git(bare,"init","--bare","--initial-branch=main","-q")
        git(repo,"remote","add","origin",str(bare))
        git(repo,"push","-q","origin",f"{old}:refs/heads/main",f"refs/heads/{branch}:refs/heads/{branch}")
        raw=canonical_receipt(dict(schema_version=1,kind="capture_authority_receipt",route=hosted.ROUTE,
                      compute_sha=SHA,authority_store_sha=old,descriptor_commit=desc_sha,nonce="a"*64))
        commitment=receipt_commitment(raw)
        def fixture_fetch(token,route,compute,expected):
            assert token=="fake-token-only" and route==hosted.ROUTE and compute==SHA
            assert expected==commitment
            return raw
        def fixture_materialize(token,route,compute,anchor,desc,destination):
            assert desc==desc_sha and anchor==old
            git(root,"clone","-q",str(bare),str(destination))
            git(destination,"fetch","-q","origin",f"refs/heads/{branch}:refs/heads/{branch}")
            # Actual native strict-local Git reader, not a fabricated descriptor.
            return load_authority(destination,route,compute,anchor,desc)
        def fixture_bind(w,route,compute,selection):
            assert git(w,"remote","get-url","origin")==str(bare)
            assert git(w,"rev-parse","HEAD")==selection.authority_store_sha==git(bare,"rev-parse","refs/heads/main")
            assert git(bare,"rev-parse",f"refs/heads/{branch}")==selection.descriptor_commit
        helpers=SimpleNamespace(collection=collection_universe,
                                collectors=SimpleNamespace(load_yaml=lambda p:yaml.safe_load(p.read_text())))
        plans=[]
        wall=NOW
        elapsed=100.0
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return wall.astimezone(tz) if tz is not None else wall.replace(tzinfo=None)
        native_claim=hosted.retention.retain_capture_object
        def commit_then_clock_rewind(*args, **kwargs):
            nonlocal wall, elapsed
            receipt=native_claim(*args, **kwargs)
            if rewind_after_claim:
                elapsed+=3600
                wall-=timedelta(minutes=1)  # Remains inside the approved wall window.
            return receipt
        def prewrite(*args,**kw):
            plans.append(args[5]);raise AssertionError("stop before any native plan write/child")
        def fixture_main(w):
            assert git(w,"remote","get-url","origin")==str(bare)
            return git(bare,"rev-parse","refs/heads/main")
        import backfill_market_data_tail as tail
        b=hosted.HostedBackend(compute=ROOT,compute_sha=SHA,token="fake-token-only",
                                key="fake-key-only",destination=root/"consumer")
        with patch.object(hosted.time,"monotonic",side_effect=lambda:elapsed), \
             patch.object(hosted.native.r,"datetime",FrozenDatetime), \
             patch.object(hosted.retention,"retain_capture_object",side_effect=commit_then_clock_rewind), \
             patch.object(hosted,"fetch_private_receipt",side_effect=fixture_fetch), \
             patch.object(hosted,"materialize_store",side_effect=fixture_materialize), \
             patch.object(hosted,"resolve_private_reference"),patch.object(hosted,"_source"), \
             patch.object(hosted,"_store_binding",side_effect=fixture_bind),patch.object(hosted,"_environment",lambda *args:nullcontext()), \
             patch.object(hosted,"_git",side_effect=git),patch.object(hosted,"_remote_main",side_effect=fixture_main), \
             patch.object(hosted.native.a,"load_helpers",return_value=helpers),patch.object(hosted.native.r,"source_guard"), \
             patch.object(hosted.native,"_publish_metadata",side_effect=prewrite), \
             patch.object(tail,"backfill",side_effect=AssertionError("never call provider")) as child:
            result=hosted.profile.run_profile(env=ENV,compute_sha=SHA,expected_commitment=commitment,backend=b,now=NOW)
            assert result==hosted.profile.UNKNOWN and child.call_count==0
            assert b.op is not None
            op=b.op.operational_store_sha
            assert b.op.approval_store_sha==old and op!=old
            assert git(bare,"rev-parse",f"refs/tags/market-data-read/{op}")==op
            assert git(bare,"rev-parse",f"{old}:data/market-data")==git(bare,"rev-parse",f"{op}:data/market-data")
            if rewind_after_claim:
                assert not plans, "a durable claim must not renew the originally anchored expiry"
                assert git(bare,"rev-parse","refs/heads/main")==op
                return
            assert len(plans)==1 and plans[0]["store_start_sha"]==op
            assert plans[0]["maximum_provider_requests"]==10
            assert plans[0]["fixed_selection"]["targets"]==private_grant["targets"]
            replay=hosted.HostedBackend(compute=ROOT,compute_sha=SHA,token="fake-token-only",
                 key="fake-key-only",destination=root/"replay")
            repeat=hosted.profile.run_profile(env=dict(ENV,GITHUB_RUN_ID="43"),compute_sha=SHA,
                         expected_commitment=commitment,backend=replay,now=NOW)
            assert repeat==hosted.profile.STOP and replay.op is None and len(plans)==1
            assert git(bare,"rev-parse","refs/heads/main")==op

def test_hosted_private_grant_claim_and_native_prewrite_on_file_git():
    _hosted_private_grant_claim_on_file_git()


def test_elapsed_private_grant_expires_across_claim_even_with_wall_rewind():
    _hosted_private_grant_claim_on_file_git(rewind_after_claim=True)


if __name__=="__main__":
    test_manual_workflow_public_surface()
    print("PASS hosted declaration exposes only public SHA/commitment and reuses Compute queue")
    test_hosted_private_grant_claim_and_native_prewrite_on_file_git()
    print("PASS real offline Git descriptor, one stable claim, readback, precise native prewrite and replay stop")
