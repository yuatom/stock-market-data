#!/usr/bin/env python3
"""Emit bounded failure metadata, never raw collector stderr or market facts.

The exception name is an observation from stderr, not an authenticated cause.
The caller owns the original exit code and must still stop publication.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

MAX_ERROR_BYTES = 8192
EXCEPTION_CLASSES = frozenset({
    "CoverageContractError", "RuntimeError", "ValueError", "TypeError",
    "KeyError", "AssertionError", "FileNotFoundError", "PermissionError",
    "JSONDecodeError", "ModuleNotFoundError", "ImportError", "OSError",
})


def summarize_failure(exit_code: int, error_file: Path) -> dict[str, object]:
    if not 1 <= exit_code <= 255:
        raise ValueError("expected a nonzero shell exit code")
    payload: dict[str, object] = {
        "status": "collector_failed",
        "exit_code": exit_code,
        "observed_exception_class": "unknown",
        "error_read_status": "unavailable",
    }
    try:
        with error_file.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - MAX_ERROR_BYTES)
            stream.seek(start)
            raw = stream.read(MAX_ERROR_BYTES)
        payload["error_read_status"] = "read"
        payload["error_tail_truncated"] = start > 0
        # Only the last nonblank line can supply a terminal exception name.
        # Discard a potentially partial first line after a bounded tail read.
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start:
            lines = lines[1:]
        terminal = next((line for line in reversed(lines) if line.strip()), "")
        prefix = terminal.partition(":")[0]
        name = prefix.removeprefix("market_data_collection.")
        if name in EXCEPTION_CLASSES and terminal.startswith(prefix + ":"):
            payload["observed_exception_class"] = name
    except (OSError, ValueError):
        pass
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--error-file", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(summarize_failure(args.exit_code, args.error_file), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
