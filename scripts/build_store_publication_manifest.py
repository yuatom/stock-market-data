#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
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
    manifest = (
        store_root
        / "collector-state"
        / trade_date[:7]
        / f"{trade_date}-{stage}-{token}-publication.json"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": stable_request_id,
                "trade_date": trade_date,
                "stage": stage,
                "required_outputs": refs,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
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
