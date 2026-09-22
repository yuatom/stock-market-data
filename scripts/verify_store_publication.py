#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_PREFIX = "refs/tags/market-data-read/"


class StorePublicationError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise StorePublicationError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def _one_remote_ref(root: Path, remote: str, ref: str) -> str | None:
    result = _git(root, "ls-remote", remote, ref)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2:
        raise StorePublicationError(f"unexpected remote ref result for {ref}")
    sha, returned_ref = rows[0]
    if returned_ref != ref or not SHA_RE.fullmatch(sha):
        raise StorePublicationError(f"invalid remote ref result for {ref}")
    return sha


def _changed_paths(root: Path, commit: str) -> list[tuple[str, str]]:
    result = _git(root, "diff-tree", "--no-commit-id", "--name-status", "-r", commit)
    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise StorePublicationError(f"unparseable changed-path row: {line}")
        status = parts[0]
        path = parts[-1]
        if not path.startswith("data/market-data/"):
            raise StorePublicationError(f"Store publication changed out-of-scope path: {path}")
        rows.append((status, path))
    return rows


def _read_json_at_commit(root: Path, commit: str, path: str) -> dict[str, Any]:
    raw = _git(root, "show", f"{commit}:{path}").stdout
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorePublicationError(f"invalid JSON at {path}") from exc
    if not isinstance(value, dict):
        raise StorePublicationError(f"{path} must contain an object")
    return value


def _verify_required_manifest(root: Path, commit: str, manifest_path: str) -> list[dict[str, str]]:
    if not manifest_path.startswith("data/market-data/collector-state/"):
        raise StorePublicationError("required output manifest is outside collector-state")
    manifest = _read_json_at_commit(root, commit, manifest_path)
    if manifest.get("schema_version") != 1:
        raise StorePublicationError("required output manifest schema_version must be 1")
    for field in ("request_id", "trade_date", "stage"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise StorePublicationError(f"required output manifest missing {field}")
    required_outputs = manifest.get("required_outputs")
    if not isinstance(required_outputs, list) or not required_outputs:
        raise StorePublicationError("required output manifest required_outputs must be non-empty")
    paths: list[str] = []
    verified: list[dict[str, str]] = []
    for item in required_outputs:
        if not isinstance(item, dict) or set(item) != {"path", "blob_sha"}:
            raise StorePublicationError("required output manifest item must contain path and blob_sha")
        relative = item.get("path")
        expected_blob = item.get("blob_sha")
        if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise StorePublicationError("required output manifest contains invalid path")
        if not isinstance(expected_blob, str) or not SHA_RE.fullmatch(expected_blob):
            raise StorePublicationError("required output manifest contains invalid blob_sha")
        paths.append(relative)
        full_path = f"data/market-data/{relative}"
        exists = _git(root, "cat-file", "-e", f"{commit}:{full_path}", check=False)
        if exists.returncode != 0:
            raise StorePublicationError(f"required output missing at Store commit: {full_path}")
        blob_sha = _git(root, "rev-parse", f"{commit}:{full_path}").stdout.strip()
        if blob_sha != expected_blob:
            raise StorePublicationError(f"required output blob mismatch at Store commit: {full_path}")
        verified.append({"path": full_path, "blob_sha": blob_sha})
    if len(paths) != len(set(paths)):
        raise StorePublicationError("required output manifest paths must be unique")
    return verified


def verify_publication(
    root: Path,
    expected_commit: str,
    *,
    remote: str = "origin",
    status: str = "store_persisted",
    required_manifest: str | None = None,
) -> dict[str, object]:
    if not SHA_RE.fullmatch(expected_commit):
        raise StorePublicationError("expected commit must be 40-char lowercase hex")

    local_head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if local_head != expected_commit:
        raise StorePublicationError(
            f"local HEAD {local_head} does not match expected commit {expected_commit}"
        )

    _git(root, "fetch", "--no-tags", remote, "main")
    remote_main = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    if remote_main != expected_commit:
        raise StorePublicationError(
            f"remote main {remote_main} does not equal pushed commit {expected_commit}"
        )

    _git(root, "cat-file", "-e", f"{remote_main}^{{commit}}")
    changed = _changed_paths(root, remote_main)
    verified_blob_count = 0
    for change_status, path in changed:
        if change_status.startswith("D"):
            continue
        local_blob = _git(root, "rev-parse", f"{expected_commit}:{path}").stdout.strip()
        remote_blob = _git(root, "rev-parse", f"{remote_main}:{path}").stdout.strip()
        if local_blob != remote_blob or not SHA_RE.fullmatch(remote_blob):
            raise StorePublicationError(f"remote blob identity mismatch for {path}")
        verified_blob_count += 1

    verified_required_outputs = (
        _verify_required_manifest(root, remote_main, required_manifest)
        if required_manifest
        else []
    )

    durable_ref = f"{REF_PREFIX}{expected_commit}"
    existing = _one_remote_ref(root, remote, durable_ref)
    if existing is not None and existing != expected_commit:
        raise StorePublicationError(
            f"durable ref {durable_ref} already targets {existing}, expected {expected_commit}"
        )
    if existing is None:
        _git(root, "push", remote, f"{expected_commit}:{durable_ref}")

    readback = _one_remote_ref(root, remote, durable_ref)
    if readback != expected_commit:
        raise StorePublicationError(
            f"durable ref readback {readback!r} does not equal {expected_commit}"
        )

    return {
        "status": "durability_verified",
        "writer_status": status,
        "store_commit_sha": expected_commit,
        "durability_ref": durable_ref,
        "remote_main_sha": remote_main,
        "verified_changed_paths": len(changed),
        "verified_non_deleted_blob_paths": verified_blob_count,
        "verified_required_outputs": verified_required_outputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", default=".")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--writer-status", default="store_persisted")
    parser.add_argument("--required-manifest")
    args = parser.parse_args()

    receipt = verify_publication(
        Path(args.store_root),
        args.expected_commit,
        remote=args.remote,
        status=args.writer_status,
        required_manifest=args.required_manifest,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
