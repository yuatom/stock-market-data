"""Literal Git descriptor verification, never an issuer or real grant."""
from contextlib import redirect_stdout
from datetime import datetime
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import market_data_collectors as collector
from scripts.private_authority_input import STORE_REPOSITORY

ROOT=Path(__file__).resolve().parents[1]
COMPUTE="1"*40
DAY="2041-06-04"
SESSION="2041-06-03"
NOW=datetime.fromisoformat("2041-06-04T11:05:00+00:00")
START="2041-06-04T11:00:00Z"
STOP="2041-06-04T12:20:00Z"
SCOPE=(("TEST1","stocks"),("TEST2","etf"))


def api():
    # Import on demand so baseline has an explicit RED for the missing reader.
    return importlib.import_module("scripts.ordinary_eod_authority")


def commit_fixture(repo, mod, *, changes=None, tree_extra=False):
    def git(*args, data=None):
        return subprocess.check_output(("git","-C",str(repo),"-c","user.name=fixture",
            "-c","user.email=fixture@example.invalid",*args),input=data).decode().strip()
    git("init","-q","--template=","-b","main")
    git("config","remote.origin.url",f"https://github.com/{STORE_REPOSITORY}")
    marker=git("hash-object","-w","--stdin",data=b"literal immutable synthetic marker\n")
    start_tree=git("mktree",data=f"100644 blob {marker}\tmarker\n".encode())
    base=git("commit-tree",start_tree,data=b"synthetic exact Store parent\n")
    git("update-ref","refs/heads/main",base)
    obj={
        "schema_version":1,"kind":"ordinary_previous_session_eod_authority_v1",
        "compute_sha":COMPUTE,"workflow_source_sha":COMPUTE,"store_repository_id":11,
        "store_parent_sha":base,"execution_day":DAY,"previous_session":SESSION,
        "market":"XNYS","provider":"twelve_data_basic","queue":"market-data-collector",
        "workflow":".github/workflows/market-data-collector.yml",
        "trigger_policy":"native_schedule_or_authorized_manual_first_attempt",
        "identity_scope":[{"symbol":s,"asset_class":a} for s,a in SCOPE],
        "scope_sha256":mod.scope_digest(SCOPE),
        "valid_from":START,"valid_until":STOP,"max_attempts":1,
        "max_requests":2,"max_credits":2,"allow_paid_upgrade":False,"nonce":"2"*64,
    }
    if changes: obj.update(changes)
    raw=mod.canonical(obj)
    blob=git("hash-object","-w","--stdin",data=raw)
    subtree=git("mktree",data=f"100644 blob {blob}\tmandate.json\n".encode())
    payload=f"040000 tree {subtree}\tordinary-authority\n100644 blob {marker}\tmarker\n"
    if tree_extra: payload+=f"100644 blob {marker}\tunapproved-extra\n"
    tree=git("mktree",data=payload.encode())
    desc=git("commit-tree",tree,"-p",base,data=b"synthetic untrusted Git author\n")
    digest=mod.commitment(raw)
    git("update-ref",mod.authority_ref(COMPUTE,digest),desc)
    return mod.ExpectedOrdinaryScope(compute_sha=COMPUTE,store_repository_id=11,store_parent_sha=base,
        commit_sha=desc,commitment_sha256=digest,execution_day=DAY,previous_session=SESSION,
        scope=SCOPE,valid_from=START,valid_until=STOP),blob,tree


class OrdinaryAuthoritySourceTest(unittest.TestCase):
    def test_literal_git_object_validates_bytes_but_cannot_authorize(self):
        mod=api()
        with tempfile.TemporaryDirectory() as t:
            repo=Path(t); scope,blob,tree=commit_fixture(repo,mod)
            prepared=mod.read_ordinary_authority(repo,scope,now=NOW)
            self.assertEqual((prepared.commit_sha,prepared.tree_sha,prepared.blob_sha),
                (scope.commit_sha,tree,blob))
            self.assertFalse(prepared.authorization_authenticated)
            self.assertFalse(prepared.production_activation)
            with self.assertRaisesRegex(mod.OrdinaryEodUnregistered,"VERIFIED_CREATOR_UNREGISTERED"):
                mod.require_hosted_binding(prepared)

    def test_wrong_scope_shapes_and_finite_fallback_are_rejected(self):
        mod=api()
        for patch_data in ({"kind":"finite_daily_basis_authority"}, {"workflow_source_sha":"f"*40},
                       {"queue":"somewhere-else"}, {"store_repository_id":12}, {"trigger_policy":"manual-only"}, {"previous_session":"2041-06-02"},
                       {"max_requests":3},{"max_credits":3},{"allow_paid_upgrade":True},
                       {"identity_scope":[{"symbol":"TEST9","asset_class":"stocks"}]},
                       {"verified_creator":True}, {"creator_actor_id":1}):
            with self.subTest(keys=tuple(patch_data)), tempfile.TemporaryDirectory() as t:
                scope,*_=commit_fixture(Path(t),mod,changes=patch_data)
                with self.assertRaisesRegex(mod.OrdinaryAuthorityInvalid,"ORDINARY_EOD_AUTHORITY_INVALID"):
                    mod.read_ordinary_authority(Path(t),scope,now=NOW)

    def test_drift_extra_files_commitment_and_exact_cutoff_fail_closed(self):
        from dataclasses import replace
        mod=api()
        with tempfile.TemporaryDirectory() as t:
            path=Path(t); scope,*_=commit_fixture(path,mod,tree_extra=True)
            with self.assertRaises(mod.OrdinaryAuthorityInvalid):
                mod.read_ordinary_authority(path,scope,now=NOW)
        with tempfile.TemporaryDirectory() as t:
            path=Path(t); scope,*_=commit_fixture(path,mod)
            for change,now in ((scope,datetime.fromisoformat(STOP)),(replace(scope,commitment_sha256="f"*64),NOW),
                    (replace(scope,commit_sha=scope.store_parent_sha),NOW)):
                with self.subTest(now=now), self.assertRaises(mod.OrdinaryAuthorityInvalid):
                    mod.read_ordinary_authority(path,change,now=now)

    def test_host_cli_unregistered_before_store_or_provider_under_real_config_selector(self):
        mod=api()
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)
            cfg=collector.load_yaml(ROOT/"config/market-data-store.yaml")
            cfg["collector"]["ordinary_eod_admission"]="claimed_v1"
            (d/"config").write_text(json.dumps(cfg))
            (d/"empty").write_text("{}")
            argv=["--mode","previous_session_eod","--trade-date",DAY,"--store-root",str(d/"out"),
                      "--config",str(d/"config"),"--watchlist",str(d/"empty"),
                      "--access-config",str(d/"empty"),"--data-completeness",str(d/"empty")]
            with patch.dict(os.environ,{"ORDINARY_EOD_ISSUER_VERIFIED":"true"}), redirect_stdout(io.StringIO()) as result, \
                patch.object(mod,"require_hosted_binding",wraps=mod.require_hosted_binding) as called, \
                patch.object(collector,"fetch_twelve_daily",side_effect=AssertionError("provider crossed")) as provider:
                self.assertEqual(collector.main(argv),2)
            called.assert_called_once_with()
            provider.assert_not_called()
            self.assertFalse((d/"out").exists())
            self.assertEqual(json.loads(result.getvalue())["status"],"held_ordinary_eod_unregistered")


if __name__=="__main__":
    unittest.main(verbosity=2)
