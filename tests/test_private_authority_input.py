"""Fabricated repositories only. These receipts are not production permission."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zlib

import pytest

from scripts import private_authority_input as selection

COMPUTE = 'c' * 40
PATH = 'capture-authority/selection.json'


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False) + '\n').encode()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def add_commit(repo, files, message='synthetic object'):
    for path, raw in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def install_descriptor(fixture, *, descriptor=None, raw=None, extras=None, mode=None, parents=None):
    repo = fixture['repo']
    git(repo, 'reset', '--hard', fixture['head'])
    raw = encode(fixture['descriptor'] if descriptor is None else descriptor) if raw is None else raw
    path = repo / PATH
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    for name, content in (extras or {}).items():
        (repo / name).write_bytes(content)
    git(repo, 'add', '.')
    if mode:
        blob = git(repo, 'hash-object', '-w', str(path))
        git(repo, 'update-index', '--cacheinfo', mode + ',' + blob + ',' + PATH)
    tree = git(repo, 'write-tree')
    args = ['commit-tree', tree, '-m', 'synthetic private selection']
    for parent in parents if parents is not None else [fixture['head']]:
        args.extend(['-p', parent])
    descriptor_commit = git(repo, *args)
    git(repo, 'update-ref', fixture['ref'], descriptor_commit)
    git(repo, 'reset', '--hard', fixture['head'])
    fixture['commit'] = descriptor_commit
    return descriptor_commit


@pytest.fixture(params=['soxx_capture_only', 'eleven_symbol_conditional_append'])
def fixture(tmp_path, request):
    repo = tmp_path / 'store'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Synthetic')
    git(repo, 'config', 'user.email', 'synthetic@example.invalid')
    route = request.param
    baseline_files = {'synthetic.txt': b'Not market facts.\n'}
    targets = []
    if route == 'eleven_symbol_conditional_append':
        for symbol in ('IBIT', 'IWM', 'KWEB', 'QQQ', 'SMH', 'SPY', 'TLT', 'VIXY', 'XLE', 'XLU', 'XLK'):
            pins = {}
            for name in ('000000.json', '_index.json'):
                path = 'data/market-data/series/daily/twelve_data_basic/' + symbol + '/' + name
                raw = b'{}\n'
                baseline_files[path] = raw
                pins[name] = hashlib.sha1(b'blob 3\0' + raw).hexdigest()
            targets.append(dict(symbol=symbol, old_count=2, first_date='2024-01-02', last_date='2024-01-03',
                                addition_dates=['2024-01-04'], files=pins))
    baseline = add_commit(repo, baseline_files)
    common = dict(schema_version=1, mode=route, compute_sha=COMPUTE, execution_day='2026-11-11',
                  valid_from='2026-11-11T12:00:00+00:00', valid_until='2026-11-11T23:00:00+00:00',
                  evidence_repository='yuatom/stock-market-data-store', evidence_prefix='capture-evidence')
    if route == 'soxx_capture_only':
        approval = dict(common, baseline_store_sha=baseline, evidence_repository_id=12345,
                        request_limit=1, credit_limit=1, market_fact_write=False,
                        request=dict(provider='twelve_data', symbol='SOXX', interval='1day', exchange='NASDAQ',
                                     mic_code='XNMS', currency='USD', type='ETF', adjust='splits', dp=-1,
                                     start_date='2024-01-02', end_date='2024-01-04', outputsize=3, order='asc'))
        approval_raw = encode(approval)
        digest = hashlib.sha256(approval_raw).hexdigest()
        authority_path = f'capture-approvals/{digest}.json'
        head = add_commit(repo, {authority_path: approval_raw})
        nested = None
    else:
        contract = dict(schema_version=1, kind='eleven_symbol_append_contract', baseline_store_sha=baseline,
                        historical_cutoff='2024-01-04', targets=targets)
        raw = encode(contract)
        contract_digest = hashlib.sha256(raw).hexdigest()
        authority_path = f'capture-contracts/{contract_digest}.json'
        head = add_commit(repo, {authority_path: raw})
        approval = dict(common, expected_store_sha=head, private_contract_sha256=contract_digest,
                        registry_sha256='d' * 64, historical_cutoff='2024-01-04', request_limit=11,
                        credit_limit=11, basis_policy='full_ohlc_overlap_preserve_volume_v1')
        approval_raw = encode(approval)
        digest = hashlib.sha256(approval_raw).hexdigest()
        nested = approval
    result = dict(repo=repo, route=route, head=head, baseline=baseline, authority_path=authority_path,
                  approval_raw=approval_raw, digest=digest, ref=f'refs/capture-authority/{route}/{COMPUTE}',
                  descriptor=dict(schema_version=1, kind='capture_authority_selection', route=route,
                                  compute_sha=COMPUTE, authority_store_sha=head, approval_sha256=digest, approval=nested))
    install_descriptor(result)
    return result


def load(f, **changes):
    args = dict(store=f['repo'], route=f['route'], compute_sha=COMPUTE,
                expected_store_head=f['head'], descriptor_commit=f['commit'])
    args.update(changes)
    return selection.load_authority(**args)


def snapshot(repo):
    return {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}


def test_selection_is_private_read_only_and_does_not_grant_activation(fixture, capsys):
    before = snapshot(fixture['repo'])
    result = load(fixture)
    assert result.approval_raw == fixture['approval_raw']
    assert result.approval_sha256 == fixture['digest']
    assert result.authority_store_sha == fixture['head']
    assert result.descriptor_commit == fixture['commit']
    assert result.authorization_authenticated is False
    assert result.production_activation is False
    assert fixture['head'] not in repr(result) and fixture['digest'] not in repr(result)
    assert '2024-' not in repr(result)
    assert snapshot(fixture['repo']) == before
    assert capsys.readouterr() == ('', '')


def test_immutable_object_not_worktree_shadow(fixture):
    target = fixture['repo'] / PATH
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b'{"production_activation":true}\n')
    assert load(fixture).approval_sha256 == fixture['digest']


@pytest.mark.parametrize('field,value', [('route', '../main'), ('compute_sha', 'main'),
    ('expected_store_head', 'HEAD'), ('descriptor_commit', 'HEAD'), ('compute_sha', 'e' * 40),
    ('expected_store_head', 'e' * 40), ('descriptor_commit', 'e' * 40)])
def test_public_binding_and_private_receipt_must_match(fixture, field, value):
    with pytest.raises(selection.PrivateAuthorityError, match='^PRIVATE_AUTHORITY_INVALID$'):
        load(fixture, **{field: value})


@pytest.mark.parametrize('mutation', ['extra', 'version_bool', 'wrong_route', 'wrong_compute', 'wrong_head', 'wrong_digest', 'approval_shape'])
def test_closed_descriptor_binding(fixture, mutation):
    descriptor = dict(fixture['descriptor'])
    if mutation == 'extra': descriptor['authorization'] = True
    elif mutation == 'version_bool': descriptor['schema_version'] = True
    elif mutation == 'wrong_route': descriptor['route'] = 'unapproved'
    elif mutation == 'wrong_compute': descriptor['compute_sha'] = 'd' * 40
    elif mutation == 'wrong_head': descriptor['authority_store_sha'] = fixture['baseline']
    elif mutation == 'wrong_digest': descriptor['approval_sha256'] = '0' * 64
    else: descriptor['approval'] = {'not': 'approval'}
    install_descriptor(fixture, descriptor=descriptor)
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


@pytest.mark.parametrize('mutation', ['pretty', 'duplicate', 'nan', 'oversize', 'invalid_utf8'])
def test_canonical_bounded_json_only(fixture, mutation):
    raw = encode(fixture['descriptor'])
    if mutation == 'pretty': raw = json.dumps(fixture['descriptor'], indent=2).encode()
    elif mutation == 'duplicate': raw = b'{"schema_version":1,' + raw[1:]
    elif mutation == 'nan': raw = raw.replace(b'"schema_version":1', b'"schema_version":NaN')
    elif mutation == 'oversize': raw = b' ' * 65537
    else: raw = b'\xff\n'
    install_descriptor(fixture, raw=raw)
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


@pytest.mark.parametrize('mutation', ['extra_path', 'executable', 'symlink', 'root', 'wrong_parent', 'merge', 'moved_ref', 'symbolic_ref', 'missing_ref', 'main_on_descriptor'])
def test_exact_commit_delta_mode_parent_and_private_ref(fixture, mutation):
    f = fixture
    if mutation == 'extra_path': install_descriptor(f, extras={'unapproved.txt': b'no'})
    elif mutation == 'executable': install_descriptor(f, mode='100755')
    elif mutation == 'symlink': install_descriptor(f, mode='120000')
    elif mutation == 'root': install_descriptor(f, parents=[])
    elif mutation == 'wrong_parent': install_descriptor(f, parents=[f['baseline']])
    elif mutation == 'merge': install_descriptor(f, parents=[f['head'], f['baseline']])
    elif mutation == 'moved_ref': git(f['repo'], 'update-ref', f['ref'], f['head'])
    elif mutation == 'symbolic_ref': git(f['repo'], 'symbolic-ref', f['ref'], 'refs/heads/main')
    elif mutation == 'missing_ref': git(f['repo'], 'update-ref', '-d', f['ref'])
    else: git(f['repo'], 'reset', '--hard', f['commit'])
    with pytest.raises(selection.PrivateAuthorityError): load(f)


@pytest.mark.parametrize('name', ['GIT_REPLACE_REF_BASE', 'GIT_GRAFT_FILE', 'GIT_SHALLOW_FILE', 'GIT_DIR',
    'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG'])
def test_ambient_git_authority_overrides_rejected(fixture, monkeypatch, name):
    monkeypatch.setenv(name, '')
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


@pytest.mark.parametrize('path', ['info/grafts', 'shallow', 'objects/info/alternates', 'objects/info/http-alternates', 'commondir'])
def test_graph_and_external_object_files_rejected(fixture, path):
    target = fixture['repo'] / '.git' / path
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_bytes(b'')
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


def test_replacement_refs_rejected_even_when_not_used(fixture):
    git(fixture['repo'], 'replace', fixture['head'], fixture['baseline'])
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


def test_corrupt_commit_object_identity_rejected(fixture):
    commit = fixture['commit']
    path = fixture['repo'] / '.git/objects' / commit[:2] / commit[2:]
    original = zlib.decompress(path.read_bytes())
    changed = original.replace(b'Synthetic', b'Alteredxx', 1)
    assert changed != original and len(changed) == len(original)
    path.unlink()
    path.write_bytes(zlib.compress(changed))
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


def test_hooks_filters_diff_and_fsmonitor_never_run(fixture, tmp_path):
    marker = tmp_path / 'should-not-exist'
    script = tmp_path / 'malicious-config.sh'
    script.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
    script.chmod(0o755)
    repo = fixture['repo']
    git(repo, 'config', 'core.fsmonitor', str(script))
    git(repo, 'config', 'core.hooksPath', str(tmp_path))
    git(repo, 'config', 'diff.external', str(script))
    git(repo, 'config', 'filter.unsafe.smudge', str(script))
    (repo / '.gitattributes').write_text('* filter=unsafe diff=unsafe\n')
    git(repo, 'config', 'diff.unsafe.textconv', str(script))
    before = snapshot(repo)
    assert load(fixture).authority_store_sha == fixture['head']
    assert not marker.exists()
    assert snapshot(repo) == before


def test_partial_clone_never_fetches_missing_objects(fixture, tmp_path):
    marker = tmp_path / 'remote-called'
    helper = tmp_path / 'git-remote-unsafe'
    helper.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
    helper.chmod(0o755)
    repo = fixture['repo']
    git(repo, 'config', 'extensions.partialClone', 'origin')
    git(repo, 'config', 'remote.origin.promisor', 'true')
    git(repo, 'config', 'remote.origin.url', 'unsafe::local')
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)
    assert not marker.exists()


def test_corrupt_tree_identity_cannot_be_hidden_by_equivalent_git_mode(fixture):
    tree = git(fixture['repo'], 'rev-parse', fixture['commit'] + '^{tree}')
    path = fixture['repo'] / '.git/objects' / tree[:2] / tree[2:]
    raw_object = zlib.decompress(path.read_bytes())
    _, raw_tree = raw_object.split(b'\0', 1)
    changed = raw_tree.replace(b'40000 ', b'040000 ', 1)
    assert changed != raw_tree
    forged = b'tree ' + str(len(changed)).encode() + b'\0' + changed
    path.unlink()
    path.write_bytes(zlib.compress(forged))
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


def test_consistently_corrupted_tree_objects_are_not_authentic_commits(fixture):
    for commit in (fixture['baseline'], fixture['head'], fixture['commit']):
        tree = git(fixture['repo'], 'rev-parse', commit + '^{tree}')
        path = fixture['repo'] / '.git/objects' / tree[:2] / tree[2:]
        original = zlib.decompress(path.read_bytes())
        changed = original.replace(b'synthetic.txt', b'fabricated.tx')
        assert changed != original and len(changed) == len(original)
        path.unlink()
        path.write_bytes(zlib.compress(changed))
    with pytest.raises(selection.PrivateAuthorityError): load(fixture)


def test_eleven_baseline_shards_are_not_subject_to_authority_json_size_limit(fixture):
    if fixture['route'] != 'eleven_symbol_conditional_append':
        pytest.skip('Only the eleven route has private shard pins.')
    f = fixture
    repo = f['repo']
    git(repo, 'reset', '--hard', f['baseline'])
    relative = 'data/market-data/series/daily/twelve_data_basic/IBIT/000000.json'
    large = b'x' * 65537
    baseline = add_commit(repo, {relative: large}, 'synthetic oversized shard')
    # Contract can retain a large shard pin without loading market data.
    prior_contract = json.loads(git(repo, 'show', f['head'] + ':' + f['authority_path']))
    prior_contract['baseline_store_sha'] = baseline
    prior_contract['targets'][0]['files']['000000.json'] = hashlib.sha1(b'blob 65537\0' + large).hexdigest()
    raw = encode(prior_contract)
    digest = hashlib.sha256(raw).hexdigest()
    authority_path = f'capture-contracts/{digest}.json'
    head = add_commit(repo, {authority_path: raw})
    approval = dict(f['descriptor']['approval'], expected_store_sha=head, private_contract_sha256=digest)
    approval_raw = encode(approval)
    f.update(head=head, baseline=baseline, authority_path=authority_path, approval_raw=approval_raw,
             digest=hashlib.sha256(approval_raw).hexdigest())
    f['descriptor'] = dict(f['descriptor'], authority_store_sha=head, approval=approval, approval_sha256=f['digest'])
    install_descriptor(f)
    assert load(f).approval_raw == approval_raw
