"""Prepare an immutable, price-only correction candidate. No publication CLI.

Only the caller can authenticate an independent approval/grant and its source
receipts. This module binds it to the Git preimage, both evidence blobs,
declared identities and reviewed fields, and produces a single-base Git commit.
No provider request, branch/ref update, checkout mutation or volume edit occurs.
"""
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable

try:
    from scripts import market_data_store as S
    from scripts.reconcile_retained_daily import strict_json
    from scripts.retained_verified_price_correction import prepare
    from scripts.verify_store_publication import _verify_required_manifest
except ModuleNotFoundError:
    import market_data_store as S
    from reconcile_retained_daily import strict_json
    from retained_verified_price_correction import prepare
    from verify_store_publication import _verify_required_manifest

ROOT = 'data/market-data/'
VENDOR = 'twelve_data_basic'
PRIMARY_SOURCE = 'twelve_data'
COMPARISON_SOURCES = frozenset({'sp_global_market_intelligence', 'nasdaq_official'})
SHA = re.compile('[0-9a-f]{40}')
MAX_BLOB_BYTES = 16 * 1024 * 1024
REQUEST = re.compile('[a-z0-9][a-z0-9-]{0,63}')
SYMBOL = re.compile('[A-Z][A-Z0-9.-]{0,15}')
EVIDENCE_PATH = re.compile(r'data/market-data/collector-state/[0-9]{4}-[0-9]{2}/[a-zA-Z0-9-]+\.json')


class CorrectionCandidateError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise CorrectionCandidateError(reason)


@dataclass(frozen=True)
class Evidence:
    commit: str
    path: str
    blob_sha: str
    source: str


@dataclass(frozen=True)
class ApprovedCorrection:
    request_id: str
    store_base: str
    index_blob_sha: str
    active_shard_blob_sha: str
    symbol: str
    asset_class: str
    identity_effective_from: str | None
    execution_trade_date: str
    retained: Evidence
    comparator: Evidence
    # Bytes of canonical JSON representing the exact reviewed row/field scope.
    revisions: bytes


