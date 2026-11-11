"""Exact remote Git CAS for the separately authorized finite daily runtime.

This adapter grants no execution authority. The trusted caller supplies source
and shared-owner platform checks, plus the independently approved finite grant.
No provider operation, dispatch, secret provisioning or retry exists here.
Tests substitute a local bare repository; production activation is separate.

A narrowly scoped --force-with-lease is an expected-old CAS, NOT permission to
rewrite history. Main/attempt updates must be single-parent direct children;
all other writable references are create-only. One atomic push couples each
publication to its exact lightweight market-data-read retention tag.
"""
from functools import wraps
import base64
import os
from pathlib import Path
import re
import subprocess
import urllib.request

from scripts import provider_basis_snapshot as B
from scripts.daily_basis_store import LocalGitStore

STORE_REPOSITORY = 'yuatom/stock-market-data-store'
STORE_URL = 'https://github.com/' + STORE_REPOSITORY + '.git'
METADATA_URL = 'https://api.github.com/repos/' + STORE_REPOSITORY
STOP = 'DAILY_REMOTE_STORE_STOPPED_INSPECT_NO_RETRY'
ZERO = '0' * 40
MAX_WIRE = 1024 * 1024
SAFETY = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
    '-c', 'core.commitGraph=false', '-c', 'core.useReplaceRefs=false',
    '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
    '-c', 'http.proxy=', '-c', 'http.sslVerify=true',
    '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
    '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true',
    '-c', 'gc.auto=0')


class RemoteStoreError(RuntimeError):
    """Value-free diagnostic, including for uncertain writes."""


def _need(value):
    if not value:
        raise RemoteStoreError(STOP)


