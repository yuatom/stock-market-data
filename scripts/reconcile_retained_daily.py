#!/usr/bin/env python3
"""One exact retained-data append; never a provider acquisition or recovery retry.

The command-line publisher is restricted to its official manual main workflow.
Library construction accepts explicit pinned inputs for isolated synthetic tests;
there is intentionally no command-line bypass of the production request/guards.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'schemas/retained-daily-reconciliation.schema.json'
APPROVED_REQUEST = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))['const']


class ReconciliationError(RuntimeError):
    """An internal failure; only explicit allowlisted diagnostics may be public."""


SAFE_FAILURE_REASONS = frozenset({
    'RETAINED_ACCOUNTING_CHANGED', 'RETAINED_ACCOUNTING_MISSING',
    'RETAINED_ADDITIONS_CHANGED', 'RETAINED_ARGUMENTS_INVALID',
    'RETAINED_BASE_BLOBS_CHANGED', 'RETAINED_BASE_COVERAGE_CHANGED',
    'RETAINED_BASE_ADVANCE_INVALID', 'RETAINED_BASE_ADVANCE_SCOPE_INVALID',
    'RETAINED_BASE_HISTORY_INVALID', 'RETAINED_BASE_TRAVERSAL_LIMIT',
    'RETAINED_BASE_FETCH_IDENTITY_CHANGED',
    'RETAINED_BASE_PROVENANCE_CHANGED', 'RETAINED_BASE_PROVENANCE_MISSING',
    'RETAINED_CANDIDATE_CONTENT_CHANGED', 'RETAINED_CANDIDATE_INVALID',
    'RETAINED_CANDIDATE_PARENT_CHANGED', 'RETAINED_CHECKOUT_DIRTY',
    'RETAINED_CHECKOUT_DRIFT', 'RETAINED_CHECKOUT_INVALID', 'RETAINED_CLOSURE_INVALID',
    'RETAINED_COMPUTE_INVALID', 'RETAINED_COMPUTE_MAIN_CHANGED',
    'RETAINED_CONFLICT_SET_CHANGED', 'RETAINED_DELETION_FORBIDDEN',
    'RETAINED_EXECUTION_SURFACE_INVALID', 'RETAINED_GIT_FAILED',
    'RETAINED_GIT_HOOK_INVALID', 'RETAINED_GIT_ROUTING_INVALID',
    'RETAINED_GIT_UNAVAILABLE', 'RETAINED_LINEAGE_CHANGED', 'RETAINED_MAIN_UNREADABLE',
    'RETAINED_NO_OVERWRITE_FAILED', 'RETAINED_ORIGIN_INVALID',
    'RETAINED_OUTPUT_COLLISION', 'RETAINED_PINS_INVALID',
    'RETAINED_RECONCILIATION_REJECTED', 'RETAINED_REQUEST_ID_INVALID',
    'RETAINED_REQUEST_INVALID', 'RETAINED_SEALED_SHARDS_CHANGED',
    'RETAINED_SOURCE_BLOB_CHANGED', 'RETAINED_SOURCE_DATES_CHANGED',
    'RETAINED_SOURCE_IDENTITY_CHANGED', 'RETAINED_SOURCE_ROWS_CHANGED',
    'RETAINED_STORE_MAIN_CHANGED', 'RETAINED_TERMINAL_PROOF_INVALID',
    'RETAINED_TIMESTAMP_INVALID', 'RETAINED_TREE_EMPTY', 'RETAINED_TREE_MODE_INVALID',
    'RETAINED_TREE_PATH_INVALID', 'RETAINED_WRITE_SCOPE_CHANGED',
})
SAFE_FAILURE_STAGES = frozenset({
    'ARGUMENT_VALIDATION', 'REQUEST_VALIDATION', 'EXECUTION', 'COMPUTE_GUARD',
    'STORE_ORIGIN_GUARD', 'SOURCE_ORIGIN_GUARD', 'STORE_MAIN_GUARD',
    'CANDIDATE_PREPARATION', 'CANDIDATE_COMMIT', 'PUBLICATION',
})


def safe_failure_reason(error: Exception) -> str:
    reason = error.args[0] if isinstance(error, ReconciliationError) and len(error.args) == 1 else None
    return reason if type(reason) is str and reason in SAFE_FAILURE_REASONS else 'RETAINED_RECONCILIATION_REJECTED'


def safe_failure_stage(error: Exception) -> str:
    stage = getattr(error, 'stage', None) if isinstance(error, ReconciliationError) else None
    return stage if type(stage) is str and stage in SAFE_FAILURE_STAGES else 'EXECUTION'


def _at_stage(stage: str, operation, *args, **kwargs):
    """Annotate failure without logging exception text, Git output, or inputs."""
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        failure = ReconciliationError(safe_failure_reason(error))
        prior = getattr(error, 'stage', None) if isinstance(error, ReconciliationError) else None
        failure.stage = prior if type(prior) is str and prior in SAFE_FAILURE_STAGES else stage
        raise failure from None


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def strict_json(raw: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReconciliationError('RETAINED_REQUEST_INVALID')
            result[key] = value
        return result

    def constant(_):
        raise ReconciliationError('RETAINED_REQUEST_INVALID')

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReconciliationError('RETAINED_REQUEST_INVALID') from None


def validate_request(raw: str) -> dict[str, Any]:
    value = strict_json(raw)
    if canonical(value) != canonical(APPROVED_REQUEST):
        raise ReconciliationError('RETAINED_REQUEST_INVALID')
    return value

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import os
import re
import subprocess
import tempfile
from typing import Callable, Mapping

try:
    from scripts import market_data_store as store
    from scripts.build_market_data_inventory import build_inventory
    from scripts import verify_store_publication as verifier
except ModuleNotFoundError:
    import market_data_store as store
    from build_market_data_inventory import build_inventory
    import verify_store_publication as verifier

PREFIX = 'data/market-data/'
SERIES = PREFIX + 'series/daily/twelve_data_basic/GLD/'
INVENTORY = PREFIX + 'collector-state/market-data-inventory.json'
CURRENT_BUDGET = PREFIX + 'collector-state/2026-11/2026-11-10-twelve-data-budget.json'
DATES = ('2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09')
SHARDS = ('000000.json', '000128.json', '000256.json')
SHA_RE = re.compile(r'[0-9a-f]{40}')
REQUEST_ID_RE = re.compile(r'[1-9][0-9]{0,29}-[1-9][0-9]{0,9}')
COMPUTE_REPOSITORY = 'yuatom/stock-market-data'
STORE_REPOSITORY = 'yuatom/stock-market-data-store'
SOURCE_AUTHORITY_COMPUTE = '0ef366bea33862076b57fb101a4498d489f826db'
BASE_ADVANCE_POLICY = 'nov10_close_artifacts_only_v1'
MAX_BASE_COMMITS = 64
MAX_BASE_PATHS_PER_COMMIT = 256
MAX_BASE_DIFF_BYTES = 256 * 1024
CLOSE_RECEIPT_RE = re.compile(re.escape(PREFIX) + r'collector-state/2026-11/2026-11-10-(?:close|close_retry)-[0-9a-f]{16}-publication\.json')
CLOSE_PROOF_RE = re.compile(re.escape(PREFIX) + r'proofs/deterministic-metrics/2026-11/2026-11-10/close/[0-9a-f]{40}/close-(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]-et\.json')
MUTABLE_CLOSE_PATHS = frozenset({
    INVENTORY,
    PREFIX + 'collector-state/2026-11/2026-11-10-close-missing.json',
    PREFIX + 'collector-state/2026-11/2026-11-10-close_retry-missing.json',
    PREFIX + 'proofs/deterministic-metrics/2026-11/2026-11-10/close/latest.json',
})


@dataclass(frozen=True)
class PinnedInputs:
    """Immutable object identities; production obtains these only from validation."""
    expected_store_sha: str
    source_candidate_sha: str
    source_tail_blob_sha: str
    expected_index_blob_sha: str
    expected_shard_blob_shas: tuple[tuple[str, str], ...]
    additions_sha256: str
    approved_reviewed_store_sha: str | None = None

    @classmethod
    def from_request(cls, request: dict[str, Any]) -> PinnedInputs:
        validate_request(canonical(request).decode('utf-8'))
        return cls(**{key: request[key] for key in (
            'expected_store_sha', 'source_candidate_sha', 'source_tail_blob_sha',
            'expected_index_blob_sha', 'additions_sha256')},
            expected_shard_blob_shas=tuple(sorted(request['expected_shard_blob_shas'].items())),
            approved_reviewed_store_sha=request['expected_store_sha'])


@dataclass(frozen=True)
class PreparedCandidate:
    expected_store_sha: str
    request_id: str
    changes: dict[str, bytes]


def git_bytes(root: Path, *args: str) -> bytes:
    """One bounded Git invocation. Never echo private Git diagnostics or retry writes."""
    try:
        result = subprocess.run(
            ['git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
             '-C', str(root), *args], capture_output=True, check=False, timeout=60,
            env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never'),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReconciliationError('RETAINED_GIT_UNAVAILABLE') from None
    if result.returncode:
        raise ReconciliationError('RETAINED_GIT_FAILED')
    return result.stdout


def git_text(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode('utf-8').strip()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReconciliationError(reason)


def _identity(pins: PinnedInputs, compute_sha: str, request_id: str) -> None:
    _require(isinstance(compute_sha, str) and bool(SHA_RE.fullmatch(compute_sha)), 'RETAINED_COMPUTE_INVALID')
    _require(isinstance(request_id, str) and bool(REQUEST_ID_RE.fullmatch(request_id)), 'RETAINED_REQUEST_ID_INVALID')
    values = (pins.expected_store_sha, pins.approved_reviewed_store_sha or pins.expected_store_sha,
              pins.source_candidate_sha, pins.source_tail_blob_sha,
              pins.expected_index_blob_sha, *(value for _, value in pins.expected_shard_blob_shas))
    _require(all(isinstance(value, str) and SHA_RE.fullmatch(value) for value in values), 'RETAINED_PINS_INVALID')
    _require(tuple(name for name, _ in pins.expected_shard_blob_shas) == SHARDS, 'RETAINED_PINS_INVALID')
    _require(bool(re.fullmatch(r'[0-9a-f]{64}', pins.additions_sha256)), 'RETAINED_PINS_INVALID')


def _paths(request_id: str) -> tuple[str, str]:
    _require(bool(REQUEST_ID_RE.fullmatch(request_id)), 'RETAINED_REQUEST_ID_INVALID')
    stem = PREFIX + 'collector-state/retained-daily-reconciliation/' + request_id
    return stem + '.json', stem + '-outputs.json'


def _require_checkout(root: Path, expected: str) -> None:
    root = root.resolve()
    _require((root / '.git').is_dir() and not (root / '.git').is_symlink(), 'RETAINED_CHECKOUT_INVALID')
    _require(Path(git_text(root, 'rev-parse', '--show-toplevel')).resolve() == root, 'RETAINED_CHECKOUT_INVALID')
    _require(git_text(root, 'rev-parse', 'HEAD') == expected, 'RETAINED_CHECKOUT_DRIFT')
    _require(not git_text(root, 'status', '--porcelain', '--untracked-files=all'), 'RETAINED_CHECKOUT_DIRTY')


def _remote_main(root: Path) -> str:
    rows = [line.split() for line in git_text(root, 'ls-remote', 'origin', 'refs/heads/main').splitlines()]
    _require(len(rows) == 1 and len(rows[0]) == 2 and rows[0][1] == 'refs/heads/main'
             and bool(SHA_RE.fullmatch(rows[0][0])), 'RETAINED_MAIN_UNREADABLE')
    return rows[0][0]


def _require_origin(root: Path, repository: str) -> None:
    allowed = {'https://github.com/' + repository, 'https://github.com/' + repository + '.git'}
    _require(git_text(root, 'remote', 'get-url', 'origin') in allowed, 'RETAINED_ORIGIN_INVALID')
    pushes = git_text(root, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()
    _require(len(pushes) == 1 and pushes[0] in allowed, 'RETAINED_ORIGIN_INVALID')
    # The unchanged terminal verifier also invokes Git. Prevent effective routing,
    # executable hooks and external filters from changing its destination/behavior.
    for line in git_text(root, 'config', '--show-scope', '--list').splitlines():
        scope, setting = line.split('\t', 1)
        # Ignore only the two safe overrides this module itself injects. Persisted
        # settings still apply to the separate verifier and must be inspected.
        if scope == 'command' and setting in {'core.hookspath=/dev/null', 'core.fsmonitor=false'}:
            continue
        key = setting.split('=', 1)[0].lower()
        _require(not (key.startswith(('url.', 'include.', 'includeif.', 'filter.', 'push.'))
                      or key in {'core.hookspath', 'core.fsmonitor', 'core.sshcommand', 'remote.origin.mirror'}
                      or key.endswith(('.pushurl', '.receivepack', '.uploadpack', '.vcs'))), 'RETAINED_GIT_ROUTING_INVALID')
    hook_root = root / '.git' / 'hooks'
    _require(not any(path.is_file() and not path.name.endswith('.sample') and path.stat().st_mode & 0o111
                     for path in hook_root.glob('*')), 'RETAINED_GIT_HOOK_INVALID')


def require_execution_surface(compute_repo: Path, compute_sha: str, request_id: str,
                              *, environment: Mapping[str, str] | None = None) -> None:
    environment = os.environ if environment is None else environment
    expected = {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': compute_sha,
        'GITHUB_REPOSITORY': COMPUTE_REPOSITORY,
        'GITHUB_WORKFLOW': 'Retained Daily Reconciliation',
        'GITHUB_WORKFLOW_REF': COMPUTE_REPOSITORY + '/.github/workflows/reconcile-retained-daily.yml@refs/heads/main',
    }
    _require(all(environment.get(key) == value for key, value in expected.items()), 'RETAINED_EXECUTION_SURFACE_INVALID')
    _require(bool(SHA_RE.fullmatch(compute_sha or '')) and bool(REQUEST_ID_RE.fullmatch(request_id or '')),
             'RETAINED_EXECUTION_SURFACE_INVALID')
    _require(request_id == environment.get('GITHUB_RUN_ID', '') + '-' + environment.get('GITHUB_RUN_ATTEMPT', ''),
             'RETAINED_EXECUTION_SURFACE_INVALID')
    _require_checkout(compute_repo, compute_sha)
    _require_origin(compute_repo, COMPUTE_REPOSITORY)
    _require(_remote_main(compute_repo) == compute_sha, 'RETAINED_COMPUTE_MAIN_CHANGED')



def validate_store_base(root: Path, pins: PinnedInputs) -> None:
    """Independently verify every immutable transition from reviewed to actual base."""
    reviewed = pins.approved_reviewed_store_sha or pins.expected_store_sha
    actual = pins.expected_store_sha
    _require(all(isinstance(sha, str) and SHA_RE.fullmatch(sha) for sha in (reviewed, actual)),
             'RETAINED_BASE_ADVANCE_INVALID')
    _require(not os.environ.get('GIT_GRAFT_FILE') and not (root / '.git/info/grafts').exists(),
             'RETAINED_BASE_HISTORY_INVALID')
    _require(git_text(root, 'rev-parse', '--is-shallow-repository') == 'false',
             'RETAINED_BASE_HISTORY_INVALID')
    for sha in {reviewed, actual}:
        _require(git_text(root, 'cat-file', '-t', sha) == 'commit', 'RETAINED_BASE_HISTORY_INVALID')
    if actual == reviewed:
        return
    try:
        ancestor = git_text(root, 'merge-base', reviewed, actual)
    except ReconciliationError:
        raise ReconciliationError('RETAINED_BASE_HISTORY_INVALID') from None
    _require(ancestor == reviewed, 'RETAINED_BASE_HISTORY_INVALID')
    commits = git_text(root, 'rev-list', '--reverse', '--max-count=' + str(MAX_BASE_COMMITS + 1),
                       reviewed + '..' + actual).splitlines()
    _require(0 < len(commits) <= MAX_BASE_COMMITS, 'RETAINED_BASE_TRAVERSAL_LIMIT')
    prior = reviewed
    for commit in commits:
        _require(bool(SHA_RE.fullmatch(commit)), 'RETAINED_BASE_HISTORY_INVALID')
        _require(git_text(root, 'rev-list', '--parents', '-n', '1', commit).split() == [commit, prior],
                 'RETAINED_BASE_HISTORY_INVALID')
        raw = git_bytes(root, 'diff', '--raw', '-z', '--no-renames', '--no-ext-diff',
                        '--no-textconv', '--no-abbrev', '--no-color', prior, commit, '--')
        _require(len(raw) <= MAX_BASE_DIFF_BYTES, 'RETAINED_BASE_TRAVERSAL_LIMIT')
        fields = raw.rstrip(b'\0').split(b'\0') if raw else []
        _require(len(fields) % 2 == 0 and len(fields) // 2 <= MAX_BASE_PATHS_PER_COMMIT,
                 'RETAINED_BASE_TRAVERSAL_LIMIT')
        _require(bool(fields), 'RETAINED_BASE_ADVANCE_SCOPE_INVALID')
        for offset in range(0, len(fields), 2):
            metadata = fields[offset].decode('ascii').split()
            path = fields[offset + 1].decode('utf-8')
            _require(len(metadata) == 5, 'RETAINED_BASE_ADVANCE_SCOPE_INVALID')
            old_mode, new_mode, old_blob, new_blob, status = metadata
            _require(status in {'A', 'M'} and new_mode == '100644'
                     and old_mode == (':000000' if status == 'A' else ':100644')
                     and bool(SHA_RE.fullmatch(old_blob)) and bool(SHA_RE.fullmatch(new_blob)),
                     'RETAINED_BASE_ADVANCE_SCOPE_INVALID')
            mutable = path in MUTABLE_CLOSE_PATHS
            immutable_addition = status == 'A' and bool(CLOSE_RECEIPT_RE.fullmatch(path) or CLOSE_PROOF_RE.fullmatch(path))
            _require(mutable or immutable_addition, 'RETAINED_BASE_ADVANCE_SCOPE_INVALID')
        prior = commit
    _require(prior == actual, 'RETAINED_BASE_HISTORY_INVALID')


def resolve_store_base(root: Path, pins: PinnedInputs) -> PinnedInputs:
    """Resolve once under the workflow lock; no repoll/rebuild or publication retry."""
    reviewed = pins.approved_reviewed_store_sha or pins.expected_store_sha
    _require(pins.expected_store_sha == reviewed, 'RETAINED_BASE_ADVANCE_INVALID')
    _require_checkout(root, reviewed)
    actual = _remote_main(root)
    _require(isinstance(actual, str) and bool(SHA_RE.fullmatch(actual)), 'RETAINED_BASE_ADVANCE_INVALID')
    git_bytes(root, 'fetch', '--no-tags', '--no-recurse-submodules', 'origin', actual)
    _require(git_text(root, 'rev-parse', 'FETCH_HEAD') == actual, 'RETAINED_BASE_FETCH_IDENTITY_CHANGED')
    effective = replace(pins, expected_store_sha=actual, approved_reviewed_store_sha=reviewed)
    validate_store_base(root, effective)
    _require_checkout(root, reviewed)
    if actual != reviewed:
        git_bytes(root, 'checkout', '--detach', actual)
    _require_checkout(root, actual)
    return effective


def _tree(root: Path, commit: str) -> tuple[dict[str, bytes], dict[str, tuple[str, str]]]:
    files, entries = {}, {}
    for record in git_bytes(root, 'ls-tree', '-r', '-z', '--full-tree', commit, '--', PREFIX).split(b'\0'):
        if not record:
            continue
        meta, path_bytes = record.split(b'\t', 1)
        mode, kind, sha = meta.decode('ascii').split()
        path = path_bytes.decode('utf-8')
        _require(path.startswith(PREFIX) and not any(part in {'', '.', '..', '.git'} for part in path.split('/'))
                 and not any(ord(char) < 32 for char in path), 'RETAINED_TREE_PATH_INVALID')
        _require(kind == 'blob' and mode == '100644', 'RETAINED_TREE_MODE_INVALID')
        entries[path] = (mode, sha)
        files[path] = git_bytes(root, 'cat-file', 'blob', sha)
    _require(bool(files), 'RETAINED_TREE_EMPTY')
    return files, entries


def _materialize(root: Path, files: Mapping[str, bytes]) -> None:
    for path, raw in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
                 'RETAINED_TIMESTAMP_INVALID')
        _require(parsed.isoformat() == value, 'RETAINED_TIMESTAMP_INVALID')
    except (ValueError, TypeError):
        raise ReconciliationError('RETAINED_TIMESTAMP_INVALID') from None
    return value


def _source_additions(source: Path, pins: PinnedInputs, old: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = git_bytes(source, 'show', pins.source_candidate_sha + ':' + SERIES + '000256.json')
    _require(store.git_blob_sha_bytes(raw) == pins.source_tail_blob_sha, 'RETAINED_SOURCE_BLOB_CHANGED')
    doc = strict_json(raw)
    _require(isinstance(doc, dict) and set(doc) == {'schema_version', 'series_id', 'start', 'records'}
             and doc['schema_version'] == 1 and doc['start'] == 256
             and doc['series_id'] == 'twelve_data_basic:GLD:regular:1d', 'RETAINED_SOURCE_IDENTITY_CHANGED')
    rows = doc['records']
    _require(isinstance(rows, list) and len(rows) == 60 and all(isinstance(row, dict) for row in rows),
             'RETAINED_SOURCE_ROWS_CHANGED')
    dates = [row.get('trade_date') for row in rows]
    _require(dates == [row['trade_date'] for row in old[256:]] + list(DATES), 'RETAINED_SOURCE_DATES_CHANGED')
    conflicts = []
    for prior, retained in zip(old[256:], rows[:54]):
        fields = sorted(key for key in set(prior) | set(retained) if canonical(prior.get(key)) != canonical(retained.get(key)))
        if fields:
            conflicts.append({'trade_date': prior['trade_date'], 'fields': fields})
    _require(conflicts == [{'trade_date': '2026-10-30', 'fields': ['volume']}], 'RETAINED_CONFLICT_SET_CHANGED')
    additions = copy.deepcopy(rows[54:])
    _require(hashlib.sha256(canonical(additions)).hexdigest() == pins.additions_sha256, 'RETAINED_ADDITIONS_CHANGED')
    return additions


def _derive_changes(base: dict[str, bytes], source: Path, *, pins: PinnedInputs,
                    compute_sha: str, request_id: str, generated_at: str) -> dict[str, bytes]:
    report_path, manifest_path = _paths(request_id)
    _timestamp(generated_at)
    _require(report_path not in base and manifest_path not in base, 'RETAINED_OUTPUT_COLLISION')
    expected_blobs = {SERIES + '_index.json': pins.expected_index_blob_sha,
                      **{SERIES + name: sha for name, sha in pins.expected_shard_blob_shas}}
    _require(all(path in base and store.git_blob_sha_bytes(base[path]) == sha for path, sha in expected_blobs.items()),
             'RETAINED_BASE_BLOBS_CHANGED')
    budgets = {path: store.git_blob_sha_bytes(raw) for path, raw in base.items()
               if path.startswith(PREFIX + 'collector-state/') and 'budget' in Path(path).name.lower()}
    _require(CURRENT_BUDGET in budgets, 'RETAINED_ACCOUNTING_MISSING')
    with tempfile.TemporaryDirectory(prefix='retained-daily-stage-') as directory:
        stage = Path(directory)
        _materialize(stage, base)
        old = store.read_daily_series(stage / PREFIX, provider='twelve_data_basic', symbol='GLD')
        prior_index = strict_json(base[SERIES + '_index.json'])
        _require(len(old) == 310 and old[-1]['trade_date'] == '2026-10-30'
                 and [item['path'] for item in prior_index['shards']] == list(SHARDS), 'RETAINED_BASE_COVERAGE_CHANGED')
        _require('baseline_initialization' in prior_index and 'lineage_segments' in prior_index, 'RETAINED_BASE_PROVENANCE_MISSING')
        additions = _source_additions(source, pins, old)
        lineage = {
            'ingest_kind': 'retained_candidate_append_reconciliation', 'request_id': request_id,
            'compute_sha': compute_sha, 'store_start_sha': pins.expected_store_sha,
            'approved_reviewed_store_sha': pins.approved_reviewed_store_sha or pins.expected_store_sha,
            'base_advance_policy': BASE_ADVANCE_POLICY,
            'source_candidate_sha': pins.source_candidate_sha, 'source_tail_blob_sha': pins.source_tail_blob_sha,
            'additions_sha256': pins.additions_sha256,
        }
        store.append_daily_bars(stage / PREFIX, provider='twelve_data_basic', symbol='GLD', asset_class='etf',
                                series_semantics='daily_regular_ohlcv', bars=additions, lineage=lineage)
        after = store.read_daily_series(stage / PREFIX, provider='twelve_data_basic', symbol='GLD')
        _require(canonical(after) == canonical(old + additions), 'RETAINED_NO_OVERWRITE_FAILED')
        index = strict_json((stage / (SERIES + '_index.json')).read_bytes())
        # Even a changed/replaced append helper cannot silently revise baseline metadata.
        old_fixed = {key: value for key, value in prior_index.items() if key not in {'record_count', 'last_trade_date', 'shards', 'lineage_segments'}}
        new_fixed = {key: value for key, value in index.items() if key not in {'record_count', 'last_trade_date', 'shards', 'lineage_segments'}}
        _require(canonical(old_fixed) == canonical(new_fixed), 'RETAINED_BASE_PROVENANCE_CHANGED')
        _require(index['lineage_segments'] == prior_index['lineage_segments'] + [dict(lineage, start=310, end=315)],
                 'RETAINED_LINEAGE_CHANGED')
        _require(index['shards'][:2] == prior_index['shards'][:2] and len(index['shards']) == 3,
                 'RETAINED_SEALED_SHARDS_CHANGED')
        report = {
            'schema_version': 1, 'mode': 'retained_daily_append', 'status': 'CANDIDATE_VALIDATED',
            'request_id': request_id, 'generated_at': generated_at,
            'production_recovery_accepted': False, 'publication_requires_terminal_receipt': True,
            'compute_sha': compute_sha, 'input_source_authority_compute_sha': SOURCE_AUTHORITY_COMPUTE,
            'store_start_sha': pins.expected_store_sha, 'source_candidate_sha': pins.source_candidate_sha,
            'approved_reviewed_store_sha': pins.approved_reviewed_store_sha or pins.expected_store_sha,
            'base_advance_policy': BASE_ADVANCE_POLICY,
            'source_tail_blob_sha': pins.source_tail_blob_sha, 'source_candidate_publication': 'NOT_ASSERTED',
            'additions_sha256': pins.additions_sha256, 'addition_dates': list(DATES),
            'existing_records': 310, 'new_records': 6, 'result_records': 316,
            'conflicts': [{'trade_date': '2026-10-30', 'fields': ['volume'], 'resolution': 'preserve_existing'}],
            'provider_requests': 0, 'additional_debits': 0, 'refunds': 0,
            'budget_policy': 'preserve_all_bytes', 'unchanged_budget_blobs': budgets,
        }
        inventory = build_inventory(stage / PREFIX)
        inventory['generated_at'] = generated_at
        for path, value in ((report_path, report), (INVENTORY, inventory)):
            target = stage / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(canonical(value) + b'\n')
        required = sorted([SERIES + '_index.json', *(SERIES + name for name in SHARDS), report_path, INVENTORY])
        manifest = {'schema_version': 1, 'request_id': request_id, 'trade_date': DATES[-1],
                    'stage': 'retained_daily_append_reconciliation',
                    'required_outputs': [{'path': path[len(PREFIX):], 'blob_sha': store.git_blob_sha_bytes((stage / path).read_bytes())}
                                         for path in required]}
        (stage / manifest_path).write_bytes(canonical(manifest) + b'\n')
        after_files = {str(path.relative_to(stage)): path.read_bytes() for path in (stage / PREFIX).rglob('*') if path.is_file()}
        _require(set(base) <= set(after_files), 'RETAINED_DELETION_FORBIDDEN')
        changes = {path: raw for path, raw in after_files.items() if base.get(path) != raw}
        _require(set(changes) == {SERIES + '000256.json', SERIES + '_index.json', report_path, manifest_path, INVENTORY},
                 'RETAINED_WRITE_SCOPE_CHANGED')
        _require(all(after_files[path] == base[path] for path in budgets), 'RETAINED_ACCOUNTING_CHANGED')
        return changes


def prepare_candidate(store_repo: Path, source_repo: Path, *, pins: PinnedInputs,
                      compute_sha: str, request_id: str, generated_at: str | None = None) -> PreparedCandidate:
    _identity(pins, compute_sha, request_id)
    _require_checkout(store_repo, pins.expected_store_sha)
    _require_checkout(source_repo, pins.source_candidate_sha)
    validate_store_base(store_repo, pins)
    base, _ = _tree(store_repo, pins.expected_store_sha)
    changes = _derive_changes(base, source_repo, pins=pins, compute_sha=compute_sha,
                              request_id=request_id, generated_at=generated_at or datetime.now(timezone.utc).isoformat())
    _require_checkout(store_repo, pins.expected_store_sha)
    _require_checkout(source_repo, pins.source_candidate_sha)
    return PreparedCandidate(pins.expected_store_sha, request_id, changes)


def commit_candidate(store_repo: Path, prepared: PreparedCandidate) -> str:
    _require_checkout(store_repo, prepared.expected_store_sha)
    report_path, manifest_path = _paths(prepared.request_id)
    _require(set(prepared.changes) == {SERIES + '000256.json', SERIES + '_index.json', report_path, manifest_path, INVENTORY},
             'RETAINED_WRITE_SCOPE_CHANGED')
    for path in prepared.changes:
        target = store_repo / path
        # Preflight every target before writing even one byte.
        # An ignored path or symlink must not silently redirect a write.
        _require(not target.is_symlink() and not any(parent.is_symlink() for parent in target.parents),
                 'RETAINED_TREE_PATH_INVALID')
        _require(path not in {report_path, manifest_path} or not target.exists(), 'RETAINED_OUTPUT_COLLISION')
    for path, raw in prepared.changes.items():
        target = store_repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    git_bytes(store_repo, 'add', '--', *sorted(prepared.changes))
    git_bytes(store_repo, '-c', 'user.name=stock-market-data-bot',
              '-c', 'user.email=stock-market-data-bot@users.noreply.github.com',
              '-c', 'commit.gpgsign=false', 'commit', '-m', 'Reconcile retained GLD daily additions ' + prepared.request_id)
    return git_text(store_repo, 'rev-parse', 'HEAD')


def validate_candidate(store_repo: Path, source_repo: Path, *, candidate_sha: str,
                       pins: PinnedInputs, compute_sha: str, request_id: str) -> list[dict[str, str]]:
    """Rederive exact outputs from immutable inputs, not a mutable staging receipt."""
    _identity(pins, compute_sha, request_id)
    _require(bool(SHA_RE.fullmatch(candidate_sha)), 'RETAINED_CANDIDATE_INVALID')
    _require_checkout(store_repo, candidate_sha)
    _require_checkout(source_repo, pins.source_candidate_sha)
    validate_store_base(store_repo, pins)
    parents = git_text(store_repo, 'rev-list', '--parents', '-n', '1', candidate_sha).split()
    _require(parents == [candidate_sha, pins.expected_store_sha], 'RETAINED_CANDIDATE_PARENT_CHANGED')
    report_path, manifest_path = _paths(request_id)
    base, base_entries = _tree(store_repo, pins.expected_store_sha)
    candidate, candidate_entries = _tree(store_repo, candidate_sha)
    _require(set(base) <= set(candidate), 'RETAINED_DELETION_FORBIDDEN')
    try:
        report = strict_json(candidate[report_path])
        expected = _derive_changes(base, source_repo, pins=pins, compute_sha=compute_sha,
                                   request_id=request_id, generated_at=report['generated_at'])
    except (KeyError, TypeError, ValueError, store.MarketDataError):
        raise ReconciliationError('RETAINED_CANDIDATE_INVALID') from None
    actual_paths = set(git_bytes(store_repo, 'diff', '--name-only', '-z', pins.expected_store_sha, candidate_sha).decode().rstrip('\0').split('\0'))
    _require(actual_paths == set(expected), 'RETAINED_WRITE_SCOPE_CHANGED')
    for path, raw in candidate.items():
        _require(raw == expected.get(path, base.get(path)), 'RETAINED_CANDIDATE_CONTENT_CHANGED')
        if path in base_entries:
            _require(candidate_entries[path][0] == base_entries[path][0], 'RETAINED_TREE_MODE_INVALID')
    _require(set(candidate) == set(base) | set(expected), 'RETAINED_WRITE_SCOPE_CHANGED')
    try:
        closure = verifier._verify_required_manifest(store_repo, candidate_sha, manifest_path)
    except verifier.StorePublicationError:
        raise ReconciliationError('RETAINED_CLOSURE_INVALID') from None
    _require({item['path'] for item in closure} == {SERIES + '_index.json', *(SERIES + name for name in SHARDS), report_path, INVENTORY},
             'RETAINED_CLOSURE_INVALID')
    _require_checkout(store_repo, candidate_sha)
    return closure


def publish_candidate(store_repo: Path, source_repo: Path, *, candidate_sha: str,
                      pins: PinnedInputs, compute_sha: str, request_id: str,
                      pre_push_guard: Callable[[], None]) -> dict[str, Any]:
    """Exactly one main push. The caller must supply its execution/destination guard.

    The production CLI always supplies the official workflow guard. Tests supply
    an explicit guard restricted to their temporary local bare fixture.
    """
    closure = validate_candidate(store_repo, source_repo, candidate_sha=candidate_sha,
                                 pins=pins, compute_sha=compute_sha, request_id=request_id)
    pre_push_guard()
    _require(_remote_main(store_repo) == pins.expected_store_sha, 'RETAINED_STORE_MAIN_CHANGED')
    _require_checkout(store_repo, candidate_sha)
    try:
        # Any failure after starting this call is uncertain. Never retry this push,
        # create a follow-up metadata commit, roll back, or silently repin/rebuild.
        git_bytes(store_repo, 'push', 'origin', candidate_sha + ':refs/heads/main')
        _, manifest_path = _paths(request_id)
        receipt = verifier.verify_publication(store_repo, candidate_sha, required_manifest=manifest_path,
                                              status='retained_daily_append_persisted')
        _require(receipt['status'] == 'durability_verified' and receipt['store_commit_sha'] == candidate_sha
                 and receipt['remote_main_sha'] == candidate_sha and receipt['verified_required_outputs'] == closure,
                 'RETAINED_TERMINAL_PROOF_INVALID')
        _require(receipt['durability_ref'] == 'refs/tags/market-data-read/' + candidate_sha,
                 'RETAINED_TERMINAL_PROOF_INVALID')
        return {'status': 'VERIFIED', 'candidate_sha': candidate_sha, 'production_recovery_accepted': False,
                'remote_main_sha': receipt['remote_main_sha'], 'durability_ref': receipt['durability_ref'],
                'verified_required_outputs': closure}
    except Exception:
        return {'status': 'UNCONFIRMED', 'candidate_sha': candidate_sha,
                'reason': 'RETAINED_PUBLICATION_UNCONFIRMED', 'production_recovery_accepted': False}


def execute(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    compute_repo = Path(__file__).resolve().parents[1]
    _require(all((args.compute_sha, args.request_id, args.store_repo, args.candidate_repo)), 'RETAINED_ARGUMENTS_INVALID')
    store_repo, source_repo = Path(args.store_repo).resolve(), Path(args.candidate_repo).resolve()
    _require(len({compute_repo, store_repo, source_repo}) == 3, 'RETAINED_CHECKOUT_INVALID')
    def production_guard():
        _at_stage('COMPUTE_GUARD', require_execution_surface, compute_repo, args.compute_sha, args.request_id)
        _at_stage('STORE_ORIGIN_GUARD', _require_origin, store_repo, STORE_REPOSITORY)
        _at_stage('SOURCE_ORIGIN_GUARD', _require_origin, source_repo, STORE_REPOSITORY)
    production_guard()
    pins = PinnedInputs.from_request(request)
    pins = _at_stage('STORE_MAIN_GUARD', resolve_store_base, store_repo, pins)
    prepared = _at_stage('CANDIDATE_PREPARATION', prepare_candidate, store_repo, source_repo,
                         pins=pins, compute_sha=args.compute_sha, request_id=args.request_id)
    candidate_sha = _at_stage('CANDIDATE_COMMIT', commit_candidate, store_repo, prepared)
    return _at_stage('PUBLICATION', publish_candidate, store_repo, source_repo, candidate_sha=candidate_sha,
                     pins=pins, compute_sha=args.compute_sha, request_id=args.request_id, pre_push_guard=production_guard)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ReconciliationError('RETAINED_ARGUMENTS_INVALID')


def main(argv: list[str] | None = None) -> int:
    try:
        parser = SafeParser()
        parser.add_argument('--request-json', required=True)
        parser.add_argument('--validate-request-only', action='store_true')
        parser.add_argument('--compute-sha')
        parser.add_argument('--store-repo')
        parser.add_argument('--candidate-repo')
        parser.add_argument('--request-id')
        args = _at_stage('ARGUMENT_VALIDATION', parser.parse_args, argv)
        request = _at_stage('REQUEST_VALIDATION', validate_request, args.request_json)
        if args.validate_request_only:
            print(json.dumps({key: request[key] for key in ('expected_store_sha', 'source_candidate_sha')}, sort_keys=True))
            return 0
        receipt = _at_stage('EXECUTION', execute, args, request)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt['status'] == 'VERIFIED' else 1
    except Exception as error:
        print(json.dumps({'status': 'REJECTED', 'reason': safe_failure_reason(error),
                          'stage': safe_failure_stage(error)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
