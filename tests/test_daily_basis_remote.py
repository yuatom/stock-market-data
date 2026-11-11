"""Synthetic local bare Git only; no provider, production remote or real token."""
import base64
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import traceback

import pytest

URL = 'https://github.com/yuatom/stock-market-data-store.git'
ZERO = '0' * 40
MAIN = 'refs/heads/main'
ATTEMPT = 'refs/heads/basis-daily-attempts/' + 'a' * 64 + '/nov11'
CANDIDATE = 'refs/heads/basis-daily-candidates/' + 'a' * 64 + '/nov11'
AUTHORITY = 'refs/heads/basis-daily-authority/' + 'b' * 40 + '/' + 'c' * 64
TOKEN = 'synthetic-token-not-a-credential'


def module():
    assert importlib.util.find_spec('scripts.daily_basis_remote') is not None, 'remote store adapter is not implemented'
    return importlib.import_module('scripts.daily_basis_remote')


def test_remote_adapter_is_available():
    assert callable(module().RemoteGitStore)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Synthetic',
        '-c', 'user.email=synthetic@example.invalid', *args], stderr=subprocess.PIPE).decode().strip()


class BareTransport:
    """Only the fixed production URL is substituted, only in this local fixture."""
    def __init__(self, repo, remote):
        self.repo, self.remote = repo, remote
        self.calls = []
        self.on_push = None
        self.after_fetch = None
        self.fail_after_push = False
        self.record = dict(id=11, private=True, full_name='yuatom/stock-market-data-store',
            default_branch='main', owner=dict(id=12, login='yuatom', type='User'),
            permissions=dict(pull=True, push=True, admin=False))

    def metadata(self):
        return copy.deepcopy(self.record)

    def git(self, *args):
        self.calls.append(args)
        assert args.count(URL) == 1
        if args[0] == 'push' and self.on_push:
            callback, self.on_push = self.on_push, None
            callback()
        actual = [str(self.remote) if x == URL else x for x in args]
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', GIT_TERMINAL_PROMPT='0')
        result = subprocess.run(['git', '-C', str(self.repo), '-c', 'protocol.file.allow=always',
            '-c', 'core.hooksPath=/dev/null', *actual], env=env, capture_output=True, timeout=30)
        if args[0] == 'fetch' and self.after_fetch:
            callback, self.after_fetch = self.after_fetch, None
            callback()
        if args[0] == 'push' and self.fail_after_push:
            assert result.returncode == 0
            raise TimeoutError(TOKEN)
        return result

    @property
    def pushes(self):
        return [call for call in self.calls if call[0] == 'push']


@pytest.fixture
def setup(tmp_path):
    R = module()
    seed, remote, repo = (tmp_path / name for name in ('seed', 'remote.git', 'checkout'))
    seed.mkdir(); git(seed, 'init', '-qb', 'main')
    (seed / 'preserve.json').write_bytes(b'{"original":"literal bytes"}\n')
    git(seed, 'add', '.'); git(seed, 'commit', '-qm', 'synthetic initial')
    git(tmp_path, 'init', '--bare', '-q', '--initial-branch=main', str(remote))
    git(seed, 'push', '-q', str(remote), 'main')
    git(tmp_path, 'clone', '-q', '--no-hardlinks', str(remote), str(repo))
    git(repo, 'remote', 'set-url', 'origin', URL)
    transport = BareTransport(repo, remote)
    state = {'source': True, 'platform': True}
    def source():
        if not state['source']: raise ValueError(TOKEN)
    def platform():
        if not state['platform']: raise ValueError(TOKEN)
    def create(**kwargs):
        return R.RemoteGitStore(repo, TOKEN, source_recheck=source, platform_guard=platform,
            expected_repository_id=11, expected_owner_id=12, transport=transport, **kwargs)
    store = create()
    return R, store, repo, remote, transport, state, create


def local_head(repo):
    return git(repo, 'rev-parse', MAIN)