def _git(root, *args, data=None, index=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    if index is not None:
        env['GIT_INDEX_FILE'] = index
    cmd = ['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
           'core.fsmonitor=false', '-c', 'protocol.allow=never', *args]
    p = subprocess.run(cmd, input=data, capture_output=True, env=env, timeout=30)
    require(p.returncode == 0, 'CORRECTION_GIT_OPERATION_FAILED')
    return p.stdout


def _commit(root, sha):
    require(type(sha) is str and SHA.fullmatch(sha), 'CORRECTION_INVALID_COMMIT')
    require(_git(root, 'cat-file', '-t', sha) == b'commit\n', 'CORRECTION_INVALID_COMMIT')


def _blob(root, commit, path, expected=None):
    entry = _git(root, 'ls-tree', '-z', '--full-tree', commit, '--', path)
    if not entry and expected is None:
        return None
    match = re.fullmatch(rb'100644 blob ([0-9a-f]{40})\t' + re.escape(path.encode()) + rb'\x00', entry)
    require(match, 'CORRECTION_PATH_NOT_EXACT_REGULAR_BLOB')
    sha = match[1].decode()
    require(expected is None or sha == expected, 'CORRECTION_PREIMAGE_CHANGED')
    size = _git(root, 'cat-file', '-s', sha).decode().strip()
    require(size.isdecimal() and int(size) <= MAX_BLOB_BYTES, 'CORRECTION_BLOB_TOO_LARGE')
    data = _git(root, 'cat-file', 'blob', sha)
    require(S.git_blob_sha_bytes(data) == sha, 'CORRECTION_BLOB_MISMATCH')
    return data


def _json_bytes(data):
    return S.canonical_bytes(data) + b'\n'


def _day(raw):
    try:
        return type(raw) is str and date.fromisoformat(raw).isoformat() == raw
    except ValueError:
        return False


def _comparator_prices(row):
    try:
        fields = ('open', 'high', 'low', 'close')
        require(all(type(row.get(k)) in {int, float} for k in fields),
                'CORRECTION_COMPARATOR_PRICE_INVALID')
        o, h, l, c = [Decimal(str(row[k])) for k in fields]
        require(all(v.is_finite() and v > 0 for v in (o, h, l, c))
                and l <= min(o, c) <= max(o, c) <= h,
                'CORRECTION_COMPARATOR_PRICE_INVALID')
        return dict(zip(fields, (o, h, l, c)))
    except (AttributeError, InvalidOperation):
        raise CorrectionCandidateError('CORRECTION_COMPARATOR_PRICE_INVALID') from None


def _read_evidence(repo, evidence, scope):
    require(type(evidence) is Evidence and SHA.fullmatch(evidence.blob_sha)
            and EVIDENCE_PATH.fullmatch(evidence.path), 'CORRECTION_EVIDENCE_PIN_INVALID')
    _commit(repo, evidence.commit)
    value = strict_json(_blob(repo, evidence.commit, evidence.path, evidence.blob_sha))
    require(type(value) is dict and set(value) ==
            {'schema_version', 'source', 'symbol', 'asset_class', 'identity_effective_from',
             'currency', 'session', 'interval', 'adjustment_semantics', 'records'}
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and (value['source'], value['symbol'], value['asset_class'], value['identity_effective_from'],
                 value['currency'], value['session'], value['interval'], value['adjustment_semantics']) ==
            (evidence.source, scope.symbol, scope.asset_class, scope.identity_effective_from,
             'USD', 'regular', '1d', 'provider_reported'),
            'CORRECTION_EVIDENCE_IDENTITY_MISMATCH')
    require(isinstance(value['records'], list) and 0 < len(value['records']) <= 5000,
            'CORRECTION_EVIDENCE_ROWS_INVALID')
    return value['records']


def derive_outputs(repo: Path, evidence_repo: Path, scope: ApprovedCorrection,
                   *, authenticate: Callable[[ApprovedCorrection], bool]):
    """Derive exact writes. `authenticate` is a required external trust boundary."""
    require(type(scope) is ApprovedCorrection and authenticate(scope) is True,
            'CORRECTION_AUTHORITY_NOT_VERIFIED')
    require(REQUEST.fullmatch(scope.request_id) and SYMBOL.fullmatch(scope.symbol)
            and scope.asset_class in {'stocks', 'etf'}
            and SHA.fullmatch(scope.index_blob_sha) and SHA.fullmatch(scope.active_shard_blob_sha)
            and _day(scope.execution_trade_date)
            and (scope.identity_effective_from is None or _day(scope.identity_effective_from)),
            'CORRECTION_SCOPE_INVALID')
    require(type(scope.retained) is Evidence and type(scope.comparator) is Evidence
            and scope.retained.source == PRIMARY_SOURCE
            and scope.comparator.source in COMPARISON_SOURCES
            and scope.retained.blob_sha != scope.comparator.blob_sha,
            'CORRECTION_INDEPENDENT_SOURCE_REQUIRED')
    _commit(repo, scope.store_base)
    base = scope.store_base
    series = ROOT + f'series/daily/{VENDOR}/{scope.symbol}/'
    catalog = ROOT + f'series-active/daily/{VENDOR}/{scope.symbol}.json'
    require(_blob(repo, base, catalog) is None, 'CORRECTION_VERSIONED_BASIS_FORBIDDEN')
    index_path = series + '_index.json'
    old_index = _blob(repo, base, index_path, scope.index_blob_sha)
    index = strict_json(old_index)
    series_id = f'{VENDOR}:{scope.symbol}:regular:1d'
    expected = dict(series_id=series_id, provider=VENDOR, symbol=scope.symbol,
                    asset_class=scope.asset_class, interval='1d', session='regular',
                    series_semantics='daily_regular_ohlcv', adjustment_semantics='provider_reported',
                    identity_effective_from=scope.identity_effective_from)
    require(all(index.get(k) == v for k, v in expected.items()), 'CORRECTION_SERIES_IDENTITY_MISMATCH')
    require(isinstance(index.get('shards'), list) and index['shards']
            and isinstance(index.get('lineage_segments'), list), 'CORRECTION_INDEX_INVALID')
    records = []
    shards = index['shards']
    for i, meta in enumerate(shards):
        name = meta['path']
        require(re.fullmatch(r'[0-9]{6}\.json', name) and meta['start'] == len(records)
                and meta['sealed'] is (i < len(shards) - 1), 'CORRECTION_SHARD_LAYOUT_INVALID')
        raw = _blob(repo, base, series + name, meta['blob_sha'])
        doc = strict_json(raw)
        require(doc == S._shard_document(series_id, len(records), doc['records'])
                and _json_bytes(doc) == raw
                and meta == S._shard_meta(name, len(records), doc, meta['sealed']),
                'CORRECTION_SHARD_INTEGRITY_FAILED')
        records.extend(doc['records'])
    active = shards[-1]
    require(active['blob_sha'] == scope.active_shard_blob_sha
            and records and len(records) == index['record_count']
            and index['first_trade_date'] == records[0]['trade_date']
            and index['last_trade_date'] == records[-1]['trade_date']
            and records[-1]['trade_date'] < scope.execution_trade_date, 'CORRECTION_INDEX_COVERAGE_INVALID')
    prior_active = raw
    retained = _read_evidence(evidence_repo, scope.retained, scope)
    comparator = _read_evidence(evidence_repo, scope.comparator, scope)
    independent = {r['trade_date']: r for r in comparator}
    require(len(independent) == len(comparator) and all(_day(day) for day in independent),
            'CORRECTION_COMPARATOR_DATES_INVALID')
    revisions = strict_json(scope.revisions)
    require(scope.revisions == S.canonical_bytes(revisions) and isinstance(revisions, list),
            'CORRECTION_REVIEW_SCOPE_INVALID')
    active_days = {r['trade_date'] for r in records[active['start']:]}
    for rev in revisions:
        require(type(rev) is dict and set(rev) == {'trade_date', 'expected_old_row_sha256', 'fields'}
                and _day(rev['trade_date']) and isinstance(rev['fields'], dict),
                'CORRECTION_REVIEW_SCOPE_INVALID')
        day = rev['trade_date']
        require(day in active_days and day in independent and
                (scope.identity_effective_from is None or day >= scope.identity_effective_from),
                'CORRECTION_DATE_NOT_ACTIVE_OR_CORROBORATED')
        # The pure helper compares reviewed cents to the primary; verify those
        # independently against the comparator object, not caller-entered proof.
        compare = _comparator_prices(independent[day])
        for field, amount in rev['fields'].items():
            try:
                require(field in compare and type(amount) is str
                        and Decimal(amount) == compare[field].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                        'CORRECTION_COMPARATOR_PRICE_MISMATCH')
            except InvalidOperation:
                raise CorrectionCandidateError('CORRECTION_COMPARATOR_PRICE_MISMATCH') from None
    corrected, changes = prepare(records, retained, revisions, max_rows=3, tail_sessions=3)
    new_doc = S._shard_document(series_id, active['start'], corrected[active['start']:])
    require(len(S.canonical_bytes(new_doc)) <= index['shard_byte_limit'], 'CORRECTION_SHARD_LIMIT')
    index['shards'][-1] = S._shard_meta(active['path'], active['start'], new_doc, False)
    archive = ROOT + f'recovery/{scope.execution_trade_date}/{VENDOR}/reviewed-price-correction/{scope.symbol}/{scope.request_id}/'
    audit_path = archive + 'audit.json'
    manifest_path = ROOT + f'collector-state/reviewed-price-correction/{scope.request_id}-outputs.json'
    for path in (archive + '_index.json', archive + active['path'], audit_path, manifest_path):
        require(_blob(repo, base, path) is None, 'CORRECTION_IMMUTABLE_OUTPUT_EXISTS')
    audit = dict(schema_version=1, kind='reviewed_price_only_correction', symbol=scope.symbol,
                 store_base=base, request_id=scope.request_id, execution_trade_date=scope.execution_trade_date,
                 retained=asdict(scope.retained), comparator=asdict(scope.comparator),
                 scope_sha256=hashlib.sha256(scope.revisions).hexdigest(),
                 prior_index_blob=scope.index_blob_sha, prior_shard_blob=scope.active_shard_blob_sha,
                 changes=changes, volume_written=False, added_dates=[])
    audit_raw = _json_bytes(audit)
    record_positions = {row['trade_date']: pos for pos, row in enumerate(records)}
    for item in changes:
        pos = record_positions[item['trade_date']]
        index['lineage_segments'].append(dict(ingest_kind='reviewed_price_only_correction',
            start=pos, end=pos, audit_path=audit_path[len(ROOT):],
            audit_blob_sha=S.git_blob_sha_bytes(audit_raw), volume_written=False))
    outputs = {index_path: _json_bytes(index), series + active['path']: _json_bytes(new_doc),
               archive + '_index.json': old_index, archive + active['path']: prior_active,
               audit_path: audit_raw}
    manifest = dict(schema_version=1, request_id=scope.request_id, trade_date=scope.execution_trade_date,
                    stage='reviewed_price_only_correction', required_outputs=[
                        dict(path=p[len(ROOT):], blob_sha=S.git_blob_sha_bytes(outputs[p]))
                        for p in sorted(outputs)])
    outputs[manifest_path] = _json_bytes(manifest)
    return manifest_path, outputs


def prepare_candidate(repo: Path, evidence_repo: Path, scope: ApprovedCorrection, *, authenticate):
    manifest, outputs = derive_outputs(repo, evidence_repo, scope, authenticate=authenticate)
    with tempfile.TemporaryDirectory(prefix='reviewed-price-index-') as temp:
        index = str(Path(temp) / 'index')
        _git(repo, 'read-tree', scope.store_base, index=index)
        for path, raw in sorted(outputs.items()):
            blob = _git(repo, 'hash-object', '-w', '--stdin', data=raw).decode().strip()
            _git(repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', index=index)
        tree = _git(repo, 'write-tree', index=index).decode().strip()
        commit = _git(repo, '-c', 'user.name=Candidate Builder', '-c',
                      'user.email=candidate-builder@example.invalid', '-c', 'commit.gpgsign=false',
                      'commit-tree', tree, '-p', scope.store_base,
                      '-m', 'Prepare reviewed daily price correction ' + scope.request_id).decode().strip()
    verify_candidate(repo, evidence_repo, scope, commit, authenticate=authenticate)
    return commit, manifest


def verify_candidate(repo: Path, evidence_repo: Path, scope: ApprovedCorrection, candidate: str, *, authenticate):
    manifest, outputs = derive_outputs(repo, evidence_repo, scope, authenticate=authenticate)
    _commit(repo, candidate)
    parents = _git(repo, 'rev-list', '--parents', '-n', '1', candidate).decode().split()
    require(parents == [candidate, scope.store_base], 'CORRECTION_CANDIDATE_PARENT_CHANGED')
    paths = _git(repo, 'diff-tree', '-r', '--no-commit-id', '--name-only', '-z',
                 scope.store_base, candidate).decode().rstrip('\0').split('\0')
    require(set(paths) == set(outputs), 'CORRECTION_CANDIDATE_SCOPE_CHANGED')
    for path, raw in outputs.items():
        require(_blob(repo, candidate, path, S.git_blob_sha_bytes(raw)) == raw,
                'CORRECTION_CANDIDATE_BYTES_CHANGED')
    require(len(_verify_required_manifest(repo, candidate, manifest)) == len(outputs) - 1,
            'CORRECTION_CANDIDATE_MANIFEST_INCOMPLETE')
    return manifest


def install_local_candidate(repo: Path, evidence_repo: Path, scope: ApprovedCorrection,
                            candidate: str, *, authenticate):
    """CAS a pre-created local review ref; deliberately cannot update main.

    This is an isolated staging operation, not a deployment or a remote action.
    The owner must initialize the exact scope's review ref at the approved base
    before calling, then independently authorize any eventual production writer.
    """
    manifest = verify_candidate(repo, evidence_repo, scope, candidate, authenticate=authenticate)
    review_ref = 'refs/reviewed-price-correction/' + scope.request_id
    # Only update the literal local review ref. The expected old object makes
    # an intervening change fail without silently repinning the candidate.
    try:
        _git(repo, 'update-ref', '--no-deref', review_ref, candidate, scope.store_base)
    except CorrectionCandidateError:
        raise CorrectionCandidateError('CORRECTION_LOCAL_REVIEW_REF_CAS_FAILED') from None
    require(_git(repo, 'rev-parse', '--verify', review_ref).decode().strip() == candidate,
            'CORRECTION_LOCAL_READBACK_UNCONFIRMED')
    return review_ref, manifest
