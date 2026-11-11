"""Synthetic-only tests: every Git write targets a temporary local bare fixture."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/reconcile_retained_daily.py'
SCHEMA = ROOT / 'schemas/retained-daily-reconciliation.schema.json'
DATES = ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
REQUEST = {
    'schema_version': 1, 'mode': 'retained_daily_append',
    'provider': 'twelve_data_basic', 'symbol': 'GLD',
    'expected_store_sha': '33217ffbc356b1006605b9cd736e7be7fd116e18',
    'base_advance_policy': 'nov10_close_artifacts_only_v1',
    'source_candidate_sha': 'e2b6227de5d026e1d01216c3c5ed6a63c9d670ee',
    'source_tail_blob_sha': 'e63ca19a19ae8b0f948a6b8c38eabda03127fb6a',
    'expected_index_blob_sha': '89fc17e2680c86446e9082df6b5913e32e04e605',
    'expected_shard_blob_shas': {
        '000000.json': 'ee576daed8557ee49fdd30229aa2bc2bbf01ed93',
        '000128.json': '0aee5a9cb428b329cc5d752f324db7ce836f6cc3',
        '000256.json': 'f23ab9254ab9b8bf36868ef33b54c96d27e8ab94',
    },
    'addition_dates': DATES,
    'additions_sha256': '7f5634906e6ac316dcef735b18dbad1ae959593c36951fcd52e1037a3a2ae2d3',
    'conflict_policy': 'preserve_existing',
    'expected_conflicts': [{'trade_date': '2026-10-30', 'fields': ['volume']}],
    'budget_policy': 'preserve_all_bytes',
}


def cli(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                          text=True, capture_output=True, env=env)


def test_request_validation_emits_only_safe_exact_checkout_identities():
    result = cli('--validate-request-only', '--request-json', json.dumps(REQUEST))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'expected_store_sha': REQUEST['expected_store_sha'],
        'source_candidate_sha': REQUEST['source_candidate_sha'],
    }
    assert result.stderr == ''


@pytest.mark.parametrize('mutation', ['extra', 'symbol', 'dates', 'base', 'source', 'tail', 'index', 'sealed', 'digest', 'conflicts', 'budget', 'base_policy', 'boolean_version'])
def test_request_refuses_scope_or_identity_changes(mutation):
    request = copy.deepcopy(REQUEST)
    if mutation == 'extra': request['private_payload'] = 'PRIVATE_VALUE'
    elif mutation == 'symbol': request['symbol'] = 'SPY'
    elif mutation == 'dates': request['addition_dates'].append('2026-11-10')
    elif mutation in ('base', 'source', 'tail', 'index', 'digest'):
        field = {'base': 'expected_store_sha', 'source': 'source_candidate_sha', 'tail': 'source_tail_blob_sha', 'index': 'expected_index_blob_sha', 'digest': 'additions_sha256'}[mutation]
        request[field] = '0' * len(request[field])
    elif mutation == 'sealed': request['expected_shard_blob_shas']['000000.json'] = '0' * 40
    elif mutation == 'conflicts': request['expected_conflicts'] = []
    elif mutation == 'budget': request['budget_policy'] = 'reset'
    elif mutation == 'base_policy': request['base_advance_policy'] = 'arbitrary_descendants'
    else: request['schema_version'] = True
    result = cli('--validate-request-only', '--request-json', json.dumps(request))
    assert result.returncode != 0
    assert 'PRIVATE_VALUE' not in result.stdout + result.stderr
    assert 'Traceback' not in result.stderr
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, json.loads(SCHEMA.read_text()))


@pytest.mark.parametrize('payload', ['{"schema_version":1,"schema_version":1}', 'NaN', '[1]', '{"private":"PRIVATE_VALUE"', '{"x":Infinity}'])
def test_request_rejects_non_strict_json_without_echo(payload):
    result = cli('--validate-request-only', '--request-json', payload)
    assert result.returncode != 0
    assert 'PRIVATE_VALUE' not in result.stdout + result.stderr
    assert 'Traceback' not in result.stderr


def test_schema_accepts_only_the_exact_approved_contract():
    jsonschema.validate(REQUEST, json.loads(SCHEMA.read_text()))

PREFIX = 'data/market-data/'
SERIES = PREFIX + 'series/daily/twelve_data_basic/GLD/'
INVENTORY = PREFIX + 'collector-state/market-data-inventory.json'
BUDGET = PREFIX + 'collector-state/2026-11/2026-11-10-twelve-data-budget.json'
OLD_BUDGET = PREFIX + 'collector-state/2026-10/2026-10-30-twelve-data-budget.json'
REPORT = PREFIX + 'collector-state/retained-daily-reconciliation/987-1.json'
MANIFEST = PREFIX + 'collector-state/retained-daily-reconciliation/987-1-outputs.json'
COMPUTE = 'c' * 40
TIMESTAMP = '2026-11-10T12:00:00+00:00'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def write(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode(value))


@pytest.fixture
def fixture(tmp_path):
    from datetime import date, timedelta
    from scripts import market_data_store as store
    r = importlib.import_module('scripts.reconcile_retained_daily')
    writer = tmp_path / 'writer'
    source = tmp_path / 'source'
    remote = tmp_path / 'fixture.git'
    writer.mkdir()
    git(tmp_path, 'init', '--bare', '-q', str(remote))
    git(writer, 'init', '-q', '-b', 'main')
    git(writer, 'config', 'user.name', 'Synthetic fixture')
    git(writer, 'config', 'user.email', 'fixture@example.invalid')
    date_list = []
    day = date(2026, 10, 30)
    while len(date_list) < 310:
        if day.weekday() < 5: date_list.append(day.isoformat())
        day -= timedelta(days=1)
    rows = [{'trade_date': d, 'open': 100, 'high': 102, 'low': 99,
             'close': 101, 'volume': 1000 + i} for i, d in enumerate(reversed(date_list))]
    store.append_daily_bars(writer / PREFIX, provider='twelve_data_basic', symbol='GLD',
                           asset_class='etf', series_semantics='daily_regular_ohlcv', bars=rows,
                           lineage={'ingest_kind': 'synthetic_baseline', 'source': 'fixture'})
    index = json.loads((writer / (SERIES + '_index.json')).read_text())
    index['baseline_initialization'] = {'status': 'accepted', 'fixture': True}
    write(writer, SERIES + '_index.json', index)
    store.append_daily_bars(writer / PREFIX, provider='twelve_data_basic', symbol='SPY',
                           asset_class='etf', bars=[{'trade_date': '2026-11-09', 'close': 42}])
    write(writer, BUDGET, {'total': 200, 'requests': [{'id': 'retained-acquisition', 'charged': True}]})
    write(writer, OLD_BUDGET, {'total': 67})
    write(writer, PREFIX + 'collector-state/original-run-receipt.json', {'status': 'failed', 'must_remain': True})
    write(writer, PREFIX + 'archives/original-run.json', {'retained': True})
    write(writer, INVENTORY, {'old_inventory': True})
    write(writer, PREFIX + 'snapshots/unrelated.json', {'unrelated': True})
    git(writer, 'add', '.')
    git(writer, 'commit', '-qm', 'synthetic base')
    base = git(writer, 'rev-parse', 'HEAD')
    git(writer, 'remote', 'add', 'origin', str(remote))
    git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    git(tmp_path, 'clone', '-q', '--no-local', str(writer), str(source))
    git(source, 'config', 'user.name', 'Synthetic fixture')
    git(source, 'config', 'user.email', 'fixture@example.invalid')
    tail = json.loads((source / (SERIES + '000256.json')).read_text())
    tail['records'][-1]['volume'] += 99
    additions = [{'trade_date': d, 'open': 105, 'high': 107, 'low': 104,
                  'close': 106, 'volume': 1500 + i} for i, d in enumerate(DATES)]
    tail['records'] += additions
    write(source, SERIES + '000256.json', tail)
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'retained unpublished source object')
    retained = git(source, 'rev-parse', 'HEAD')
    pins = r.PinnedInputs(
        expected_store_sha=base, source_candidate_sha=retained,
        source_tail_blob_sha=git(source, 'rev-parse', retained + ':' + SERIES + '000256.json'),
        expected_index_blob_sha=git(writer, 'rev-parse', base + ':' + SERIES + '_index.json'),
        expected_shard_blob_shas=tuple((name, git(writer, 'rev-parse', base + ':' + SERIES + name))
                                     for name in ['000000.json', '000128.json', '000256.json']),
        additions_sha256=hashlib.sha256(encode(additions).rstrip(b'\n')).hexdigest(),
    )
    return r, writer, source, remote, pins


def prepare(fixture):
    r, writer, source, _, pins = fixture
    return r.prepare_candidate(writer, source, pins=pins, compute_sha=COMPUTE,
                               request_id='987-1', generated_at=TIMESTAMP)


def install(fixture, changes=None):
    r, writer, source, _, pins = fixture
    prepared = prepare(fixture)
    if changes is None:
        return r.commit_candidate(writer, prepared), prepared
    for path, raw in changes.items():
        target = writer / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    git(writer, 'add', '.')
    git(writer, 'commit', '-qm', 'adversarial candidate')
    return git(writer, 'rev-parse', 'HEAD'), prepared


def local_guard(fixture):
    _, writer, _, remote, _ = fixture
    def guard():
        assert git(writer, 'remote', 'get-url', 'origin') == str(remote)
        assert remote.parent == writer.parent and remote.name == 'fixture.git'
        assert git(remote, 'rev-parse', '--is-bare-repository') == 'true'
    return guard


def validate(fixture, sha):
    r, writer, source, _, pins = fixture
    return r.validate_candidate(writer, source, candidate_sha=sha, pins=pins,
                                compute_sha=COMPUTE, request_id='987-1')


def publish(fixture, sha):
    r, writer, source, _, pins = fixture
    return r.publish_candidate(writer, source, candidate_sha=sha, pins=pins,
                               compute_sha=COMPUTE, request_id='987-1',
                               pre_push_guard=local_guard(fixture))


def rehash_manifest(changes):
    manifest = json.loads(changes[MANIFEST])
    for output in manifest['required_outputs']:
        path = PREFIX + output['path']
        if path in changes: output['blob_sha'] = blob(changes[path])
    changes[MANIFEST] = encode(manifest)


def test_exact_append_preserves_existing_history_all_budgets_and_receipts(fixture):
    from scripts import market_data_store as store
    r, writer, source, remote, pins = fixture
    before = store.read_daily_series(writer / PREFIX, provider='twelve_data_basic', symbol='GLD')
    prepared = prepare(fixture)
    assert git(writer, 'status', '--porcelain') == ''
    assert set(prepared.changes) == {SERIES + '000256.json', SERIES + '_index.json', REPORT, INVENTORY, MANIFEST}
    sha = r.commit_candidate(writer, prepared)
    after = store.read_daily_series(writer / PREFIX, provider='twelve_data_basic', symbol='GLD')
    assert len(after) == 316 and after[:310] == before
    assert [row['trade_date'] for row in after[310:]] == DATES
    for path in [BUDGET, OLD_BUDGET, PREFIX + 'archives/original-run.json', PREFIX + 'collector-state/original-run-receipt.json', SERIES + '000000.json', SERIES + '000128.json']:
        assert git(writer, 'rev-parse', sha + ':' + path) == git(writer, 'rev-parse', pins.expected_store_sha + ':' + path)
    report = json.loads((writer / REPORT).read_text())
    assert report['status'] == 'CANDIDATE_VALIDATED'
    assert report['production_recovery_accepted'] is False
    assert report['publication_requires_terminal_receipt'] is True
    assert report['provider_requests'] == report['additional_debits'] == report['refunds'] == 0
    assert report['source_candidate_publication'] == 'NOT_ASSERTED'
    assert report['source_candidate_sha'] == pins.source_candidate_sha
    assert report['conflicts'] == [{'trade_date': '2026-10-30', 'fields': ['volume'], 'resolution': 'preserve_existing'}]
    assert len(report['unchanged_budget_blobs']) == 2
    inventory = json.loads((writer / INVENTORY).read_text())
    assert {s['symbol']: s['record_count'] for s in inventory['daily_series_summary']['series']} == {'GLD': 316, 'SPY': 1}
    closure = validate(fixture, sha)
    assert {item['path'] for item in closure} == {SERIES + x for x in ['_index.json', '000000.json', '000128.json', '000256.json']} | {REPORT, INVENTORY}


def test_full_retained_tail_still_fails_real_overlap_check(fixture):
    from scripts import market_data_store as store
    _, writer, source, _, _ = fixture
    tail = json.loads((source / (SERIES + '000256.json')).read_text())
    with pytest.raises(store.MarketDataConflict):
        store.append_daily_bars(writer / PREFIX, provider='twelve_data_basic', symbol='GLD', asset_class='etf',
                                series_semantics='daily_regular_ohlcv', bars=tail['records'])


@pytest.mark.parametrize('target', ['base_head', 'source_head', 'dirty_base', 'dirty_source', 'source_blob', 'index_blob', 'sealed_blob', 'addition_digest', 'symlink'])
def test_input_drift_is_rejected_without_staging(fixture, target):
    from dataclasses import replace
    r, writer, source, _, pins = fixture
    if target == 'base_head': pins = replace(pins, expected_store_sha='0' * 40)
    elif target == 'source_head': pins = replace(pins, source_candidate_sha='0' * 40)
    elif target == 'dirty_base': (writer / BUDGET).write_text('PRIVATE_VALUE')
    elif target == 'dirty_source': (source / (SERIES + '000256.json')).write_text('PRIVATE_VALUE')
    elif target == 'source_blob': pins = replace(pins, source_tail_blob_sha='0' * 40)
    elif target == 'index_blob': pins = replace(pins, expected_index_blob_sha='0' * 40)
    elif target == 'sealed_blob': pins = replace(pins, expected_shard_blob_shas=(('000000.json', '0' * 40), *pins.expected_shard_blob_shas[1:]))
    elif target == 'addition_digest': pins = replace(pins, additions_sha256='0' * 64)
    else: (writer / 'data/market-data/private-link').symlink_to('/tmp')
    with pytest.raises(r.ReconciliationError):
        r.prepare_candidate(writer, source, pins=pins, compute_sha=COMPUTE, request_id='987-1', generated_at=TIMESTAMP)
    assert not (writer / REPORT).exists()
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == fixture[-1].expected_store_sha


@pytest.mark.parametrize('target', ['old_history', 'new_value', 'baseline', 'lineage', 'new_lineage', 'budget', 'historical_budget', 'unrelated', 'report', 'inventory', 'closure', 'new_file', 'mode'])
def test_rehashed_candidate_tampering_fails_independent_validation(fixture, target):
    r, writer, source, _, pins = fixture
    changes = dict(prepare(fixture).changes)
    if target in ('old_history', 'new_value'):
        tail = json.loads(changes[SERIES + '000256.json'])
        tail['records'][0 if target == 'old_history' else -1]['volume'] += 1
        changes[SERIES + '000256.json'] = encode(tail)
        index = json.loads(changes[SERIES + '_index.json'])
        from scripts import market_data_store as store
        index['shards'][-1] = store._shard_meta('000256.json', 256, tail, False)
        changes[SERIES + '_index.json'] = encode(index)
    elif target in ('baseline', 'lineage', 'new_lineage'):
        index = json.loads(changes[SERIES + '_index.json'])
        if target == 'baseline': index['baseline_initialization'] = {'status': 'forged'}
        elif target == 'lineage': index['lineage_segments'][0]['source'] = 'forged'
        else: index['lineage_segments'][-1]['source_candidate_sha'] = '0' * 40
        changes[SERIES + '_index.json'] = encode(index)
    elif target in ('budget', 'historical_budget'): changes[BUDGET if target == 'budget' else OLD_BUDGET] = encode({'total': 0})
    elif target == 'unrelated': changes[PREFIX + 'snapshots/unrelated.json'] = encode({'changed': True})
    elif target == 'report':
        report = json.loads(changes[REPORT]); report['production_recovery_accepted'] = True; changes[REPORT] = encode(report)
    elif target == 'inventory':
        inventory = json.loads(changes[INVENTORY]); inventory['daily_series_summary']['series'].pop(); changes[INVENTORY] = encode(inventory)
    elif target == 'closure':
        manifest = json.loads(changes[MANIFEST]); manifest['required_outputs'].pop(); changes[MANIFEST] = encode(manifest)
    elif target == 'new_file': changes['OUTSIDE.md'] = b'out of scope\n'
    rehash_manifest(changes)
    sha, _ = install(fixture, changes)
    if target == 'mode':
        (writer / (SERIES + '000256.json')).chmod(0o755)
        git(writer, 'add', '.')
        git(writer, 'commit', '--amend', '--no-edit', '-q')
        sha = git(writer, 'rev-parse', 'HEAD')
    with pytest.raises(r.ReconciliationError): validate(fixture, sha)
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == pins.expected_store_sha


def test_real_publication_closes_six_objects_and_retention_without_claiming_old_recovery(fixture):
    _, writer, source, remote, pins = fixture
    assert git(source, 'tag', '--list') == ''  # Source tag absence is not publication proof.
    sha, _ = install(fixture)
    receipt = publish(fixture, sha)
    assert receipt['status'] == 'VERIFIED'
    assert receipt['candidate_sha'] == receipt['remote_main_sha'] == sha
    assert receipt['production_recovery_accepted'] is False
    assert len(receipt['verified_required_outputs']) == 6
    assert git(writer, 'ls-remote', 'origin', receipt['durability_ref']).split()[0] == sha
    assert git(writer, 'rev-parse', 'HEAD') == sha  # No metadata commit after publication.


def test_drift_before_push_does_not_repin_or_publish(fixture):
    r, writer, source, remote, pins = fixture
    sha, _ = install(fixture)
    git(writer, 'checkout', '-q', '--detach', pins.expected_store_sha)
    git(writer, 'commit', '--allow-empty', '-qm', 'competing writer')
    competing = git(writer, 'rev-parse', 'HEAD')
    git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    git(writer, 'checkout', '-q', '--detach', sha)
    with pytest.raises(r.ReconciliationError): publish(fixture, sha)
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == competing


def test_conflicting_retention_is_unconfirmed_without_rollback_or_retarget(fixture):
    _, writer, _, _, pins = fixture
    sha, _ = install(fixture)
    ref = 'refs/tags/market-data-read/' + sha
    git(writer, 'push', '-q', 'origin', pins.expected_store_sha + ':' + ref)
    receipt = publish(fixture, sha)
    assert receipt == {'status': 'UNCONFIRMED', 'candidate_sha': sha,
                       'reason': 'RETAINED_PUBLICATION_UNCONFIRMED', 'production_recovery_accepted': False}
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == sha
    assert git(writer, 'ls-remote', 'origin', ref).split()[0] == pins.expected_store_sha

def test_terminal_receipt_must_name_exact_candidate_retention_ref(fixture, monkeypatch):
    r, writer, _, _, _ = fixture
    sha, _ = install(fixture)
    original = r.verifier.verify_publication
    def wrong_ref(*args, **kwargs):
        receipt = original(*args, **kwargs)
        receipt['durability_ref'] = 'refs/tags/not-the-candidate'
        return receipt
    monkeypatch.setattr(r.verifier, 'verify_publication', wrong_ref)
    receipt = publish(fixture, sha)
    assert receipt['status'] == 'UNCONFIRMED'
    assert 'not-the-candidate' not in json.dumps(receipt)
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == sha


def test_lost_fact_push_acknowledgement_never_retries_or_claims_success(fixture, monkeypatch):
    r, writer, source, remote, pins = fixture
    sha, _ = install(fixture)
    original = r.git_bytes
    pushes = []
    def lost_ack(root, *args):
        result = original(root, *args)
        if args[:2] == ('push', 'origin'):
            pushes.append(args)
            raise r.ReconciliationError('PRIVATE_VALUE from remote')
        return result
    monkeypatch.setattr(r, 'git_bytes', lost_ack)
    receipt = publish(fixture, sha)
    assert receipt['status'] == 'UNCONFIRMED'
    assert receipt['candidate_sha'] == sha
    assert 'PRIVATE_VALUE' not in json.dumps(receipt)
    assert len(pushes) == 1
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == sha
    assert git(writer, 'ls-remote', 'origin', 'refs/tags/market-data-read/' + sha) == ''


def test_post_push_main_drift_is_unconfirmed_and_not_overwritten(fixture, monkeypatch):
    r, writer, source, remote, pins = fixture
    sha, _ = install(fixture)
    original = r.git_bytes
    competing = []
    def race(root, *args):
        result = original(root, *args)
        if args[:2] == ('push', 'origin'):
            git(writer, 'commit', '--allow-empty', '-qm', 'race after push')
            competing.append(git(writer, 'rev-parse', 'HEAD'))
            git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
            git(writer, 'checkout', '-q', '--detach', sha)
        return result
    monkeypatch.setattr(r, 'git_bytes', race)
    receipt = publish(fixture, sha)
    assert receipt['status'] == 'UNCONFIRMED'
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == competing[0]


def test_production_cli_has_no_local_or_test_bypass(fixture):
    _, writer, source, _, pins = fixture
    env = dict(os.environ)
    env.pop('GITHUB_ACTIONS', None)
    result = cli('--request-json', json.dumps(REQUEST), '--compute-sha', COMPUTE,
                 '--store-repo', str(writer), '--candidate-repo', str(source), '--request-id', '987-1', env=env)
    assert result.returncode != 0
    assert json.loads(result.stderr)['status'] == 'REJECTED'
    assert git(writer, 'rev-parse', 'HEAD') == pins.expected_store_sha
    assert git(writer, 'status', '--porcelain') == ''
    bypass = cli('--request-json', json.dumps(REQUEST), '--local-test', '--private-value', 'PRIVATE_VALUE')
    assert bypass.returncode != 0 and 'PRIVATE_VALUE' not in bypass.stdout + bypass.stderr


def test_reconciliation_has_a_pinned_candidate_api():
    r = importlib.import_module('scripts.reconcile_retained_daily')
    assert callable(getattr(r, 'prepare_candidate', None)), 'No immutable retained-candidate implementation exists'


@pytest.mark.parametrize('field,value', [('GITHUB_ACTIONS', 'false'), ('GITHUB_EVENT_NAME', 'schedule'),
                                        ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_SHA', '0' * 40),
                                        ('GITHUB_WORKFLOW', 'Market Data Collector'),
                                        ('GITHUB_WORKFLOW_REF', 'wrong/route@refs/heads/main')])
def test_official_execution_surface_rejects_wrong_context(fixture, field, value):
    r, writer, _, _, pins = fixture
    environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': pins.expected_store_sha,
                   'GITHUB_REPOSITORY': 'yuatom/stock-market-data',
                   'GITHUB_WORKFLOW': 'Retained Daily Reconciliation',
                   'GITHUB_WORKFLOW_REF': 'yuatom/stock-market-data/.github/workflows/reconcile-retained-daily.yml@refs/heads/main',
                   'GITHUB_RUN_ID': '987', 'GITHUB_RUN_ATTEMPT': '1'}
    environment[field] = value
    with pytest.raises(r.ReconciliationError):
        r.require_execution_surface(writer, pins.expected_store_sha, '987-1', environment=environment)


def test_compute_main_drift_blocks_publication_before_any_push(fixture, monkeypatch):
    r, writer, source, remote, pins = fixture
    sha, _ = install(fixture)
    def drift_guard():
        raise r.ReconciliationError('RETAINED_COMPUTE_MAIN_CHANGED')
    with pytest.raises(r.ReconciliationError):
        r.publish_candidate(writer, source, candidate_sha=sha, pins=pins,
                            compute_sha=COMPUTE, request_id='987-1', pre_push_guard=drift_guard)
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == pins.expected_store_sha


def official_environment(sha):
    return {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': sha,
            'GITHUB_REPOSITORY': 'yuatom/stock-market-data',
            'GITHUB_WORKFLOW': 'Retained Daily Reconciliation',
            'GITHUB_WORKFLOW_REF': 'yuatom/stock-market-data/.github/workflows/reconcile-retained-daily.yml@refs/heads/main',
            'GITHUB_RUN_ID': '987', 'GITHUB_RUN_ATTEMPT': '1'}


def simulated_official_origin(r, monkeypatch):
    original = r.git_text
    def external_origin(root, *args):
        if args[:2] == ('remote', 'get-url'):
            return 'https://github.com/yuatom/stock-market-data.git'
        return original(root, *args)
    monkeypatch.setattr(r, 'git_text', external_origin)


def test_official_context_requires_exact_clean_source_and_live_compute_main(fixture, monkeypatch):
    r, writer, source, remote, pins = fixture
    simulated_official_origin(r, monkeypatch)
    environment = official_environment(pins.expected_store_sha)
    r.require_execution_surface(writer, pins.expected_store_sha, '987-1', environment=environment)
    git(writer, 'commit', '--allow-empty', '-qm', 'advance compute fixture main')
    newer = git(writer, 'rev-parse', 'HEAD')
    git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    git(writer, 'checkout', '-q', '--detach', pins.expected_store_sha)
    with pytest.raises(r.ReconciliationError, match='RETAINED_COMPUTE_MAIN_CHANGED'):
        r.require_execution_surface(writer, pins.expected_store_sha, '987-1', environment=environment)
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == newer


def test_official_context_rejects_dirty_compute_source(fixture, monkeypatch):
    r, writer, _, _, pins = fixture
    simulated_official_origin(r, monkeypatch)
    (writer / 'unexpected.py').write_text('PRIVATE_VALUE')
    with pytest.raises(r.ReconciliationError, match='RETAINED_CHECKOUT_DIRTY'):
        r.require_execution_surface(writer, pins.expected_store_sha, '987-1', environment=official_environment(pins.expected_store_sha))


@pytest.mark.parametrize('collision', [REPORT, MANIFEST])
def test_existing_receipt_names_are_never_overwritten(fixture, collision):
    from dataclasses import replace
    r, writer, source, _, pins = fixture
    write(writer, collision, {'existing_receipt': True})
    git(writer, 'add', '.')
    git(writer, 'commit', '-qm', 'existing receipt')
    pins = replace(pins, expected_store_sha=git(writer, 'rev-parse', 'HEAD'))
    with pytest.raises(r.ReconciliationError, match='RETAINED_OUTPUT_COLLISION'):
        r.prepare_candidate(writer, source, pins=pins, compute_sha=COMPUTE, request_id='987-1', generated_at=TIMESTAMP)
    assert json.loads((writer / collision).read_text()) == {'existing_receipt': True}
    assert git(writer, 'status', '--porcelain') == ''


@pytest.mark.parametrize('override', [('remote.origin.pushurl', '/tmp/forbidden.git'),
                                     ('url.file:///tmp/evil.insteadOf', 'https://github.com/'),
                                     ('core.hooksPath', '/tmp/evil')])
def test_production_origin_rejects_routing_and_hook_overrides(fixture, override):
    r, writer, _, _, _ = fixture
    git(writer, 'remote', 'set-url', 'origin', 'https://github.com/yuatom/stock-market-data.git')
    git(writer, 'config', override[0], override[1])
    with pytest.raises(r.ReconciliationError): r._require_origin(writer, 'yuatom/stock-market-data')


def test_ignored_output_collision_fails_before_mutating_any_candidate_file(fixture):
    r, writer, _, _, pins = fixture
    prepared = prepare(fixture)
    (writer / '.git/info/exclude').write_text('/data/market-data/collector-state/retained-daily-reconciliation/\n')
    write(writer, MANIFEST, {'existing_ignored_receipt': True})
    before = (writer / (SERIES + '000256.json')).read_bytes()
    with pytest.raises(r.ReconciliationError, match='RETAINED_OUTPUT_COLLISION'):
        r.commit_candidate(writer, prepared)
    assert (writer / (SERIES + '000256.json')).read_bytes() == before
    assert git(writer, 'status', '--porcelain') == ''


def test_production_origin_accepts_real_checkout_config_without_self_rejection(fixture):
    r, writer, _, _, _ = fixture
    git(writer, 'remote', 'set-url', 'origin', 'https://github.com/yuatom/stock-market-data.git')
    r._require_origin(writer, 'yuatom/stock-market-data')


@pytest.mark.parametrize('key', ['remote.origin.receivepack', 'remote.origin.uploadpack'])
def test_production_origin_rejects_custom_transport_executables(fixture, key):
    r, writer, _, _, _ = fixture
    git(writer, 'remote', 'set-url', 'origin', 'https://github.com/yuatom/stock-market-data.git')
    git(writer, 'config', key, '/tmp/forbidden-program')
    with pytest.raises(r.ReconciliationError): r._require_origin(writer, 'yuatom/stock-market-data')


def test_production_origin_rejects_external_vcs_transport_helper(fixture):
    r, writer, _, _, _ = fixture
    git(writer, 'remote', 'set-url', 'origin', 'https://github.com/yuatom/stock-market-data.git')
    git(writer, 'config', 'remote.origin.vcs', 'forbidden-helper')
    with pytest.raises(r.ReconciliationError):
        r._require_origin(writer, 'yuatom/stock-market-data')



@pytest.mark.parametrize('previous', ['4b511e8c8d211adfaff0e1ddbcce75c070a32781',
                                      '3a516c3d097cdf57f9f9e6e1c5c8a0a6399c0ad5'])
def test_previous_store_pin_is_rejected_after_exact_reviewed_repin(previous):
    request = copy.deepcopy(REQUEST)
    request['expected_store_sha'] = previous
    result = cli('--validate-request-only', '--request-json', json.dumps(request))
    assert result.returncode == 1
    assert json.loads(result.stderr)['reason'] == 'RETAINED_REQUEST_INVALID'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, json.loads(SCHEMA.read_text()))


def test_advanced_store_preserves_close_proofs_budgets_and_rebuilds_inventory(fixture):
    from dataclasses import replace
    from scripts.build_market_data_inventory import build_inventory
    from scripts import market_data_store as store
    r, writer, source, remote, pins = fixture
    original_rows = store.read_daily_series(writer / PREFIX, provider='twelve_data_basic', symbol='GLD')
    proof = PREFIX + 'proofs/deterministic-metrics/2026-11/2026-11-10/close/' + COMPUTE + '/close-171301-et.json'
    proof_latest = PREFIX + 'proofs/deterministic-metrics/2026-11/2026-11-10/close/latest.json'
    receipt = PREFIX + 'collector-state/2026-11/2026-11-10-close-fixture-publication.json'
    missing = PREFIX + 'collector-state/2026-11/2026-11-10-close-missing.json'
    snapshot = 'snapshots/2026-11/2026-11-10/close/synthetic-close.json'
    snapshot_latest = PREFIX + 'snapshots/2026-11/2026-11-10/close/latest.json'
    advanced_files = {
        proof: {'synthetic_proof': True, 'must_preserve': ['all', 'bytes']},
        proof_latest: {'proof_path': proof[len(PREFIX):], 'proof_blob_sha': 'a' * 40},
        receipt: {'status': 'durability_verified', 'synthetic_close': True},
        missing: {'stage': 'close', 'missing': [{'symbol': 'SYNTHETIC', 'reason': 'fixture'}]},
        PREFIX + snapshot: {'trade_date': '2026-11-10', 'stage': 'close', 'snapshot_id': 'synthetic-close',
                            'generated_at': TIMESTAMP, 'actual_data_cutoff': TIMESTAMP,
                            'data_refs': [{'provider': 'fixture', 'window': 'close'}], 'missing': []},
        snapshot_latest: {'snapshot_path': snapshot},
        INVENTORY: {'deliberately_stale': True},
    }
    for path, document in advanced_files.items():
        write(writer, path, document)
    git(writer, 'add', '.')
    git(writer, 'commit', '-qm', 'synthetic unrelated close publication')
    new_base = git(writer, 'rev-parse', 'HEAD')
    assert git(writer, 'rev-parse', new_base + '^') == pins.expected_store_sha
    base, base_entries = r._tree(writer, new_base)
    # Moving main is still rejected until the explicit new immutable pin is supplied.
    with pytest.raises(r.ReconciliationError, match='RETAINED_CHECKOUT_DRIFT'):
        prepare(fixture)
    advanced_pins = replace(pins, expected_store_sha=new_base)
    advanced_fixture = (r, writer, source, remote, advanced_pins)
    prepared = prepare(advanced_fixture)
    sha = r.commit_candidate(writer, prepared)
    assert git(writer, 'rev-parse', sha + '^') == new_base
    assert set(prepared.changes) == {SERIES + '000256.json', SERIES + '_index.json', REPORT, INVENTORY, MANIFEST}
    candidate, candidate_entries = r._tree(writer, sha)
    for path, raw in base.items():
        if path not in prepared.changes:
            assert candidate[path] == raw
            assert candidate_entries[path] == base_entries[path]
    for path in set(advanced_files) - {INVENTORY}:
        assert candidate[path] == base[path]
    budgets = {path: blob(raw) for path, raw in base.items()
               if path.startswith(PREFIX + 'collector-state/') and 'budget' in Path(path).name.lower()}
    report = json.loads(candidate[REPORT])
    assert report['store_start_sha'] == new_base
    assert report['unchanged_budget_blobs'] == budgets
    assert report['provider_requests'] == report['additional_debits'] == report['refunds'] == 0
    assert all(candidate[path] == base[path] for path in budgets)
    after = store.read_daily_series(writer / PREFIX, provider='twelve_data_basic', symbol='GLD')
    assert after[:310] == original_rows and [row['trade_date'] for row in after[310:]] == DATES
    inventory = json.loads(candidate[INVENTORY])
    expected_inventory = build_inventory(writer / PREFIX)
    expected_inventory['generated_at'] = TIMESTAMP
    assert inventory == expected_inventory
    assert inventory['stage_snapshot_summary'][0]['snapshot_id'] == 'synthetic-close'
    assert {row['symbol']: row['record_count'] for row in inventory['daily_series_summary']['series']} == {'GLD': 316, 'SPY': 1}
    assert len(validate(advanced_fixture, sha)) == 6
