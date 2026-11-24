"""Fail-closed hosted selector for an unregistered ordinary retained scope.

The event and repository checks are routing guards, not authorization. The
existing private receipt verifier does not register this role. A later reviewed
issuer must authenticate the one-use receipt, claim it durably under the same
writer queue, and reconcile uncertain outcomes without reissuing or retrying.
This module deliberately has no credential, network, Store, or writer access.
"""
import json
import os
from pathlib import Path
import re
import sys

_REPO = "yuatom/stock-market-data"
_WORKFLOW = _REPO + "/.github/workflows/reconcile-retained-daily.yml@refs/heads/main"
_INPUT = "ordinary_retained_commitment"
_MAX_EVENT_BYTES = 256 * 1024
_REJECTED = "ORDINARY_RETAINED_PREFLIGHT_REJECTED"
_HELD = "ORDINARY_RETAINED_HELD_PRIVATE_ISSUER_UNREGISTERED"


def _closed_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _select(env):
    sha = env.get("GITHUB_SHA", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": _REPO,
        "GITHUB_REPOSITORY_OWNER": "yuatom",
        "GITHUB_ACTOR": "yuatom",
        "GITHUB_TRIGGERING_ACTOR": "yuatom",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": _WORKFLOW,
        "GITHUB_WORKFLOW_SHA": sha,
        "GITHUB_RUN_ATTEMPT": "1",
    }
    if (not re.fullmatch(r"[a-f0-9]{40}", sha)
            or not re.fullmatch(r"[1-9][0-9]{0,29}", run_id)
            or not all(env.get(key) == value for key, value in expected.items())):
        return _REJECTED
    try:
        with Path(env["GITHUB_EVENT_PATH"]).open("rb") as event_file:
            raw = event_file.read(_MAX_EVENT_BYTES + 1)
        if not 0 < len(raw) <= _MAX_EVENT_BYTES:
            return _REJECTED
        event = json.loads(raw, object_pairs_hook=_closed_pairs)
        inputs = event["inputs"]
        commitment = inputs[_INPUT]
        if (event["ref"] not in ("main", "refs/heads/main")
                or event["repository"]["full_name"] != _REPO
                or event["sender"]["login"] != "yuatom"
                or set(inputs) - {_INPUT, "expected_compute_sha", "request_json"}
                or inputs.get("expected_compute_sha", "") != ""
                or inputs.get("request_json", "") != ""
                or type(commitment) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", commitment)
                or commitment == "0" * 64):
            return _REJECTED
    except (KeyError, OSError, UnicodeError, TypeError, ValueError, RecursionError):
        return _REJECTED

    # Never infer authority from a valid password-shaped input or platform
    # identity, nor permit an env flag/caller callback to activate a writer.
    return _HELD


def main():
    state = _REJECTED if len(sys.argv) != 1 else _select(os.environ)
    print(state, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
