"""Owner-only hosted entry for the private fixed Nov 20 tail.

Only public Compute SHA and standard opaque salted receipt commitment are
inputs. Existing authenticated private descriptor carries the closed grant.
No promotion of a descriptor onto Store main occurs here.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

COMPUTE = Path(__file__).resolve().parents[1]
SCRIPTS = COMPUTE / "scripts"
sys.path.insert(0, str(SCRIPTS))  # Same native modules used by existing route.

from scripts import private_fixed_tail_20261120 as profile
from scripts.market_data_store import git_blob_sha_bytes
from scripts.private_authority_materialize import materialize_store
from scripts.private_authority_receipt import verify_receipt
from scripts.private_authority_receipt_transport import fetch_private_receipt
from scripts.private_authority_transport import resolve_private_reference
from scripts.run_private_authority_route import _environment, _git, _remote_main, _source, _store_binding, STORE_URL

import capture_store_retention as retention
import maintain_fixed_symbols as native
import private_fixed_tail_readback as readback
import yaml

ROUTE = "fixed_daily_tail_20261120"
LANE = "FIXED-TAIL-1120"


def _need(condition):
    if not condition:
        raise ValueError("PRIVATE_FIXED_TAIL_BOUNDARY")


def _claim_payload(*, commitment, compute_sha, prior, day, request_id):
    value = dict(schema_version=1, kind="fixed_daily_tail_batch_claim", route=ROUTE,
                 approval_commitment=commitment, reviewed_store_sha=prior, compute_sha=compute_sha,
                 execution_date=day, historical_cutoff=profile.CUTOFF, request_id=request_id,
                 physical_attempt=secrets.token_hex(32))
    # Physical attempt changes bytes only. The path is stable per grant even if
    # a new workflow run ID is presented. Identical bytes can co-confirm.
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _verify_claim(repo, old, receipt, path, payload, git=_git, remote_main=_remote_main):
    new = receipt["store_commit_sha"]
    _need(profile._hex(new,40) and new != old and receipt["path"] == path)
    blob = git_blob_sha_bytes(payload)
    tag = f"refs/tags/market-data-read/{new}"
    _need(receipt["sha256"] == sha256(payload).hexdigest() and receipt["blob_sha"] == blob
          and receipt["durability_ref"] == tag and receipt["market_qualified"] is False
          and receipt["production_activation"] is False)
    _need(git(repo,"rev-parse","HEAD") == new
          and git(repo,"rev-list","--parents","-n1",new) == f"{new} {old}"
          and git(repo,"diff-tree","--no-commit-id","--name-status","-r",new) == f"A\t{path}"
          and git(repo,"ls-tree",new,"--",path) == f"100644 blob {blob}\t{path}")
    # Whole market tree unchanged, including all 10 indexes, all their shards
    # and ledgers. Exact positive membership; no remote-path fallback.
    _need(git(repo,"rev-parse",f"{old}:data/market-data") == git(repo,"rev-parse",f"{new}:data/market-data"))
    _need(git(repo,"ls-remote","--refs","origin",tag) == f"{new}\t{tag}" and remote_main(repo) == new)
    return profile.ClaimHandoff(old,new)


class HostedBackend:
    def __init__(self, *, compute, compute_sha, token, key, destination):
        self.compute=compute
        self.compute_sha=compute_sha
        self.token=token
        self.key=key
        self.repo=destination
        self.selection=None
        self.grant=None
        self.commitment=None
        self.op=None
        self.approval_window=None
        self.selector=None
        self.helpers=None
        self.config=yaml.safe_load((compute/"config/market-data-store.yaml").read_text(encoding="utf-8"))
        limits=self.config["collector"]["budgets"]["twelve_data_basic"]
        _need(limits["hard_credits_per_day"] == 200 and limits["hard_credits_per_minute"] == 8)
        policy=self.config["collector"]["twelve_data_daily"]["bounded_overlap_revision_policy"]
        _need(policy == "price_only_volume_blocked_v1")

    def fetch_private_grant(self, commitment, compute_sha):
        _need(self.selection is None and compute_sha == self.compute_sha)
        _source(self.compute,self.compute_sha)
        raw=fetch_private_receipt(self.token,ROUTE,self.compute_sha,commitment)
        authorized=verify_receipt(raw,commitment,ROUTE,self.compute_sha)
        # No prepare_promoted_authority: the descriptor stays off main.
        selection=materialize_store(self.token,ROUTE,self.compute_sha,authorized.authority_store_sha,
                                    authorized.descriptor_commit,self.repo)
        _need(selection.authority_store_sha==authorized.authority_store_sha
              and selection.descriptor_commit==authorized.descriptor_commit
              and sha256(selection.approval_raw).hexdigest()==selection.approval_sha256)
        self.selection=selection
        self.commitment=commitment
        self.grant=json.loads(selection.approval_raw)
        return selection.approval_raw

    def materialize_exact_store(self, sha):
        _need(self.selection is not None and sha==self.selection.authority_store_sha)
        return self.repo

    def _anchor_private_approval(self):
        # The first verified preclaim observation owns the elapsed deadline.
        # Don't sample again after claim/readback: wall-clock rewind could
        # otherwise renew an already expired approval.
        _need(self.grant is not None and self.approval_window is None)
        observed_elapsed=time.monotonic()
        observed_wall=native.r.now_utc()
        profile._window(self.grant,observed_wall)
        until=profile._utc(self.grant["valid_until"])
        self.approval_window=(until,observed_elapsed+(until-observed_wall).total_seconds())

    def _check_claim_window(self):
        _need(self.approval_window is not None)
        until,deadline=self.approval_window
        _need(time.monotonic()<deadline and native.r.now_utc()<until)

    def preclaim_state(self, *, repo, store_sha, execution_date):
        _need(repo==self.repo and store_sha==self.selection.authority_store_sha
              and execution_date==self.grant["execution_date"])
        self._anchor_private_approval()
        _source(self.compute,self.compute_sha)
        resolve_private_reference(self.token,ROUTE,self.compute_sha,self.selection.descriptor_commit)
        with _environment(self.token):
            _store_binding(repo,ROUTE,self.compute_sha,self.selection)
            budget=native._budget(repo,execution_date,self.config)
            self._check_claim_window()
            return dict(compute_repository="yuatom/stock-market-data",lock_group="market-data-collector",
                        lock_held=True,cancel_in_progress=False,queue="max",store_sha=_remote_main(repo),
                        available_credits=200-budget["credits"])

    def claim_once(self, commitment, run_id):
        _need(commitment == self.commitment and self.op is None)
        self._check_claim_window()
        _source(self.compute,self.compute_sha)
        old=self.selection.authority_store_sha
        day=self.grant["execution_date"]
        request_id=f"{run_id}-1"
        raw=_claim_payload(commitment=commitment,compute_sha=self.compute_sha,prior=old,day=day,request_id=request_id)
        with _environment(self.token):
            self._check_claim_window()
            receipt=retention.retain_capture_object(repo=self.repo,expected_commit=old,
               approval_sha256=commitment,symbol=LANE,name="claim.json",payload=raw,
               execution_day=day,request_id=request_id,forbidden_bytes=(self.key.encode(),self.token.encode()))
            self.op=_verify_claim(self.repo,old,receipt,f"capture-evidence/{commitment}/{LANE}/claim.json",raw,
              git=_git,remote_main=_remote_main)
        return self.op

    def invoke_native(self, **kwargs):
        op=self.op
        _need(op is not None and kwargs["repo"]==self.repo
              and kwargs["compute_sha"]==self.compute_sha and kwargs["store_sha"]==op.operational_store_sha
              and kwargs["execution_date"]==self.grant["execution_date"] and kwargs["historical_cutoff"]==profile.CUTOFF
              and kwargs["fixed_selector"]=={"schema_version":1,"targets":self.grant["targets"]})
        _need(self.approval_window is not None)
        authorization_valid_until,authorization_deadline=self.approval_window
        _source(self.compute,self.compute_sha)
        self.helpers=native.a.load_helpers()
        self.selector=kwargs["fixed_selector"]
        universe=self.helpers.collection.load_collection_universe(self.compute/"config/collection-universe.json")
        access=self.helpers.collectors.load_yaml(self.compute/"config/market-data-collector-access.yaml")
        with _environment(self.token):
            native._remote_exact(self.repo,op.operational_store_sha)
            return native.maintain_append(h=self.helpers,compute=self.compute,config=self.config,
                                          universe=universe,access=access,key=self.key,
                                          authorization_valid_until=authorization_valid_until,
                                          authorization_deadline=authorization_deadline,**kwargs)

    def verify_native_receipts(self, *, result, compute_sha, store_sha, execution_date, historical_cutoff, request_id):
        _need(self.op is not None and self.selector is not None and self.helpers is not None
              and compute_sha==self.compute_sha and store_sha==self.op.operational_store_sha
              and execution_date==self.grant["execution_date"] and historical_cutoff==profile.CUTOFF)
        with _environment(self.token):
            return readback.verify(h=self.helpers,repo=self.repo,result=result,compute_sha=compute_sha,
              store_sha=store_sha,execution_date=execution_date,historical_cutoff=historical_cutoff,
              request_id=request_id,fixed_selector=self.selector)


class _Parser(argparse.ArgumentParser):
    def error(self, _):
        raise ValueError("PRIVATE_FIXED_TAIL_INVALID_DISPATCH")


def main(argv=None):
    result=profile.STOP
    try:
        parser=_Parser(add_help=False,allow_abbrev=False)
        parser.add_argument("--compute-sha",required=True)
        parser.add_argument("--receipt-commitment",required=True)
        args=parser.parse_args(argv)
        profile._identity(os.environ,args.compute_sha)
        _need(profile._hex(args.receipt_commitment,64))
        token=os.environ.get("MARKET_DATA_STORE_TOKEN","")
        key=os.environ.get("TWELVE_DATA_API_KEY","")
        _need(re.fullmatch(r"[A-Za-z0-9_.-]{8,4096}",token) and
              8 <= len(key) <= 4096 and all(33 <= ord(c) <= 126 for c in key))
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix="private-fixed-tail-") as parent, _environment():
            backend=HostedBackend(compute=COMPUTE,compute_sha=args.compute_sha,token=token,
                                  key=key,destination=Path(parent).resolve()/"store")
            result=profile.run_profile(env=os.environ,compute_sha=args.compute_sha,
                                       expected_commitment=args.receipt_commitment,backend=backend)
    except KeyboardInterrupt:
        raise KeyboardInterrupt() from None
    except (Exception,SystemExit):
        pass
    print(result)
    return 0 if result == profile.DONE else 2


if __name__ == "__main__":
    raise SystemExit(main())