def test_atomic_main_publication_preserves_bytes_and_retains_exact_commit(setup):
    R, store, repo, remote, tr, state, create = setup
    old = store.head()
    with store.lock():
        new = store.publish(old, {'new.json': b'{"new":1}\n'}, replace=set())
    assert local_head(repo) == local_head(remote) == new
    assert git(remote, 'rev-parse', 'refs/tags/market-data-read/' + new) == new
    assert git(remote, 'cat-file', '-t', 'refs/tags/market-data-read/' + new) == 'commit'
    assert git(remote, 'rev-parse', new + '^') == old
    assert git(remote, 'show', new + ':preserve.json') == '{"original":"literal bytes"}'
    assert len(tr.pushes) == 1 and '--atomic' in tr.pushes[0]
    assert git(remote, 'cat-file', 'commit', new) == git(repo, 'cat-file', 'commit', new)


def test_commit_only_creates_local_objects(setup):
    _, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'local.json': b'{}\n'}, replace=set())
    assert new != old and local_head(repo) == local_head(remote) == old
    assert tr.pushes == []


def test_same_commit_winner_cannot_acquire_claim_via_up_to_date_push(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'claim.json': b'{}\n'}, replace=set())
        def winner():
            git(repo, 'push', '--atomic', str(remote), new + ':' + MAIN,
                new + ':refs/tags/market-data-read/' + new)
        tr.on_push = winner
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == old and local_head(remote) == new
    assert len(tr.pushes) == 1


def test_competing_main_child_is_not_overwritten(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'claim.json': b'{}\n'}, replace=set())
        other = store.commit(old, {'other.json': b'{}\n'}, replace=set())
        tr.on_push = lambda: git(repo, 'push', str(remote), other + ':' + MAIN)
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == old and local_head(remote) == other
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/tags/market-data-read/' + new) == ''
    assert len(tr.pushes) == 1


def test_unknown_landed_push_stops_without_local_sync_or_retry(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'claim.json': b'{}\n'}, replace=set())
        tr.fail_after_push = True
        for _ in range(2):
            with pytest.raises(R.RemoteStoreError) as caught: store.cas(MAIN, new, old)
            assert TOKEN not in ''.join(traceback.format_exception(caught.value))
    assert len(tr.pushes) == 1 and local_head(repo) == old and local_head(remote) == new


def test_attempt_evidence_append_survives_unrelated_main_movement(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        store.cas(ATTEMPT, old, ZERO)
        other = store.commit(old, {'unrelated.json': b'{}\n'}, replace=set())
        git(repo, 'push', str(remote), other + ':' + MAIN)
        with pytest.raises(R.RemoteStoreError): store.head()
        evidence = store.commit(old, {'evidence.json': b'{"raw":"exact"}\n'}, replace=set())
        store.cas(ATTEMPT, evidence, old)
    assert git(remote, 'rev-parse', ATTEMPT) == evidence
    assert git(remote, 'rev-parse', 'refs/tags/market-data-read/' + evidence) == evidence
    assert local_head(repo) == old and local_head(remote) == other


@pytest.mark.parametrize('ref', [CANDIDATE,
    'refs/heads/basis-daily-verification/' + 'a' * 64 + '/nov11',
    'refs/heads/basis-daily-deliveries/' + 'a' * 64 + '/nov11',
    'refs/heads/basis-read-receipts/' + 'b' * 40 + '/' + 'c' * 64])
def test_immutable_scoped_ref_cannot_be_replaced(setup, ref):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        store.cas(ref, old, ZERO)
        new = store.commit(old, {'forbidden.json': b'{}\n'}, replace=set())
        with pytest.raises(R.RemoteStoreError): store.cas(ref, new, old)
    assert git(remote, 'rev-parse', ref) == old and len(tr.pushes) == 1


@pytest.mark.parametrize('ref', [MAIN, ATTEMPT])
def test_mutable_ref_update_requires_single_direct_parent(setup, ref):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        if ref == ATTEMPT: store.cas(ref, old, ZERO)
        child = store.commit(old, {'child.json': b'{}\n'}, replace=set())
        grandchild = store.commit(child, {'grandchild.json': b'{}\n'}, replace=set())
        before = len(tr.pushes)
        with pytest.raises(R.RemoteStoreError): store.cas(ref, grandchild, old)
    assert len(tr.pushes) == before and git(remote, 'rev-parse', ref) == old


def test_exact_ref_materialization_preserves_remote_and_local_main(setup):
    _, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'authority.json': b'{}\n'}, replace=set())
        git(repo, 'push', str(remote), new + ':' + AUTHORITY)
        assert store.materialize_ref(AUTHORITY) == new
        assert store.materialize_ref(AUTHORITY) == new
        assert store.ref(AUTHORITY) == new
    assert local_head(repo) == local_head(remote) == old and tr.pushes == []


