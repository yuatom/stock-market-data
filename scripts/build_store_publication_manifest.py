#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

DECLARED_PATH_KEYS = {"snapshot_path", "result_state_path", "probe_path"}


class PublicationManifestError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PublicationManifestError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()


def _last_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _declared_paths(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in DECLARED_PATH_KEYS and isinstance(item, str) and item:
                found.add(item)
            else:
                found.update(_declared_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_declared_paths(item))
    return found


EOD_HOST_REPOSITORY = "yuatom/stock-market-data"
EOD_OWNER_WORKFLOW = "Market Data Collector"
EOD_MAIN_REF = "refs/heads/main"
EOD_OWNER_WORKFLOW_REF_PREFIX = EOD_HOST_REPOSITORY + "/.github/workflows/market-data-collector.yml@"
EOD_CONTEXT = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": EOD_HOST_REPOSITORY,
    "GITHUB_REPOSITORY_OWNER": "yuatom",
    "GITHUB_WORKFLOW": EOD_OWNER_WORKFLOW,
}
EOD_VARIABLES = {
    *EOD_CONTEXT, "GITHUB_EVENT_NAME", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
    "GITHUB_REF", "GITHUB_REF_TYPE", "GITHUB_WORKFLOW_REF",
}
EOD_INVOCATION_RE = re.compile(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,8}")


def _eod_scope(stage: str) -> tuple[str, str] | None:
    """The trusted caller for formal EOD is the existing GitHub workflow.

    The workflow and Compute checkout can have different source revisions.
    Only the existing caller context and diagnostic name the current run;
    no new CLI flags are needed or accepted.
    """
    if stage != "previous_session_eod":
        return None
    if not any(key in os.environ for key in EOD_VARIABLES):
        return None
    if any(os.environ.get(key) != value for key, value in EOD_CONTEXT.items()):
        raise PublicationManifestError("ordinary EOD hosted context not verified")
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    invocation = f"{run}-{attempt}"
    trigger = os.environ.get("GITHUB_EVENT_NAME", "")
    if (not EOD_INVOCATION_RE.fullmatch(invocation)
            or trigger not in {"schedule", "workflow_dispatch"}):
        raise PublicationManifestError("invalid EOD manifest invocation")
    ref = os.environ.get("GITHUB_REF", "")
    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    prefix = {"branch": "refs/heads/", "tag": "refs/tags/"}.get(ref_type)
    if (not prefix or not ref.startswith(prefix)
            or os.environ.get("GITHUB_WORKFLOW_REF") != EOD_OWNER_WORKFLOW_REF_PREFIX + ref
            or (trigger == "schedule" and ref != EOD_MAIN_REF)):
        raise PublicationManifestError("ordinary EOD caller ref not verified")
    # Never use a branch or tag name as a local path or shell interpolation.
    # Match the same native ref space already supported by workflow_dispatch.
    if subprocess.run(["git", "check-ref-format", ref], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, check=False).returncode:
        raise PublicationManifestError("ordinary EOD caller ref is invalid")
    return invocation, trigger


