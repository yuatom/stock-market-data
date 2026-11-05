#!/bin/sh
# Local equivalent of the former Data Plane contract-test workflow.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
python=${PYTHON:-python3.12}
if ! "$python" -c 'import sys; sys.exit(2) if sys.version_info[:2] != (3, 12) else None; import yaml, pytest, jsonschema' >/dev/null 2>&1; then
    printf '%s\n' 'Contract tests require Python 3.12 with PyYAML, pytest, and jsonschema installed.' >&2
    exit 2
fi
exec "$python" -m pytest tests -v "$@"