def test_materialization_requires_stable_remote_and_never_updates_existing_ref(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'authority.json': b'{}\n'}, replace=set())
        git(repo, 'push', str(remote), old + ':' + AUTHORITY)
        tr.after_fetch = lambda: git(repo, 'push', str(remote), new + ':' + AUTHORITY)
        with pytest.raises(R.RemoteStoreError): store.materialize_ref(AUTHORITY)
        assert store.ref(AUTHORITY) is None
        git(repo, 'update-ref', AUTHORITY, old)
        with pytest.raises(R.RemoteStoreError): store.materialize_ref(AUTHORITY)
        assert store.ref(AUTHORITY) == old


@pytest.mark.parametrize('attack', ['symbolic', 'annotated', 'blob', 'symlink'])
def test_materialize_rejects_nonliteral_or_noncommit_refs(setup, attack):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    ref = 'refs/tags/market-data-read/' + old
    if attack == 'symbolic': git(remote, 'symbolic-ref', ref, MAIN)
    elif attack == 'annotated':
        git(repo, 'tag', '-am', 'annotated', 'market-data-read/' + old, old)
        git(repo, 'push', str(remote), ref)
    elif attack == 'blob':
        blob = git(repo, 'rev-parse', old + ':preserve.json')
        git(repo, 'push', str(remote), blob + ':' + ref)
    else:
        ref = AUTHORITY
        git(repo, 'push', str(remote), old + ':' + ref)
        target = repo / '.git' / 'refs' / 'heads' / 'basis-daily-authority'
        target.symlink_to(remote / 'refs' / 'heads', target_is_directory=True)
    with store.lock():
        with pytest.raises(R.RemoteStoreError): store.materialize_ref(ref)


@pytest.mark.parametrize('change', [
    {'private': False}, {'id': 13}, {'id': True}, {'full_name': 'other/store'},
    {'default_branch': 'other'}, {'owner': {'id': 13, 'login': 'yuatom'}},
    {'owner': {'id': 12, 'login': 'other'}}, {'permissions': {'push': False}},
    {'permissions': {'push': 1}},
])
def test_wrong_private_identity_or_write_permission_rejected(setup, change):
    R, store, repo, remote, tr, state, create = setup
    tr.record.update(change)
    with pytest.raises(R.RemoteStoreError): create()
    assert tr.pushes == []


@pytest.mark.parametrize('key,value', [
    ('url.https://evil.invalid/.insteadOf', 'https://github.com/'),
    ('remote.origin.pushurl', 'https://evil.invalid/store'),
    ('include.path', '/tmp/untrusted-config'), ('core.sshCommand', 'false'),
    ('credential.helper', '!echo stolen'), ('http.extraheader', 'Authorization: bad'),
    ('remote.origin.url', 'git@github.com:yuatom/stock-market-data-store.git'),
    ('remote.origin.url', URL + '?query=1'), ('core.worktree', '/tmp/elsewhere'),
])
def test_malicious_local_configuration_is_rejected_before_remote_io(setup, key, value):
    R, store, repo, remote, tr, state, create = setup
    git(repo, 'config', '--local', key, value)
    before = len(tr.calls)
    with pytest.raises(R.RemoteStoreError): create()
    assert len(tr.calls) == before


