"""Read-only, request-bound verification of durable fixed-tail native results."""
import json
import re
from datetime import date

import maintain_fixed_symbols as native

HEX40 = re.compile(r"[0-9a-f]{40}")
VOL = {"PRESERVED_PROVIDER_VOLUME", "BLOCKED_UNRESOLVED"}


def _need(ok):
    if not ok:
        raise ValueError("PRIVATE_FIXED_TAIL_READBACK_INVALID")


def _sha(value):
    _need(type(value) is str and HEX40.fullmatch(value) is not None)
    return value


def _matches(value, expected):
    _need(type(value) is dict and all(value.get(k) == v for k, v in expected.items()))


def _object(repo, sha, path):
    _sha(sha)
    native._immutable_blob(repo, sha, path)
    return json.loads(native.a.git(repo, "show", f"{sha}:{path}"))


def verify(*, h, repo, result, compute_sha, store_sha, execution_date,
           historical_cutoff, request_id, fixed_selector):
    """Return True only after checking Store; locators are not treated as evidence.

    The Store must be a private checkout bound to the exact origin, with the
    official shared Compute lock held. Caller passes the verified claim-only
    operational head, not old authority preimage. No writes, fetches, retry,
    resume or provider calls here. Errors are suppressed by the quiet caller.
    """
    _sha(compute_sha)
    _sha(store_sha)
    for day in (execution_date, historical_cutoff):
        _need(type(day) is str and date.fromisoformat(day).isoformat() == day)
    _need(type(request_id) is str and re.fullmatch(r"[1-9][0-9]{0,19}-1", request_id))
    targets = fixed_selector["targets"]
    _need(fixed_selector == {"schema_version": 1, "targets": targets} and len(targets) == 10)
    for target in targets:
        _need(set(target) == {"symbol", "index_blob_sha"} and
              re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", target["symbol"]))
        _sha(target["index_blob_sha"])
    _need(len({t["symbol"] for t in targets}) == len(targets))
    prefix = f"{native.r.PREFIX}collector-state/{execution_date[:7]}/{execution_date}"
    budget = f"{prefix}-twelve-data-budget.json"
    plan_path = f"{prefix}-daily-symbol-maintenance-{request_id}.json"
    common = dict(compute_sha=compute_sha, store_start_sha=store_sha, execution_date=execution_date,
                  historical_cutoff=historical_cutoff, request_id=request_id, automatic_retry_allowed=False)
    _matches(result, dict(common, status="PASS", batch_manifest_path=plan_path,
                          requests_attempted=10, budget_debits_confirmed=10,
                          budget_debits_unconfirmed=0, published_count=10,
                          not_attempted_symbols=[], quarantined_symbols=[], stop=None))
    plan_sha = _sha(result["batch_manifest_commit_sha"])
    cursor = plan_sha
    final = _sha(result["last_verified_commit_sha"])
    native._remote_exact(repo, final)
    native._metadata_proof(repo, plan_sha, store_sha, plan_path, execution_date, request_id)
    _matches(_object(repo, plan_sha, plan_path),
             dict(common, mode=native.MODE, trade_date=execution_date, kind="append_only_batch_plan",
                  status="PLANNED", fixed_selection=fixed_selector, maximum_provider_requests=10,
                  overall_timeout_seconds=1800, volume_policy_scope="per_symbol", transactions=[],
                  requests_attempted=0, budget_debits_confirmed=0, budget_debits_unconfirmed=0,
                  published_count=0, last_verified_commit_sha=store_sha, stop=None))
    rows = result["transactions"]
    _need(type(rows) is list and len(rows) == len(targets))
    passed = set()
    prefix_rows = []
    initial = native._immutable_budget(repo, store_sha, budget)
    for count, (target, row) in enumerate(zip(targets, rows), start=1):
        symbol = target["symbol"]
        child_path = f"{prefix}-daily-history-tail-backfill-{request_id}-{symbol}.json"
        checkpoint_path = f"{prefix}-daily-symbol-maintenance-{request_id}-{symbol}.json"
        _matches(row, dict(symbol=symbol, status="PASS", store_start_sha=cursor,
                           result_state_path=child_path, checkpoint_path=checkpoint_path))
        child, checkpoint = _sha(row["store_commit_sha"]), _sha(row["checkpoint_commit_sha"])
        before = native._immutable_budget(repo, cursor, budget)
        # The exact terminal ledger supplies locators only. Native then
        # independently re-derives the expected output set and exact blobs.
        outputs = _object(repo, child, budget)["required_outputs"]
        _need(type(outputs) is list and 0 < len(outputs) <= 64)
        full_outputs = []
        for output in outputs:
            _need(type(output) is dict and set(output) == {"path", "blob_sha"})
            relative = output["path"]
            _need(type(relative) is str and len(relative) <= 512 and
                  re.fullmatch(r"[A-Za-z0-9_./-]+", relative) and
                  all(part not in {"", ".", ".."} for part in relative.split("/")))
            full_outputs.append({"path": native.r.PREFIX + relative, "blob_sha": _sha(output["blob_sha"])})
        passed.add(symbol)
        durable = native._verify_append_terminal(
            h, repo, start=cursor, sha=child, path=child_path, day=execution_date,
            cutoff=historical_cutoff, request_id=request_id, symbol=symbol,
            selector={"schema_version": 1, "targets": [target]}, before=before,
            remaining=targets, passed=passed, compute_sha=compute_sha,
            result={"status": "PASS", "appended_count": 1, "verified_required_outputs": full_outputs},
        )
        summary = durable["results"][0]
        _need(summary["new_records"] == 1 and summary["last_trade_date"] == historical_cutoff and
              summary["volume_policy"] in VOL and row["volume_policy"] == summary["volume_policy"])
        native._metadata_proof(repo, checkpoint, child, checkpoint_path, execution_date, request_id)
        projected = {k:v for k,v in row.items() if k not in {"checkpoint_commit_sha", "checkpoint_path"}}
        _matches(_object(repo, checkpoint, checkpoint_path),
                 dict(common, mode=native.MODE, trade_date=execution_date,
                      kind="append_only_batch_checkpoint", status="PROGRESS_VALIDATED",
                      batch_manifest_commit_sha=plan_sha, batch_manifest_path=plan_path,
                      transactions=prefix_rows + [projected], last_verified_commit_sha=child,
                      requests_attempted=count, budget_debits_confirmed=count,
                      budget_debits_unconfirmed=0, published_count=count, quarantined_symbols=[], stop=None))
        prefix_rows.append(row)
        cursor = checkpoint
    _need(cursor == final and native._immutable_budget(repo, cursor, budget)["credits"] == initial["credits"] + len(targets))
    native._remote_exact(repo, final)
    return True
