#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts.push_proof_store_commit import transient
except ModuleNotFoundError:
    from push_proof_store_commit import transient


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_PREFIX = "refs/tags/market-data-read/"
# Implementation bounds; kept aligned with the publication transport owner by tests.
REMOTE_ATTEMPTS = 3
REMOTE_COMMAND_TIMEOUT_SECONDS = 30
REMOTE_BUDGET_SECONDS = 120
REMOTE_BACKOFF_SECONDS = (1, 3)


class StorePublicationError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True, capture_output=True, check=False,
            timeout=REMOTE_COMMAND_TIMEOUT_SECONDS,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never"),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StorePublicationError("STORE_LOCAL_GIT_UNAVAILABLE") from None
    if check and result.returncode != 0:
        # A raw Git error can contain credential URLs or private payload values.
        raise StorePublicationError("STORE_LOCAL_GIT_FAILED")
    return result


class _PublicationTransport:
    """One bounded verification attempt; never switch commit or retry a whole producer."""

    def __init__(self, root: Path):
        self.root = root
        self.deadline = time.monotonic() + REMOTE_BUDGET_SECONDS

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StorePublicationError("STORE_PUBLICATION_TRANSPORT_DEADLINE")
        return remaining

    def backoff(self, attempt: int) -> None:
        if attempt:
            delay = REMOTE_BACKOFF_SECONDS[attempt - 1]
            if delay >= self.remaining():
                raise StorePublicationError("STORE_PUBLICATION_TRANSPORT_DEADLINE")
            time.sleep(delay)

    def once(self, *args: str) -> subprocess.CompletedProcess[str]:
        timeout = min(REMOTE_COMMAND_TIMEOUT_SECONDS, self.remaining())
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), *args], text=True,
                capture_output=True, check=False, timeout=timeout,
                env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never"),
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess(args, 124, "", "git transport timeout")
        except OSError:
            raise StorePublicationError("STORE_PUBLICATION_EXECUTOR_UNAVAILABLE") from None
        self.remaining()
        return result

    def read(self, *args: str) -> subprocess.CompletedProcess[str]:
        for attempt in range(REMOTE_ATTEMPTS):
            self.backoff(attempt)
            result = self.once(*args)
            if result.returncode == 0:
                return result
            if not transient(result):
                raise StorePublicationError("STORE_PUBLICATION_REMOTE_READ_REJECTED")
        raise StorePublicationError("STORE_PUBLICATION_REMOTE_READ_EXHAUSTED")


def _one_remote_ref(
    root: Path, remote: str, ref: str,
    *, transport: _PublicationTransport | None = None,
) -> str | None:
    transport = transport or _PublicationTransport(root)
    result = transport.read("ls-remote", remote, ref)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2:
        raise StorePublicationError(f"unexpected remote ref result for {ref}")
    sha, returned_ref = rows[0]
    if returned_ref != ref or not SHA_RE.fullmatch(sha):
        raise StorePublicationError(f"invalid remote ref result for {ref}")
    return sha


def _ensure_retention_ref(
    root: Path, remote: str, expected_commit: str, durable_ref: str,
    transport: _PublicationTransport,
) -> None:
    def require_identity() -> None:
        if _git(root, "rev-parse", "HEAD").stdout.strip() != expected_commit:
            raise StorePublicationError("STORE_PUBLICATION_CANDIDATE_CHANGED")
        main = _one_remote_ref(root, remote, "refs/heads/main", transport=transport)
        if main != expected_commit:
            raise StorePublicationError("STORE_PUBLICATION_REMOTE_MAIN_CHANGED")

    def read_tag() -> str | None:
        observed = _one_remote_ref(root, remote, durable_ref, transport=transport)
        if observed is not None and observed != expected_commit:
            raise StorePublicationError(
                f"durable ref {durable_ref} already targets {observed}, expected {expected_commit}"
            )
        return observed

    for attempt in range(REMOTE_ATTEMPTS):
        transport.backoff(attempt)
        require_identity()
        if read_tag() == expected_commit:
            require_identity()
            return
        # Retry only after fresh exact main and tag-absence observations.
        # Git rejects tag retargeting: never force, integrate, rebuild or delete.
        if _git(root, "rev-parse", "HEAD").stdout.strip() != expected_commit:
            raise StorePublicationError("STORE_PUBLICATION_CANDIDATE_CHANGED")
        pushed = transport.once("push", remote, f"{expected_commit}:{durable_ref}")
        # A lost acknowledgement is reconciled by the actual remote ref, not
        # by the push exit status. Unreadable readback forbids another write.
        if read_tag() == expected_commit:
            require_identity()
            return
        if pushed.returncode != 0 and not transient(pushed):
            raise StorePublicationError("STORE_PUBLICATION_TAG_PUSH_REJECTED")
    raise StorePublicationError("STORE_PUBLICATION_TAG_READBACK_UNCONFIRMED")


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

    transport = _PublicationTransport(root)
    # Observe the exact branch ref once, then fetch only that verified immutable
    # object. A second floating-main resolution can describe a different moment.
    remote_main = _one_remote_ref(root, remote, "refs/heads/main", transport=transport)
    if remote_main != expected_commit:
        raise StorePublicationError(
            f"remote main {remote_main} does not equal pushed commit {expected_commit}"
        )
    transport.read(
        "fetch", "--no-tags", "--no-recurse-submodules", "--write-fetch-head",
        remote, expected_commit,
    )
    fetched_commit = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    if fetched_commit != expected_commit:
        raise StorePublicationError("STORE_PUBLICATION_FETCHED_COMMIT_MISMATCH")
    # Neither a successful fetch nor a prior matching ref authorizes a changed
    # local candidate or remote branch. Stop before output/tag processing.
    if _git(root, "rev-parse", "HEAD").stdout.strip() != expected_commit:
        raise StorePublicationError("STORE_PUBLICATION_CANDIDATE_CHANGED")
    if _one_remote_ref(root, remote, "refs/heads/main", transport=transport) != expected_commit:
        raise StorePublicationError("STORE_PUBLICATION_REMOTE_MAIN_CHANGED")

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
    _ensure_retention_ref(root, remote, expected_commit, durable_ref, transport)
    transport.remaining()

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

    try:
        receipt = verify_publication(
            Path(args.store_root),
            args.expected_commit,
            remote=args.remote,
            status=args.writer_status,
            required_manifest=args.required_manifest,
        )
    except StorePublicationError as exc:
        print(json.dumps({"status": "store_publication_failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