@pytest.mark.parametrize('state_key', ['source', 'platform'])
def test_lost_trusted_callback_blocks_local_commit_and_remote_push(setup, state_key):
    R, store, repo, remote, tr, state, create = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'local.json': b'{}\n'}, replace=set())
        state[state_key] = False
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
        with pytest.raises(R.RemoteStoreError): store.commit(old, {'later.json': b'{}\n'}, replace=set())
    assert local_head(repo) == local_head(remote) == old and tr.pushes == []


def test_existing_wrong_retention_tag_prevents_push(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'claim.json': b'{}\n'}, replace=set())
        git(repo, 'push', str(remote), old + ':refs/tags/market-data-read/' + new)
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert tr.pushes == [] and local_head(remote) == old


def test_existing_exact_lightweight_retention_can_be_reused(setup):
    _, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'claim.json': b'{}\n'}, replace=set())
        git(repo, 'push', str(remote), new + ':refs/tags/market-data-read/' + new)
        store.cas(MAIN, new, old)
    assert local_head(remote) == new and len(tr.pushes) == 1


def test_default_git_transport_keeps_token_only_in_minimal_child_environment(tmp_path, monkeypatch):
    R = module()
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    monkeypatch.setenv('HTTP_PROXY', 'http://evil.invalid')
    monkeypatch.setenv('GIT_TRACE', '1')
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-inherit')
    monkeypatch.setattr(R.subprocess, 'run', run)
    before = dict(os.environ)
    transport = R.GitHubGitTransport(tmp_path, TOKEN)
    transport.git('ls-remote', '--symref', URL, MAIN, MAIN + '^{}')
    assert os.environ == before
    argv, options = calls[0]
    assert TOKEN not in repr(argv) and 'http.followRedirects=false' in argv
    assert 'credential.helper=' in argv and 'protocol.allow=never' in argv
    assert 'protocol.https.allow=always' in argv and 'http.proxy=' in argv
    env = options['env']
    assert 'HTTP_PROXY' not in env and 'GIT_TRACE' not in env and 'UNRELATED_SECRET' not in env
    encoded = base64.b64encode(('x-access-token:' + TOKEN).encode()).decode()
    settings = dict((env['GIT_CONFIG_KEY_' + str(i)], env['GIT_CONFIG_VALUE_' + str(i)]) for i in range(int(env['GIT_CONFIG_COUNT'])))
    assert settings['http.' + URL + '.extraheader'] == 'AUTHORIZATION: basic ' + encoded
    assert 0 < options['timeout'] <= 120 and options['stdin'] == subprocess.DEVNULL
    assert not list(tmp_path.iterdir())


def test_default_git_transport_redacts_exception_chain(tmp_path, monkeypatch):
    R = module()
    def fail(*args, **kwargs): raise RuntimeError(TOKEN)
    monkeypatch.setattr(R.subprocess, 'run', fail)
    with pytest.raises(R.RemoteStoreError) as caught:
        R.GitHubGitTransport(tmp_path, TOKEN).git('ls-remote', '--symref', URL, MAIN, MAIN + '^{}')
    assert TOKEN not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('raw', [b'{"id":11,"id":12}', b'{"id":NaN}', b'[]', b'{}' * 100000, b'\xff'])
