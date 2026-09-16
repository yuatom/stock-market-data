#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


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


def verify_publication(
    root: Path,
    expected_commit: str,
    *,
    remote: str = "origin",
    status: str = "store_persisted",
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
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", default=".")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--writer-status", default="store_persisted")
    args = parser.parse_args()

    receipt = verify_publication(
        Path(args.store_root),
        args.expected_commit,
        remote=args.remote,
        status=args.writer_status,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
