"""Bounded close-only advancement; all Git operations use local synthetic fixtures."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_retained_daily_reconciliation import (
    fixture, git, write, encode, blob, prepare, validate, publish, local_guard,
    PREFIX, SERIES, INVENTORY, BUDGET, OLD_BUDGET, REPORT, MANIFEST, COMPUTE, TIMESTAMP, DATES,
)


def advance(root, token=1, stage='close'):
    receipt = PREFIX + f'collector-state/2026-11/2026-11-10-{stage}-{token:016x}-publication.json'
    proof = PREFIX + f'proofs/deterministic-metrics/2026-11/2026-11-10/close/{COMPUTE}/close-1713{token:02d}-et.json'
    latest = PREFIX + 'proofs/deterministic-metrics/2026-11/2026-11-10/close/latest.json'
    missing = PREFIX + f'collector-state/2026-11/2026-11-10-{stage}-missing.json'
    for path, value in {receipt: {'fixture': token}, proof: {'proof': token},
                        latest: {'proof_path': proof}, missing: {'missing': []},
                        INVENTORY: {'stale_inventory': token}}.items():
        write(root, path, value)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'synthetic close metadata')
    return git(root, 'rev-parse', 'HEAD'), receipt, proof


def publish_fixture_head(fixture):
    _, writer, _, _, pins = fixture
    current = git(writer, 'rev-parse', 'HEAD')
    git(writer, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    git(writer, 'checkout', '-q', '--detach', pins.expected_store_sha)
    return current


def test_resolve_two_close_commits_preserves_actual_base_and_provenance(fixture, monkeypatch):
    r, writer, source, remote, pins = fixture
    advance(writer)
    advance(writer, 2, 'close_retry')
    current = publish_fixture_head(fixture)
    before, _ = r._tree(writer, current)
    calls = []
    original = r.git_bytes
    def observe(root, *args):
        if args[0] == 'fetch': calls.append(args)
        return original(root, *args)
    monkeypatch.setattr(r, 'git_bytes', observe)
    effective = r.resolve_store_base(writer, pins)
    assert effective.expected_store_sha == current
    assert effective.approved_reviewed_store_sha == pins.expected_store_sha
    assert calls == [('fetch', '--no-tags', '--no-recurse-submodules', 'origin', current)]
    advanced = (r, writer, source, remote, effective)
    prepared = prepare(advanced)
    sha = r.commit_candidate(writer, prepared)
    assert git(writer, 'rev-parse', sha + '^') == current
    after, _ = r._tree(writer, sha)
    assert all(after[path] == raw for path, raw in before.items() if path not in prepared.changes)
    assert after[BUDGET] == before[BUDGET] and after[OLD_BUDGET] == before[OLD_BUDGET]
    report = json.loads(after[REPORT])
    assert report['approved_reviewed_store_sha'] == pins.expected_store_sha
    assert report['store_start_sha'] == current
    assert report['base_advance_policy'] == 'nov10_close_artifacts_only_v1'
    assert report['addition_dates'] == DATES and report['existing_records'] == 310
    assert report['provider_requests'] == report['additional_debits'] == report['refunds'] == 0
    lineage = json.loads(after[SERIES + '_index.json'])['lineage_segments'][-1]
    assert lineage['approved_reviewed_store_sha'] == pins.expected_store_sha
    assert lineage['store_start_sha'] == current
    assert len(validate(advanced, sha)) == 6
    assert publish(advanced, sha)['status'] == 'VERIFIED'


@pytest.mark.parametrize('path', [
    SERIES + '000256.json', SERIES + '_index.json', BUDGET, OLD_BUDGET,
    PREFIX + 'collector-state/new-budget.json', PREFIX + 'series/daily/twelve_data_basic/SPY/_index.json',
    PREFIX + 'archives/source.json', '.gitattributes', PREFIX + '.gitattributes', 'README.md',
    PREFIX + 'collector-state/2026-11/2026-11-11-close-1111111111111111-publication.json',
    PREFIX + 'collector-state/2026-11/2026-11-10-close-anything-publication.json',
    PREFIX + f'proofs/deterministic-metrics/2026-11/2026-11-10/close/{COMPUTE}/close-251301-et.json',
    PREFIX + f'proofs/deterministic-metrics/2026-11/2026-11-10/close_retry/{COMPUTE}/close-171301-et.json',
])
def test_unapproved_changed_paths_reject_before_checkout(fixture, path):
    r, writer, _, _, pins = fixture
    write(writer, path, {'forbidden': True})
    git(writer, 'add', '.')
    git(writer, 'commit', '-qm', 'forbidden drift')
    current = publish_fixture_head(fixture)
    with pytest.raises(r.ReconciliationError): r.resolve_store_base(writer, pins)
    assert git(writer, 'rev-parse', 'HEAD') == pins.expected_store_sha
    assert git(writer, 'status', '--porcelain') == ''
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == current


@pytest.mark.parametrize('mutation', ['deletion', 'mode', 'symlink', 'receipt_rewrite', 'proof_rewrite', 'change_revert'])
def test_forbidden_history_not_hidden_by_net_diff(fixture, mutation):
    r, writer, _, _, pins = fixture
    if mutation in {'receipt_rewrite', 'proof_rewrite'}:
        _, receipt, proof = advance(writer)
        write(writer, receipt if mutation == 'receipt_rewrite' else proof, {'rewritten': True})
    elif mutation == 'deletion': (writer / INVENTORY).unlink()
    elif mutation == 'mode': (writer / INVENTORY).chmod(0o755)
    elif mutation == 'symlink':
        target = writer / (PREFIX + 'collector-state/2026-11/2026-11-10-close-1111111111111111-publication.json')
        target.symlink_to('/tmp/forbidden')
    else:
        original = (writer / BUDGET).read_bytes()
        write(writer, BUDGET, {'total': 0})
        git(writer, 'add', '.'); git(writer, 'commit', '-qm', 'hidden accounting change')
        (writer / BUDGET).write_bytes(original)
    git(writer, 'add', '.'); git(writer, 'commit', '-qm', 'forbidden history')
    publish_fixture_head(fixture)
    with pytest.raises(r.ReconciliationError): r.resolve_store_base(writer, pins)
    assert git(writer, 'rev-parse', 'HEAD') == pins.expected_store_sha


@pytest.mark.parametrize('kind', ['unrelated', 'merge', 'shallow', 'graft'])
def test_noncanonical_history_rejected(fixture, kind):
    r, writer, _, _, pins = fixture
    current, _, _ = advance(writer)
    if kind in {'unrelated', 'merge'}:
        tree = git(writer, 'rev-parse', current + '^{tree}')
        if kind == 'unrelated': current = git(writer, 'commit-tree', tree, '-m', 'unrelated root')
        else:
            other = git(writer, 'commit-tree', tree, '-p', pins.expected_store_sha, '-m', 'other parent')
            current = git(writer, 'commit-tree', tree, '-p', current, '-p', other, '-m', 'merge')
    elif kind == 'shallow': (writer / '.git/shallow').write_text(current + '\n')
    else: (writer / '.git/info/grafts').write_text(current + '\n')
    effective = replace(pins, expected_store_sha=current, approved_reviewed_store_sha=pins.expected_store_sha)
    with pytest.raises(r.ReconciliationError): r.validate_store_base(writer, effective)


def test_resolve_unchanged_anchor_records_it_without_new_scope(fixture):
    r, writer, _, _, pins = fixture
    effective = r.resolve_store_base(writer, pins)
    assert effective.expected_store_sha == effective.approved_reviewed_store_sha == pins.expected_store_sha


@pytest.mark.parametrize('identity', ['refs/heads/main', '--upload-pack=forbidden', 'a' * 39, 'A' * 40])
def test_noncanonical_resolved_identity_never_fetched(fixture, monkeypatch, identity):
    r, writer, _, _, pins = fixture
    monkeypatch.setattr(r, '_remote_main', lambda root: identity)
    original = r.git_bytes
    calls = []
    def observe(root, *args):
        if args[0] == 'fetch': calls.append(args)
        return original(root, *args)
    monkeypatch.setattr(r, 'git_bytes', observe)
    with pytest.raises(r.ReconciliationError): r.resolve_store_base(writer, pins)
    assert calls == []


def test_unexpected_fetch_identity_rejects_without_checkout(fixture, monkeypatch):
    r, writer, _, _, pins = fixture
    advance(writer); publish_fixture_head(fixture)
    original = r.git_text
    def wrong_fetch(root, *args):
        return 'f' * 40 if args == ('rev-parse', 'FETCH_HEAD') else original(root, *args)
    monkeypatch.setattr(r, 'git_text', wrong_fetch)
    with pytest.raises(r.ReconciliationError): r.resolve_store_base(writer, pins)
    assert git(writer, 'rev-parse', 'HEAD') == pins.expected_store_sha


def test_main_moves_again_no_fact_push_or_resolve_retry(fixture, monkeypatch, tmp_path):
    r, writer, source, remote, pins = fixture
    advance(writer); publish_fixture_head(fixture)
    effective = r.resolve_store_base(writer, pins)
    advanced = (r, writer, source, remote, effective)
    sha = r.commit_candidate(writer, prepare(advanced))
    competitor = tmp_path / 'competitor'
    git(tmp_path, 'clone', '-q', str(remote), str(competitor))
    git(competitor, 'checkout', '-q', 'main')
    git(competitor, 'config', 'user.name', 'Fixture'); git(competitor, 'config', 'user.email', 'fixture@example.invalid')
    moved, _, _ = advance(competitor, 2, 'close_retry')
    git(competitor, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    original = r.git_bytes
    calls = []
    def observe(root, *args):
        if args[0] in {'push', 'fetch'}: calls.append(args)
        return original(root, *args)
    monkeypatch.setattr(r, 'git_bytes', observe)
    with pytest.raises(r.ReconciliationError, match='RETAINED_STORE_MAIN_CHANGED'): publish(advanced, sha)
    assert calls == []
    assert git(writer, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == moved


def test_staged_reviewed_anchor_cannot_override_pins(fixture):
    r, writer, source, remote, pins = fixture
    advance(writer); publish_fixture_head(fixture)
    effective = r.resolve_store_base(writer, pins)
    advanced = (r, writer, source, remote, effective)
    prepared = prepare(advanced)
    report = json.loads(prepared.changes[REPORT])
    report['approved_reviewed_store_sha'] = effective.expected_store_sha
    prepared.changes[REPORT] = encode(report)
    manifest = json.loads(prepared.changes[MANIFEST])
    for output in manifest['required_outputs']:
        if PREFIX + output['path'] == REPORT: output['blob_sha'] = blob(prepared.changes[REPORT])
    prepared.changes[MANIFEST] = encode(manifest)
    sha = r.commit_candidate(writer, prepared)
    with pytest.raises(r.ReconciliationError, match='RETAINED_CANDIDATE_CONTENT_CHANGED'): validate(advanced, sha)


def test_explicit_policy_is_required_by_request_and_authority():
    import yaml
    from scripts import reconcile_retained_daily as r
    assert r.APPROVED_REQUEST['base_advance_policy'] == 'nov10_close_artifacts_only_v1'
    missing = dict(r.APPROVED_REQUEST)
    missing.pop('base_advance_policy')
    with pytest.raises(r.ReconciliationError): r.validate_request(json.dumps(missing))
    policy = yaml.safe_load((Path(r.__file__).resolve().parents[1] / 'config/market-data-store.yaml').read_text())['series']['baseline_initialization']['retained_candidate_reconciliation']
    assert policy['base_advance_policy'] == r.BASE_ADVANCE_POLICY
    assert policy['all_intervening_commits_independently_validated'] is True


@pytest.mark.parametrize('bound', ['commits', 'paths'])
def test_base_traversal_limits_fail_closed(fixture, monkeypatch, bound):
    r, writer, _, _, pins = fixture
    advance(writer)
    if bound == 'commits':
        advance(writer, 2, 'close_retry')
        monkeypatch.setattr(r, 'MAX_BASE_COMMITS', 1)
    else: monkeypatch.setattr(r, 'MAX_BASE_PATHS_PER_COMMIT', 2)
    publish_fixture_head(fixture)
    with pytest.raises(r.ReconciliationError, match='RETAINED_BASE_TRAVERSAL_LIMIT'):
        r.resolve_store_base(writer, pins)
    assert git(writer, 'rev-parse', 'HEAD') == pins.expected_store_sha


def test_replacement_objects_cannot_hide_forbidden_history(fixture, monkeypatch):
    r, writer, _, _, pins = fixture
    write(writer, BUDGET, {'forbidden': True})
    git(writer, 'add', '.'); git(writer, 'commit', '-qm', 'forbidden history')
    forbidden = git(writer, 'rev-parse', 'HEAD')
    git(writer, 'checkout', '-q', '--detach', pins.expected_store_sha)
    allowed, _, _ = advance(writer)
    git(writer, 'replace', forbidden, allowed)
    import yaml
    workflow = yaml.safe_load((Path(r.__file__).resolve().parents[1] / '.github/workflows/reconcile-retained-daily.yml').read_text())
    assert workflow['jobs']['reconcile']['steps'][-1]['env']['GIT_NO_REPLACE_OBJECTS'] == '1'
    monkeypatch.setenv('GIT_NO_REPLACE_OBJECTS', '1')
    assert BUDGET in r.verifier._git(writer, 'diff', '--name-only', pins.expected_store_sha, forbidden).stdout
    effective = replace(pins, expected_store_sha=forbidden, approved_reviewed_store_sha=pins.expected_store_sha)
    with pytest.raises(r.ReconciliationError, match='RETAINED_BASE_ADVANCE_SCOPE_INVALID'):
        r.validate_store_base(writer, effective)


def test_environment_graft_override_cannot_hide_intermediate_changes(fixture, monkeypatch, tmp_path):
    r, writer, _, _, pins = fixture
    original = (writer / BUDGET).read_bytes()
    write(writer, BUDGET, {'forbidden': True})
    git(writer, 'add', '.'); git(writer, 'commit', '-qm', 'hidden accounting change')
    (writer / BUDGET).write_bytes(original)
    current, _, _ = advance(writer)
    external = tmp_path / 'external-history-override'
    external.write_text(current + ' ' + pins.expected_store_sha + '\n')
    monkeypatch.setenv('GIT_GRAFT_FILE', str(external))
    effective = replace(pins, expected_store_sha=current, approved_reviewed_store_sha=pins.expected_store_sha)
    with pytest.raises(r.ReconciliationError, match='RETAINED_BASE_HISTORY_INVALID'):
        r.validate_store_base(writer, effective)