def _safe(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            raise RemoteStoreError(STOP) from None
    return wrapped


def _environment():
    """No ambient credentials, proxies, Git overrides, tracing or askpass."""
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR') if key in os.environ}
    env.update(HOME='/nonexistent', GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null',
        GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1',
        GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    return env


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubGitTransport:
    """Fixed HTTPS endpoint, bounded duration/results, no persistent auth.

    Only this object's child-process environment receives the Git credential.
    No caller-supplied host, remote alias, proxy or transport protocol is used.
    A complete checkout is required; fetches never shallow or filter history.
    """
    @_safe
    def __init__(self, repo, token):
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{8,4096}', token))
        self._repo, self._token = Path(repo), token

    @_safe
    def metadata(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(METADATA_URL, method='GET', headers={
            'Authorization': 'Bearer ' + self._token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'finite-daily-basis-store'})
        with opener.open(request, timeout=30) as response:
            _need(response.status == 200 and response.geturl() == METADATA_URL)
            raw = response.read(MAX_WIRE + 1)
        _need(type(raw) is bytes and 0 < len(raw) <= MAX_WIRE)
        result = B._parse(raw, canonical=False)
        _need(type(result) is dict)
        return result

    @_safe
    def git(self, *args):
        _need(args and args[0] in ('ls-remote', 'fetch', 'push') and args.count(STORE_URL) == 1)
        env = _environment()
        env['GIT_CONFIG_COUNT'] = '1'
        env['GIT_CONFIG_KEY_0'] = 'http.' + STORE_URL + '.extraheader'
        env['GIT_CONFIG_VALUE_0'] = 'AUTHORIZATION: basic ' + base64.b64encode(
            ('x-access-token:' + self._token).encode('ascii')).decode('ascii')
        result = subprocess.run(['git', '-C', str(self._repo), '--no-replace-objects',
            *SAFETY, *args], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=120)
        _need(type(result.stdout) is bytes and type(result.stderr) is bytes and
            len(result.stdout) <= MAX_WIRE and len(result.stderr) <= MAX_WIRE)
        return result


class _LiteralGit(B._LocalGit):
    def read(self, *args):
        # Dynamic dispatch also covers _LocalGit's constructor-time reads.
        # Never mutate os.environ or expose ambient provider/token variables.
        self.env = dict(_environment(), GIT_ALLOW_PROTOCOL='')
        return super().read(*args)


class RemoteGitStore(LocalGitStore):
    """Local object writer plus independently verified remote publication.

    transport is a dependency seam: metadata() returns a repository dictionary;
    git(*args) returns a CompletedProcess with byte output. It is not approval.
    Callbacks raise when trusted source/platform checks fail; None is success.
    """
    @_safe
    def __init__(self, repo, token, *, source_recheck, platform_guard,
                 expected_repository_id=1335101677, expected_owner_id=None, transport=None):
        _need(callable(source_recheck) and callable(platform_guard))
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{8,4096}', token))
        _need(type(expected_repository_id) is int and expected_repository_id > 0)
        _need(expected_owner_id is None or type(expected_owner_id) is int and expected_owner_id > 0)
        self._source_recheck, self._platform_guard = source_recheck, platform_guard
        self._repository_id, self._owner_id = expected_repository_id, expected_owner_id
        self._uncertain = False
        path = Path(repo).absolute()
        _need(not any(p.is_symlink() for p in (path, *path.parents)))
        # _LiteralGit retains _LocalGit's ambient-override rejection, while
        # isolating even its initial validation subprocesses from credentials.
        self.g = _LiteralGit(path)
        self.repo, self._lock = self.g.repo, None
        self._validate_local()
        self._transport = transport if transport is not None else GitHubGitTransport(self.repo, token)
        self._check()

    def _validate_local(self):
        self.g.assert_unmodified_graph()
        for relative in ('config', 'packed-refs', 'HEAD', 'objects/pack'):
            _need(not (self.g.gitdir / relative).is_symlink())
        allowed = {'core.repositoryformatversion', 'core.filemode', 'core.bare',
            'core.logallrefupdates', 'core.ignorecase', 'core.precomposeunicode',
            'gc.auto', 'remote.origin.url', 'remote.origin.fetch',
            'branch.main.remote', 'branch.main.merge', 'user.name', 'user.email'}
        seen = set()
        for record in self.g.read('config', '--local', '--no-includes', '--null', '--list').split(b'\0'):
            if not record:
                continue
            name, separator, value = record.partition(b'\n')
            name, value = name.decode('ascii').lower(), value.decode('utf-8')
            _need(separator and name in allowed and name not in seen)
            seen.add(name)
            if name == 'gc.auto': _need(value == '0')
            if name == 'core.bare': _need(value == 'false')
            if name == 'core.repositoryformatversion': _need(value == '0')
            if name == 'remote.origin.url': _need(value in (STORE_URL, STORE_URL[:-4]))
            if name == 'remote.origin.fetch': _need(value == '+refs/heads/*:refs/remotes/origin/*')
            if name == 'branch.main.remote': _need(value == 'origin')
            if name == 'branch.main.merge': _need(value == 'refs/heads/main')
        _need('remote.origin.url' in seen)

    def _check(self):
        self._validate_local()
        self._source_recheck()
        self._platform_guard()
        value = self._transport.metadata()
        _need(type(value) is dict and value.get('private') is True and
            type(value.get('id')) is int and value['id'] == self._repository_id and
            value.get('full_name') == STORE_REPOSITORY and value.get('default_branch') == 'main')
        owner, permissions = value.get('owner'), value.get('permissions')
        _need(type(owner) is dict and owner.get('login') == 'yuatom' and
            type(owner.get('id')) is int and owner['id'] > 0 and
            (self._owner_id is None or owner['id'] == self._owner_id))
        _need(type(permissions) is dict and permissions.get('push') is True)
        # Pin the numeric owner on the first authenticated metadata result.
        if self._owner_id is None:
            self._owner_id = owner['id']

    def _ref_scope(self, ref, *, write=False):
        _need(type(ref) is str)
        main = ref == 'refs/heads/main'
        attempt = re.fullmatch(r'refs/heads/basis-daily-attempts/[0-9a-f]{64}/[A-Za-z0-9_-]{1,64}', ref)
        immutable = re.fullmatch(r'refs/heads/basis-daily-(?:candidates|verification|deliveries)/[0-9a-f]{64}/[A-Za-z0-9_-]{1,64}', ref)
        receipt = re.fullmatch(r'refs/heads/basis-read-receipts/[0-9a-f]{40}/[0-9a-f]{64}', ref)
        authority = re.fullmatch(r'refs/heads/basis-daily-authority/[0-9a-f]{40}/[0-9a-f]{64}', ref)
        retention = re.fullmatch(r'refs/tags/market-data-read/[0-9a-f]{40}', ref)
        verifier = re.fullmatch(r'refs/heads/basis-consumer-delivery/[0-9a-f]{40}', ref)
        _need(main or attempt or immutable or receipt or not write and (authority or retention or verifier))
        path = self.g.gitdir / ref
        _need(not any(p.is_symlink() for p in (path, *path.parents)))
        return 'mutable' if main or attempt else 'immutable'

    @_safe
    def ref(self, ref):
        self._ref_scope(ref)
        return super().ref(ref)

    def _remote_ref(self, ref):
        self._ref_scope(ref)
        result = self._transport.git('ls-remote', '--symref', STORE_URL, ref, ref + '^{}')
        _need(result.returncode == 0 and type(result.stdout) is bytes and len(result.stdout) <= 4096)
        if result.stdout == b'':
            return None
        # Reject symrefs, multiple matches and annotated-tag peel records.
        match = re.fullmatch(rb'([0-9a-f]{40})\t' + re.escape(ref.encode()) + rb'\n', result.stdout)
        _need(match)
        identity = match.group(1).decode('ascii')
        if ref.startswith('refs/tags/market-data-read/'):
            _need(ref.rsplit('/', 1)[1] == identity)
        return identity

    @_safe
    def remote_ref(self, ref):
        self._check()
        return self._remote_ref(ref)

    @_safe
    def head(self):
        self._check()
        local = super().head()
        _need(local == self._remote_ref('refs/heads/main'))
        return local

    @_safe
    def materialize_ref(self, ref):
        self.assert_locked()
        self._check()
        before = self._remote_ref(ref)
        B._hex(before)
        local = self.ref(ref)
        _need(local is None or local == before)
        self._check()
        result = self._transport.git('fetch', '--no-tags', '--no-write-fetch-head',
            '--no-recurse-submodules', '--no-auto-maintenance', STORE_URL, ref)
        _need(result.returncode == 0)
        self._check()
        _need(self._remote_ref(ref) == before)
        self.g.commit(before)
        _need(self.ref(ref) == local)
        if local is None:
            self._write('update-ref', ref, before, ZERO)
        _need(self.ref(ref) == before)
        return before

    @_safe
    def commit(self, parent, updates, *, replace):
        self.assert_locked()
        _need(not self._uncertain)
        self._check()
        return super().commit(parent, updates, replace=replace)

    def _push_changed(self, result, ref, tag, *, creation, tag_existed):
        _need(result.returncode == 0 and type(result.stdout) is bytes and len(result.stdout) <= MAX_WIRE)
        records = {}
        for line in result.stdout.decode('ascii').splitlines():
            if '\t' not in line:
                # Git's header/footer include only destination text, never auth.
                continue
            fields = line.split('\t')
            _need(len(fields) == 3 and len(fields[0]) == 1)
            source, separator, target = fields[1].partition(':')
            _need(separator and target in (ref, tag) and target not in records)
            records[target] = fields[0]
        _need(set(records) == {ref, tag})
        _need(records[ref] == ('*' if creation else ' '))
        _need(records[tag] == ('=' if tag_existed else '*'))

    @_safe
    def cas(self, ref, new, expected):
        self.assert_locked()
        _need(not self._uncertain)
        B._hex(new); B._hex(expected)
        kind = self._ref_scope(ref, write=True)
        creation = expected == ZERO
        _need(ref != 'refs/heads/main' or not creation)
        _need(kind == 'mutable' or creation)
        self._check()
        old = None if creation else expected
        _need(self.ref(ref) == old and self._remote_ref(ref) == old)
        _, parents = self.g.commit(new)
        if not creation:
            _need(new != expected and parents == (expected,))
        tag = 'refs/tags/market-data-read/' + new
        tag_old = self._remote_ref(tag)
        _need(tag_old is None or tag_old == new)
        self._check()
        _need(self.ref(ref) == old and self._remote_ref(ref) == old)
        # Set before crossing the write boundary. Any failure, malformed receipt,
        # '=' lost claim, or uncertain readback blocks further writes in this run.
        self._uncertain = True
        result = self._transport.git('push', '--porcelain', '--atomic',
            '--force-with-lease=' + ref + ':' + expected,
            '--force-with-lease=' + tag + ':' + (tag_old or ZERO),
            STORE_URL, new + ':' + ref, new + ':' + tag)
        self._push_changed(result, ref, tag, creation=creation, tag_existed=tag_old is not None)
        self._check()
        _need(self._remote_ref(ref) == new and self._remote_ref(tag) == new)
        self.g.commit(new)
        _need(self.ref(ref) == old)
        self._write('update-ref', ref, new, expected)
        _need(self.ref(ref) == new)
        self._uncertain = False

    @_safe
    def publish(self, expected, updates, *, replace):
        return super().publish(expected, updates, replace=replace)
