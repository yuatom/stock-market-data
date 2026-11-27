"""Closed 11/20 profile. This module implements no CLI, network or credential handling.

The reviewed owner job supplies a trusted single-process backend under the
existing Compute collector lock. The job must bind reviewed source, separately
authorized private receipt creation and the opaque commitment; a digest or
environment match alone is not authorization. Only fixed public status crosses
this module; a missing backend or private grant remains closed.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import yaml
from backfill_market_data_tail import validate_dates

from scripts import market_data_store as store
from scripts.private_authority_input import _LocalGit
# The native coordinator also imports these short names from its scripts path.
import fixed_baseline_targets as fixed
import maintain_fixed_symbols as native

STOP = "PRIVATE_FIXED_TAIL_BLOCKED"
DONE = "PRIVATE_FIXED_TAIL_COMPLETE"
PRICE_ONLY = "PRIVATE_FIXED_TAIL_PRICE_ONLY_VOLUME_UNRESOLVED_NO_RETRY"
UNKNOWN = "PRIVATE_FIXED_TAIL_OUTCOME_UNKNOWN_NO_RETRY"
CUTOFF = "2026-11-20"
BASELINE = "2026-11-19"  # Adjacent calendar dates: no intervening append day.
SCOPE = ("AMD", "META", "MU", "NVDA", "ORCL", "QQQ", "SPCX", "SPY", "TME", "TSM")
LIMIT = len(SCOPE)
ET = ZoneInfo("America/New_York")
PROVIDER = "twelve_data_basic"
INDEX_PREFIX = fixed.INDEX_PREFIX
MAX_RECEIPT = 16 * 1024
KEYS = {"schema_version", "kind", "nonce", "compute_sha", "store_sha", "execution_date",
        "historical_cutoff", "retained_through", "credit_limit", "request_limit",
        "valid_from", "valid_until", "targets"}


def _need(condition):
    if not condition:
        raise ValueError()


def _hex(value, n):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % n, value) is not None


def _identity(env, compute_sha):
    _need(_hex(compute_sha, 40))
    expected = {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "yuatom/stock-market-data", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY_OWNER": "yuatom", "GITHUB_WORKFLOW_SHA": compute_sha, "GITHUB_SHA": compute_sha, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_ACTOR": "yuatom",
        "GITHUB_TRIGGERING_ACTOR": "yuatom",
        "GITHUB_WORKFLOW_REF": "yuatom/stock-market-data/.github/workflows/private-fixed-daily-tail.yml@refs/heads/main",
    }
    _need(all(env.get(k) == v for k, v in expected.items()))
    run_id = env.get("GITHUB_RUN_ID", "")
    _need(type(run_id) is str and re.fullmatch("[1-9][0-9]{0,19}", run_id))
    return run_id


def _utc(value):
    _need(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value))
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _window(receipt, t):
    _need(t.tzinfo is not None)
    t = t.astimezone(timezone.utc)
    start, until = (_utc(receipt[k]) for k in ("valid_from", "valid_until"))
    deadline = t + timedelta(seconds=1800)
    # Native runner still owns its clocks and per-request maturity. Reject
    # ahead of claim if that full allowance would reach a new ET ledger day.
    _need(start <= t and deadline <= until <= start + timedelta(days=1))
    local_date = t.astimezone(ET).date()
    _need(deadline.astimezone(ET).date() == local_date)
    _need(receipt["execution_date"] == local_date.isoformat())
    _need(receipt["execution_date"] > CUTOFF)
    _settled(receipt["execution_date"], t)


def _settled(execution_date, now):
    # Use the same reviewed source and settlement owner as the native runner.
    owner = Path(__file__).resolve().parents[1] / "config/market-data-store.yaml"
    daily = yaml.safe_load(owner.read_text(encoding="utf-8"))["collector"]["twelve_data_daily"]
    maturity = daily["settlement_maturity"]["minimum_next_day_et"]
    validate_dates(execution_date, CUTOFF, maturity, now)


def _grant(raw, compute_sha, now):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_RECEIPT)
    receipt = json.loads(raw.decode("ascii"))
    _need(type(receipt) is dict and set(receipt) == KEYS)
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    _need((canonical + "\n").encode("ascii") == raw)
    _need(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1)
    _need(receipt["kind"] == "private_fixed_tail_20261120" and receipt["compute_sha"] == compute_sha)
    _need(_hex(receipt["nonce"], 64) and receipt["nonce"] != "0" * 64
          and _hex(receipt["store_sha"], 40))
    _need(receipt["historical_cutoff"] == CUTOFF and receipt["retained_through"] == BASELINE)
    for key in ("request_limit", "credit_limit"):
        _need(type(receipt[key]) is int and receipt[key] == LIMIT)
    targets = receipt["targets"]
    _need(type(targets) is list and len(targets) == LIMIT)
    for target, symbol in zip(targets, SCOPE):
        _need(type(target) is dict and set(target) == {"symbol", "index_blob_sha"}
              and target["symbol"] == symbol and _hex(target["index_blob_sha"], 40))
    _window(receipt, now)
    return receipt


def _bound_series(repo, receipt):
    # A fresh, non-shared exact private materialization is a backend contract;
    # these checks reject worktree swaps/drift, wrong typed Git objects and shard
    # corruption independently before the existing controller can spend.
    native._clean(repo)
    sha = receipt["store_sha"]
    reader = _LocalGit(repo)
    _need(reader.read("rev-parse", "HEAD") == (sha + "\n").encode("ascii"))
    for target in receipt["targets"]:
        symbol = target["symbol"]
        rel = f"{INDEX_PREFIX}/{symbol}/_index.json"
        oid, raw = reader.blob(sha, rel)
        _need(oid == target["index_blob_sha"])
        _need(fixed._exact_file(repo, sha, rel, oid) == raw)
        index = json.loads(raw)
        _need(type(index) is dict and index.get("provider") == PROVIDER and index.get("symbol") == symbol
              and index.get("series_id") == f"{PROVIDER}:{symbol}:regular:1d"
              and index.get("series_semantics") == "daily_regular_ohlcv"
              and index.get("adjustment_semantics") == "provider_reported")
        shards = index.get("shards")
        _need(type(shards) is list and 1 <= len(shards) <= 40)
        for path in native.r.series_paths(repo, PROVIDER, symbol, index):
            # Verify typed Git membership; the native series reader verifies
            # every shard content digest and the complete chronological shape.
            approved_blob = reader.normal_blob_identity(sha, path)
            file = native.r.safe_path(repo, path)
            _need(file.stat().st_size <= 4 * 1024 * 1024)
            _need(store.git_blob_sha_bytes(file.read_bytes()) == approved_blob)
        bars = store.read_daily_series(repo / "data/market-data", provider=PROVIDER, symbol=symbol)
        _need(bars and bars[-1]["trade_date"] == BASELINE)
    native._clean(repo)
    _need(reader.read("rev-parse", "HEAD") == (sha + "\n").encode("ascii"))
    reader.assert_unmodified_graph()


def _preclaim_under_compute_lock(backend, repo, receipt):
    # Trusted caller runs in the Compute repository, NOT in the Store repo.
    # Require a fresh observation under the shared literal lock after verifying
    # the scoped histories. A matching private materialization alone cannot
    # rule out a newer EOD run or ledger exhaustion on actual remote main.
    state = backend.preclaim_state(repo=repo, store_sha=receipt["store_sha"],
                                   execution_date=receipt["execution_date"])
    _need(type(state) is dict and state.get("compute_repository") == "yuatom/stock-market-data"
          and state.get("lock_group") == "market-data-collector"
          and state.get("lock_held") is True and state.get("cancel_in_progress") is False
          and state.get("queue") == "max" and state.get("store_sha") == receipt["store_sha"])
    credits = state.get("available_credits")
    _need(type(credits) is int and LIMIT <= credits <= 200)


@contextmanager
def _quiet():
    # Match the pre-existing quiet private-route boundary at Python and child FD
    # levels. Use only in a dedicated single-thread process, never a worker pool.
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "w") as sink:
        restored = []
        try:
            for fd in (1, 2):
                restored.append((fd, os.dup(fd)))
                os.dup2(sink.fileno(), fd)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            for fd, previous in reversed(restored):
                os.dup2(previous, fd)
                os.close(previous)


def _candidate_status(result):
    # Native verifies actual child reports and publishes subsequent checkpoints.
    # This validates their output shape and binds the exact chain; it is NOT
    # durable evidence. The independent callback must read it back privately.
    _need(type(result) is dict and result.get("status") == "PASS")
    for key in ("requests_attempted", "budget_debits_confirmed", "published_count"):
        _need(type(result.get(key)) is int and result[key] == LIMIT)
    pending = result.get("budget_debits_unconfirmed")
    _need(type(pending) is int and pending == 0 and result.get("not_attempted_symbols") == []
          and result.get("automatic_retry_allowed") is False)
    tx = result.get("transactions")
    cursor = result.get("batch_manifest_commit_sha")
    _need(type(tx) is list and len(tx) == LIMIT and _hex(cursor, 40))
    unresolved = False
    for row, symbol in zip(tx, SCOPE):
        _need(type(row) is dict and row.get("symbol") == symbol and row.get("status") == "PASS"
              and row.get("store_start_sha") == cursor)
        commit, checkpoint = row.get("store_commit_sha"), row.get("checkpoint_commit_sha")
        _need(_hex(commit, 40) and _hex(checkpoint, 40) and len({cursor, commit, checkpoint}) == 3)
        for key in ("result_state_path", "checkpoint_path"):
            path = row.get(key)
            _need(type(path) is str and 0 < len(path) <= 512
                  and all(part not in ("", ".", "..") for part in path.split("/"))
                  and re.fullmatch(r"[a-zA-Z0-9_./-]+", path))
        volume = row.get("volume_policy")
        _need(volume in ("PRESERVED_PROVIDER_VOLUME", "BLOCKED_UNRESOLVED"))
        unresolved = unresolved or volume == "BLOCKED_UNRESOLVED"
        cursor = checkpoint
    _need(result.get("last_verified_commit_sha") == cursor)
    return PRICE_ONLY if unresolved else DONE


@dataclass(frozen=True, repr=False)
class ClaimHandoff:
    approval_store_sha: str
    operational_store_sha: str


def run_profile(*, env, compute_sha, expected_commitment, backend=None, now=None):
    """Run a verified hosted backend; no default and no public private inputs.

    The hosted backend authenticates the standard salted receipt and exact Git
    descriptor before returning private canonical grant bytes. Its transports
    never use caller-supplied private input. A backend is mandatory.
    Backend methods: fetch_private_grant(commitment, compute_sha) -> grant bytes;
    materialize_exact_store(pinned_sha) -> fresh exact checkout;
    preclaim_state(...) -> privacy-verified fresh Store main and budget evidence
    observed while holding the actual shared Compute collector lock, not a
    local flag or an identically named Store-repository concurrency group;
    claim_once(commitment, run_id) -> typed old/new handoff after durable claim;
    invoke_native(**validated_scope) -> private unchanged maintain_append result;
    verify_native_receipts(...) -> exactly True only after independent private
    readback of all exact Store receipt/child/checkpoint scopes, budget evidence
    and native price/volume dispositions; the hosted backend supplies it.
    Clock override is for offline tests only. No private fields are returned.
    """
    possibly_spent = False
    try:
        if backend is None:
            return STOP
        run_id = _identity(env, compute_sha)
        _need(_hex(expected_commitment, 64))
        clock = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
        with _quiet():
            current = clock()
            _settled(current.astimezone(ET).date().isoformat(), current)
            raw = backend.fetch_private_grant(expected_commitment, compute_sha)
            receipt = _grant(raw, compute_sha, clock())
            _need(callable(backend.verify_native_receipts))
            repo = Path(backend.materialize_exact_store(receipt["store_sha"])).resolve(strict=True)
            _bound_series(repo, receipt)  # EOD-first retained target fails before claim.
            _window(receipt, clock())
            _preclaim_under_compute_lock(backend, repo, receipt)
            _window(receipt, clock())
            # Claim by commitment, not GitHub run id alone, to deny another
            # first-attempt job reusing the same approval. A negative/unknown
            # claim may refer to an already spent grant. Never assert zero.
            possibly_spent = True
            handoff = backend.claim_once(expected_commitment, run_id)
            _need(type(handoff) is ClaimHandoff and handoff.approval_store_sha == receipt["store_sha"]
                  and _hex(handoff.operational_store_sha, 40)
                  and handoff.operational_store_sha != handoff.approval_store_sha)
            _window(receipt, clock())
            # Same approved index SHAs are re-checked by maintain_append before
            # its plan write and for every pending symbol after earlier children.
            result = backend.invoke_native(compute_sha=compute_sha, repo=repo,
                store_sha=handoff.operational_store_sha, execution_date=receipt["execution_date"],
                historical_cutoff=CUTOFF, request_id=f"{run_id}-1",
                fixed_selector={"schema_version": 1, "targets": receipt["targets"]})
            status = _candidate_status(result)
            _need(backend.verify_native_receipts(result=result, compute_sha=compute_sha,
                store_sha=handoff.operational_store_sha, execution_date=receipt["execution_date"],
                historical_cutoff=CUTOFF, request_id=f"{run_id}-1") is True)
        return status
    except KeyboardInterrupt:
        # Cancellation still reaches the parent as cancellation, not a status.
        # Strip a backend exception's arbitrary/private arguments.
        raise KeyboardInterrupt() from None
    except (Exception, SystemExit):
        # SystemExit can otherwise leak its argument after _quiet restores the
        # public file descriptors. Do not reinterpret true parent cancellation.
        return UNKNOWN if possibly_spent else STOP