def test_default_metadata_rejects_unbounded_or_non_strict_json(tmp_path, monkeypatch, raw):
    R = module()
    class Reply:
        status = 200
        def geturl(self): return 'https://api.github.com/repos/yuatom/stock-market-data-store'
        def read(self, n): return raw[:n]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, req, timeout): return Reply()
    monkeypatch.setattr(R.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(R.RemoteStoreError): R.GitHubGitTransport(tmp_path, TOKEN).metadata()


def test_default_metadata_fixed_endpoint_no_proxy_or_redirect(tmp_path, monkeypatch):
    R = module()
    observed = {}
    class Reply:
        status = 200
        def geturl(self): return 'https://api.github.com/repos/yuatom/stock-market-data-store'
        def read(self, n):
            observed['bound'] = n
            return b'{"id":11}'
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, req, timeout):
            observed.update(url=req.full_url, headers=req.headers, timeout=timeout)
            return Reply()
    def opener(*handlers):
        observed['handlers'] = handlers
        return Opener()
    monkeypatch.setattr(R.urllib.request, 'build_opener', opener)
    result = R.GitHubGitTransport(tmp_path, TOKEN).metadata()
    assert result == {'id': 11}
    assert observed['url'] == 'https://api.github.com/repos/yuatom/stock-market-data-store'
    assert observed['headers']['Authorization'] == 'Bearer ' + TOKEN
    assert observed['handlers'][0].proxies == {}
    assert observed['handlers'][1].redirect_request(None, None, 302, '', {}, 'https://evil.invalid') is None
    assert 0 < observed['timeout'] <= 30 and observed['bound'] <= 1024 * 1024 + 1


def test_create_only_ref_ancestor_race_is_rejected_without_mutating_winner(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'candidate.json': b'{}\n'}, replace=set())
        tr.on_push = lambda: git(repo, 'push', str(remote), old + ':' + CANDIDATE)
        with pytest.raises(R.RemoteStoreError): store.cas(CANDIDATE, new, ZERO)
    assert git(remote, 'rev-parse', CANDIDATE) == old
    assert store.ref(CANDIDATE) is None and len(tr.pushes) == 1
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/tags/market-data-read/' + new) == ''


def test_main_rewind_between_precheck_and_advertisement_does_not_gain_a_claim(setup):
    R, store, repo, remote, tr, *_ = setup
    initial = store.head()
    with store.lock():
        old = store.publish(initial, {'first.json': b'{}\n'}, replace=set())
        new = store.commit(old, {'second.json': b'{}\n'}, replace=set())
        tr.on_push = lambda: git(remote, 'update-ref', MAIN, initial, old)
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == old and local_head(remote) == initial and len(tr.pushes) == 2