def build_manifest(
    *,
    store_root: Path,
    result_file: Path,
    request_id: str,
    trade_date: str,
    stage: str,
) -> Path | None:
    store_root = store_root.resolve()
    repo_root = Path(_git(store_root, "rev-parse", "--show-toplevel").strip()).resolve()
    data_root = repo_root / "data" / "market-data"
    if store_root != data_root:
        raise PublicationManifestError("store_root must equal repository data/market-data root")

    scope = _eod_scope(stage)
    if scope is not None:
        eod_invocation_id, eod_trigger = scope
        try:
            normalized_day = date.fromisoformat(trade_date).isoformat()
        except ValueError as exc:
            raise PublicationManifestError("invalid EOD manifest date") from exc
        if normalized_day != trade_date:
            raise PublicationManifestError("invalid EOD manifest date")

    result = _last_json(result_file)
    if stage == "previous_session_eod" and not result.get("result_state_path"):
        raise PublicationManifestError("ordinary EOD result is missing required private diagnostic output")
    if stage == "previous_session_eod":
        relative = result["result_state_path"]
        path_pattern = (
            rf"collector-state/{re.escape(trade_date[:7])}/"
            rf"{re.escape(trade_date)}-previous_session_eod-[0-9a-f]{{16}}-conflicts\.json"
        )
        if not isinstance(relative, str) or not re.fullmatch(path_pattern, relative):
            raise PublicationManifestError("ordinary EOD diagnostic path is invalid")
        diagnostic_path = store_root / relative
        try:
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationManifestError("ordinary EOD diagnostic is missing or invalid JSON") from exc
        if not isinstance(diagnostic, dict):
            raise PublicationManifestError("ordinary EOD diagnostic is missing required evidence")
        invocation = diagnostic.get("invocation") if isinstance(diagnostic, dict) else None
        conflicts = diagnostic.get("conflicts") if isinstance(diagnostic, dict) else None
        counts_valid = (
            isinstance(diagnostic, dict)
            and isinstance(diagnostic.get("failure_target_count"), int)
            and isinstance(diagnostic.get("conflict_record_count"), int)
            and isinstance(diagnostic.get("conflict_diagnostic_record_count"), int)
            and isinstance(conflicts, list)
            and diagnostic["conflict_diagnostic_record_count"] == len(conflicts)
            and diagnostic["conflict_record_count"] >= len(conflicts)
            and diagnostic.get("conflict_diagnostics_truncated")
                == (diagnostic["conflict_record_count"] > len(conflicts))
        )
        identity_valid = (
            isinstance(invocation, dict)
            and invocation.get("mode") == "previous_session_eod"
            and isinstance(invocation.get("invocation_id"), str)
            and bool(invocation["invocation_id"])
            and all(re.fullmatch(r"[0-9a-f]{40}", str(diagnostic.get(key) or ""))
                    for key in ("compute_sha", "store_input_sha"))
        )
        if (diagnostic.get("schema_version") != 1
                or diagnostic.get("diagnostic_authority") != "non_authoritative_conflict_evidence"
                or diagnostic.get("trade_date") != trade_date
                or not counts_valid or not identity_valid):
            raise PublicationManifestError("ordinary EOD diagnostic is missing required evidence")
        expected_token = hashlib.sha256(invocation["invocation_id"].encode("utf-8")).hexdigest()[:16]
        if not relative.endswith(f"{expected_token}-conflicts.json"):
            raise PublicationManifestError("ordinary EOD diagnostic path does not match invocation identity")
        if scope is not None:
            run, attempt = eod_invocation_id.split("-")
            expected_invocation = {
                "invocation_id": eod_invocation_id,
                "run_id": run,
                "run_attempt": attempt,
                "event": eod_trigger,
                "repository": EOD_HOST_REPOSITORY,
                "workflow": EOD_OWNER_WORKFLOW,
            }
            if any(invocation.get(key) != value for key, value in expected_invocation.items()):
                raise PublicationManifestError("ordinary EOD diagnostic does not bind this run")
    declared: set[str] = set()
    for path in _declared_paths(result):
        if path.startswith("data/market-data/"):
            path = path[len("data/market-data/") :]
        candidate = store_root / path
        if not candidate.is_file():
            raise PublicationManifestError(f"producer declared output does not exist: {path}")
        declared.add(path)

    changed = _git(
        repo_root,
        "ls-files",
        "--modified",
        "--others",
        "--exclude-standard",
        "--",
        "data/market-data",
    )
    actual: set[str] = set()
    for full in changed.splitlines():
        full = full.strip()
        if not full or not full.startswith("data/market-data/"):
            continue
        relative = full[len("data/market-data/") :]
        candidate = store_root / relative
        if candidate.is_file():
            actual.add(relative)

    outputs = sorted(actual | declared)
    if not outputs:
        return None

    refs = []
    for relative in outputs:
        raw = (store_root / relative).read_bytes()
        refs.append({"path": relative, "blob_sha": _git_blob_sha(raw)})

    stable_request_id = request_id.strip() or f"scheduled-{trade_date}-{stage}"
    token = hashlib.sha256(stable_request_id.encode("utf-8")).hexdigest()[:16]
    directory = store_root / "collector-state" / trade_date[:7]
    legacy = directory / f"{trade_date}-{stage}-{token}-publication.json"

    def document(identity: str) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": identity,
                    "trade_date": trade_date,
                    "stage": stage,
                    "required_outputs": refs,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    directory.mkdir(parents=True, exist_ok=True)
    if scope is None:
        if stage == "previous_session_eod":
            try:
                with legacy.open("xb") as stream:
                    stream.write(document(stable_request_id))
            except FileExistsError as exc:
                raise PublicationManifestError("legacy EOD manifest already exists") from exc
        else:
            legacy.write_bytes(document(stable_request_id))
        return legacy

    # Reserve the returned per-run path before touching the legacy locator.
    # The same single-writer queue already owns this private checkout.
    manifest = directory / f"{trade_date}-{stage}-run-{eod_invocation_id}-publication.json"
    try:
        stream = manifest.open("xb")
    except FileExistsError as exc:
        raise PublicationManifestError("this EOD run manifest already exists") from exc
    new_legacy = False
    try:
        with stream:
            # Preserve the old date locator once for the scheduled lane. It is
            # compatible at its original commit, never proof of a later run.
            if eod_trigger == "schedule":
                legacy_bytes = document(stable_request_id)
                try:
                    compat = legacy.open("xb")
                except FileExistsError:
                    if legacy.is_symlink() or not legacy.is_file():
                        raise PublicationManifestError("legacy EOD locator is not a normal file")
                else:
                    new_legacy = True
                    with compat:
                        compat.write(legacy_bytes)
                    refs.append({"path": legacy.relative_to(store_root).as_posix(),
                                 "blob_sha": _git_blob_sha(legacy_bytes)})
                    refs.sort(key=lambda ref: ref["path"])
            stream.write(document(f"eod-run-{eod_invocation_id}"))
    except Exception:
        # Both paths are scratch files this invocation created exclusively.
        # A failed build cannot leave either one as an intended publication.
        manifest.unlink()
        if new_legacy:
            legacy.unlink()
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stage", required=True)
    args = parser.parse_args()
    manifest = build_manifest(
        store_root=Path(args.store_root),
        result_file=Path(args.result_file),
        request_id=args.request_id,
        trade_date=args.trade_date,
        stage=args.stage,
    )
    if manifest is None:
        print("")
    else:
        print(manifest.relative_to(Path(args.store_root).resolve()).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
