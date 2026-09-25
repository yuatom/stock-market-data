#!/usr/bin/env python3
"""Push one proof-bearing Store commit without changing its identity.

This is transport acknowledgement only. verify_store_publication.py must still
establish remote object/output closure and the durable retention ref afterwards.
No provider access, proof rebuilding, integration, or history rewrite is allowed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable

SHA = re.compile(r"[0-9a-f]{40}\Z")
REMOTE_REF = "refs/heads/main"


class StorePushError(RuntimeError):
    """An unsatisfied publication boundary, not a market-data quality verdict."""


def transient(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stderr or "").lower()
    if any(x in text for x in ("permission denied", "authentication failed", "protected branch", "pre-receive hook declined", "non-fast-forward", "repository not found", "error: 401", "error: 403")):
        return False
    return result.returncode == 124 or any(x in text for x in (
        "internal server error", "bad gateway", "service unavailable", "gateway timeout",
        "error: 500", "error: 502", "error: 503", "error: 504", "connection reset",
        "connection timed out", "operation timed out", "could not resolve host",
        "temporary failure in name resolution", "remote end hung up unexpectedly",
    ))


def git_command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=30, check=False, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "git transport timeout")
    except OSError as exc:
        raise StorePushError("STORE_LOCAL_EXECUTION_UNAVAILABLE") from exc


def push_exact(root: Path, base: str, candidate: str, *, attempts: int = 3,
               run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
               sleep: Callable[[float], None] = time.sleep) -> dict:
    if not SHA.fullmatch(base) or not SHA.fullmatch(candidate) or base == candidate:
        raise StorePushError("STORE_INVALID_EXACT_IDENTITY")
    if not 1 <= attempts <= 3:
        raise StorePushError("STORE_INVALID_RETRY_BOUND")
    execute = run or (lambda *args: git_command(root, *args))

    def local_value(*args: str) -> str:
        result = execute(*args)
        if result.returncode != 0:
            raise StorePushError("STORE_LOCAL_IDENTITY_UNREADABLE")
        return result.stdout.strip()

    def require_unchanged() -> None:
        if local_value("rev-parse", "HEAD") != candidate:
            raise StorePushError("STORE_CANDIDATE_CHANGED")
        if local_value("status", "--porcelain", "--untracked-files=all"):
            raise StorePushError("STORE_DIRTY_WORKTREE")

    def remote_value(result: subprocess.CompletedProcess[str]) -> str:
        # Exact output shape: no missing ref, multiple matches, or symbolic ref.
        fields = result.stdout.strip().split()
        if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] != REMOTE_REF:
            raise StorePushError("STORE_REMOTE_IDENTITY_INVALID")
        return fields[0]

    def check_remote(sha: str) -> bool:
        if sha == candidate:
            require_unchanged()
            return True
        if sha != base:
            raise StorePushError("STORE_REMOTE_MAIN_ADVANCED")
        return False

    def success(pushes: int) -> dict:
        return {"status": "store_push_verified", "store_commit_sha": candidate,
                "remote_main_sha": candidate, "push_attempts": pushes,
                "durability_status": "NOT_CHECKED"}

    require_unchanged()
    parents = local_value("rev-list", "--parents", "-n", "1", candidate).split()
    if parents != [candidate, base]:
        raise StorePushError("STORE_CANDIDATE_NOT_SINGLE_BASE_CHILD")
    changed = execute("diff", "--name-only", "-z", base, candidate)
    if changed.returncode != 0:
        raise StorePushError("STORE_DIFF_UNREADABLE")
    paths = [p for p in changed.stdout.split("\0") if p]
    if not paths or any(not p.startswith("data/market-data/") for p in paths):
        raise StorePushError("STORE_WRITE_SCOPE_INVALID")

    pushes = 0
    last_error = "STORE_TRANSPORT_RETRIES_EXHAUSTED"
    for attempt in range(attempts):
        if attempt:
            sleep((1, 3)[attempt - 1])
        require_unchanged()
        observed = execute("ls-remote", "--exit-code", "origin", REMOTE_REF)
        if observed.returncode != 0:
            if not transient(observed):
                raise StorePushError("STORE_REMOTE_UNREADABLE")
            last_error = "STORE_REMOTE_READ_RETRIES_EXHAUSTED"
            continue  # Never push without a fresh exact-base observation.
        if check_remote(remote_value(observed)):
            return success(pushes)
        # A normal fast-forward push, never force/lease/rebase/merge. Always the
        # same immutable SHA, even when the previous acknowledgement was lost.
        require_unchanged()
        pushed = execute("push", "--porcelain", "origin", f"{candidate}:{REMOTE_REF}")
        pushes += 1
        observed = execute("ls-remote", "--exit-code", "origin", REMOTE_REF)
        if observed.returncode == 0:
            if check_remote(remote_value(observed)):
                return success(pushes)
        elif not transient(observed):
            raise StorePushError("STORE_PUSH_READBACK_UNAVAILABLE")
        if pushed.returncode != 0 and not transient(pushed):
            raise StorePushError("STORE_PUSH_REJECTED_NON_TRANSIENT")
        last_error = ("STORE_PUSH_READBACK_UNCONFIRMED" if pushed.returncode == 0
                      else "STORE_TRANSPORT_RETRIES_EXHAUSTED")
    raise StorePushError(last_error)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    try:
        result = push_exact(args.store_root, args.base, args.candidate)
    except StorePushError as exc:
        # Do not leak private payloads, credential URLs or raw git output.
        print(json.dumps({"status": "store_push_failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