def test_atomic_unsupported_stops_without_fallback(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    git(remote, 'config', 'receive.advertiseAtomic', 'false')
    with store.lock():
        new = store.commit(old, {'candidate.json': b'{}\n'}, replace=set())
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == local_head(remote) == old and len(tr.pushes) == 1
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/tags/market-data-read') == ''


def test_retention_tag_race_does_not_publish_target(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with store.lock():
        new = store.commit(old, {'candidate.json': b'{}\n'}, replace=set())
        tag = 'refs/tags/market-data-read/' + new
        tr.on_push = lambda: git(repo, 'push', str(remote), old + ':' + tag)
        with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == local_head(remote) == old and git(remote, 'rev-parse', tag) == old
    assert len(tr.pushes) == 1


def test_retention_ref_name_must_identify_its_exact_commit(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    tag = 'refs/tags/market-data-read/' + 'f' * 40
    git(repo, 'push', str(remote), old + ':' + tag)
    with store.lock():
        with pytest.raises(R.RemoteStoreError): store.materialize_ref(tag)
    assert store.ref(tag) is None


@pytest.mark.parametrize('relative', ['shallow', 'info/grafts', 'objects/info/alternates',
    'objects/info/http-alternates', 'commondir'])
def test_incomplete_or_rewritten_local_graph_is_rejected(setup, relative):
    R, store, repo, remote, tr, state, create = setup
    target = repo / '.git' / relative
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_text('')
    before = len(tr.calls)
    with pytest.raises(R.RemoteStoreError): create()
    assert len(tr.calls) == before


def test_replacement_ref_is_rejected(setup):
    R, store, repo, remote, tr, state, create = setup
    old = store.head()
    git(repo, 'update-ref', 'refs/replace/' + 'f' * 40, old)
    with pytest.raises(R.RemoteStoreError): create()


def test_ambient_auth_is_not_inherited_by_local_object_writes(setup, monkeypatch):
    _, store, repo, remote, tr, state, create = setup
    monkeypatch.setenv('MARKET_DATA_STORE_TOKEN', TOKEN)
    monkeypatch.setenv('TWELVE_DATA_API_KEY', 'other-sensitive-value')
    new_store = create()
    assert TOKEN not in repr(new_store.g.env)
    assert 'TWELVE_DATA_API_KEY' not in new_store.g.env


def test_ambient_git_auth_override_rejected_without_mutating_environment(setup, monkeypatch):
    R, store, repo, remote, tr, state, create = setup
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'http.extraheader')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', TOKEN)
    before = dict(os.environ)
    with pytest.raises(R.RemoteStoreError): create()
    assert os.environ == before


def test_store_lock_is_required_before_remote_mutations(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    with pytest.raises(R.RemoteStoreError): store.materialize_ref(MAIN)
    with pytest.raises(R.RemoteStoreError): store.cas(ATTEMPT, old, ZERO)
    assert tr.pushes == []


def test_post_push_retention_failure_never_syncs_or_retries(setup):
    R, store, repo, remote, tr, *_ = setup
    old = store.head()
    actual = tr.git
    with store.lock():
        new = store.commit(old, {'candidate.json': b'{}\n'}, replace=set())
        def remove_retention(*args):
            result = actual(*args)
            if args[0] == 'push': git(remote, 'update-ref', '-d', 'refs/tags/market-data-read/' + new)
            return result
        tr.git = remove_retention
        for _ in range(2):
            with pytest.raises(R.RemoteStoreError): store.cas(MAIN, new, old)
    assert local_head(repo) == old and local_head(remote) == new and len(tr.pushes) == 1


def test_initial_local_validation_never_passes_ambient_secrets_to_git(setup, monkeypatch):
    R, store, repo, remote, tr, state, create = setup
    monkeypatch.setenv('MARKET_DATA_STORE_TOKEN', TOKEN)
    monkeypatch.setenv('TWELVE_DATA_API_KEY', 'provider-secret-must-not-inherit')
    original_run = subprocess.run
    environments = []
    def observe(*args, **kwargs):
        environments.append(kwargs.get('env', {}))
        return original_run(*args, **kwargs)
    monkeypatch.setattr(R.subprocess, 'run', observe)
    create()
    assert environments
    assert all(TOKEN not in repr(env) and 'TWELVE_DATA_API_KEY' not in env for env in environments)


def test_exact_ref_fetch_materializes_previously_missing_objects(setup):
    _, store, repo, remote, tr, *_ = setup
    old = store.head()
    other = repo.parent / 'other-checkout'
    git(repo.parent, 'clone', '-q', '--no-hardlinks', str(remote), str(other))
    (other / 'authority.json').write_bytes(b'{"newly_fetched":true}\n')
    git(other, 'add', '.'); git(other, 'commit', '-qm', 'remote-only authority')
    remote_commit = local_head(other)
    git(other, 'push', str(remote), remote_commit + ':' + AUTHORITY)
    before = subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', remote_commit], capture_output=True)
    assert before.returncode != 0
    with store.lock(): assert store.materialize_ref(AUTHORITY) == remote_commit
    assert store.read(remote_commit, 'authority.json') == b'{"newly_fetched":true}\n'
    assert local_head(repo) == local_head(remote) == old
    assert not (repo / '.git' / 'FETCH_HEAD').exists()
    assert not (repo / '.git' / 'shallow').exists()


def test_verifier_source_namespace_is_read_only(setup):
    R,store,repo,remote,tr,*_=setup
    head=local_head(remote);ref='refs/heads/basis-consumer-delivery/'+head
    git(remote,'update-ref',ref,head)
    with store.lock():
        assert store.remote_ref(ref)==head
        assert store.materialize_ref(ref)==head
        child=store.commit(head,{'forbidden-source-edit.json':b'{}\n'},replace=set())
        with pytest.raises(R.RemoteStoreError):store.cas(ref,child,head)
    assert git(remote,'rev-parse',ref)==head
    assert not tr.pushes
