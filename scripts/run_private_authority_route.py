"""Reviewed hosted binding behind exact manual owner/source checks.

Only an independently approved, reviewed GitHub job holding the repository-wide
collector concurrency lock may call this entrypoint. Environment comparisons
are guards, NOT platform attestation or proof of consent/lock ownership. Never
invoke locally against production. No private values are CLI arguments.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import argparse
import base64
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from scripts.private_authority_preflight import _validate_identity
from scripts.private_authority_promotion import prepare_promoted_authority

COMPUTE_URL = 'https://github.com/yuatom/stock-market-data'
STORE_URL = 'https://github.com/yuatom/stock-market-data-store.git'
STOP = 'PRIVATE_ROUTE_STOPPED_INSPECT_BEFORE_RETRY'
SAFETY = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
          '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
          '-c', 'http.proxy=', '-c', 'http.sslVerify=true',
          '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always')


class PrivateRouteError(RuntimeError):
    pass


def _need(value):
    if not value:
        raise ValueError()


@contextmanager
def _quiet():
    """Discard Python and inherited subprocess output, including on failure."""
    sys.stdout.flush(); sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    try:
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), 1); os.dup2(sink.fileno(), 2)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
    finally:
        for fd, prior in zip((1, 2), saved):
            os.dup2(prior, fd); os.close(prior)


@contextmanager
def _environment(token=None):
    """Single-threaded dedicated process only. No global Git config changes."""
    before = dict(os.environ)
    safe = {k: v for k, v in before.items()
            if k in {'PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT'} or k.startswith('GITHUB_')}
    safe.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null',
                GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1',
                GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0', LC_ALL='C',
                PYTHONDONTWRITEBYTECODE='1')
    if token is not None:
        settings = [(SAFETY[i+1].split('=',1)[0], SAFETY[i+1].split('=',1)[1])
                    for i in range(0,len(SAFETY),2)]
        settings.append(('http.'+STORE_URL+'.extraheader', 'AUTHORIZATION: basic '+
                         base64.b64encode(('x-access-token:'+token).encode()).decode()))
        safe['GIT_CONFIG_COUNT'] = str(len(settings))
        for i, (key, value) in enumerate(settings):
            safe['GIT_CONFIG_KEY_'+str(i)] = key
            safe['GIT_CONFIG_VALUE_'+str(i)] = value
    try:
        os.environ.clear(); os.environ.update(safe)
        yield
    finally:
        os.environ.clear(); os.environ.update(before)


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *SAFETY, *args],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
    _need(result.returncode == 0)
    return result.stdout.decode('utf-8').strip()


def _remote_main(root):
    fields = _git(root, 'ls-remote', '--refs', 'origin', 'refs/heads/main').split()
    _need(len(fields) == 2 and fields[1] == 'refs/heads/main')
    return fields[0]


def _source(compute, sha):
    _need(_git(compute, 'rev-parse', '--show-toplevel') == str(compute))
    # No local include, URL rewrite, push destination, filters or executable config.
    allowed = {'core.repositoryformatversion', 'core.filemode', 'core.bare',
               'core.logallrefupdates', 'core.ignorecase', 'core.precomposeunicode', 'gc.auto',
               'remote.origin.url', 'remote.origin.fetch', 'branch.main.remote',
               'branch.main.merge', 'user.name', 'user.email'}
    config = _git(compute, 'config', '--local', '--no-includes', '--null', '--list')
    for entry in config.split('\0'):
        if entry:
            name, _, value = entry.partition('\n')
            _need(name in allowed)
            if name == 'gc.auto': _need(value == '0')
    _need(_git(compute, 'remote', 'get-url', '--all', 'origin') in (COMPUTE_URL, COMPUTE_URL+'.git'))
    _need(_git(compute, 'rev-parse', 'HEAD') == sha)
    _need(not _git(compute, 'status', '--porcelain', '--untracked-files=all'))
    _need(not _git(compute, 'ls-files', '--others'))  # Includes ignored files.
    _need(all(line.startswith('H ') for line in _git(compute, 'ls-files', '-v').splitlines()))
    _need(not _git(compute, 'for-each-ref', '--format=%(refname)', 'refs/replace'))
    for name in ('info/grafts', 'objects/info/alternates', 'shallow'):
        path = Path(_git(compute, 'rev-parse', '--git-path', name))
        if not path.is_absolute(): path = compute/path
        _need(not os.path.lexists(path))
    _need(_remote_main(compute) == sha)


def _store_remote_refs(store, ref):
    return _git(store, 'ls-remote', '--refs', 'origin', 'refs/heads/main', ref)


def _store_binding(store, route, compute_sha, selection):
    _need(_git(store, 'remote', 'get-url', '--all', 'origin') == STORE_URL)
    _need(_git(store, 'remote', 'get-url', '--push', '--all', 'origin') == STORE_URL)
    _need(_git(store, 'rev-parse', 'HEAD') == selection.authority_store_sha)
    _need(not _git(store, 'status', '--porcelain') and not _git(store, 'ls-files', '--others'))
    ref = 'refs/heads/capture-authority/'+route+'/'+compute_sha
    expected = sorted([selection.authority_store_sha+'\trefs/heads/main',
                       selection.descriptor_commit+'\t'+ref])
    _need(sorted(_store_remote_refs(store, ref).splitlines()) == expected)


def _coordinator(route):
    # Fresh -B process with a reviewed clean checkout is the supported caller.
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path: sys.path.insert(0, scripts)
    return importlib.import_module('run_soxx_capture' if route == 'soxx_capture_only'
                                   else 'run_eleven_symbol_append')


def _invoke(*, route, compute, compute_sha, store, selection, request_id, key, token):
    runtime = _coordinator(route)
    if route == 'soxx_capture_only':
        identity = runtime.private_identity(token)
        helpers = runtime.admission.load_helpers()
        result = runtime.execute(compute=compute, compute_sha=compute_sha, store=store,
            approval_commit=selection.authority_store_sha, approval_sha256=selection.approval_sha256,
            request_id=request_id, h=helpers,
            config=helpers.collectors.load_yaml(compute/'config/market-data-store.yaml'),
            access=helpers.collectors.load_yaml(compute/'config/market-data-collector-access.yaml'),
            key=key, store_token=token, repository_identity=identity)
        _need(result['status'] == 'CAPTURE_RETAINED_UNQUALIFIED')
    else:
        result = runtime.run_official(repo=store, compute=compute,
            approval_raw=selection.approval_raw, approval_digest=selection.approval_sha256,
            request_id=request_id, key=key, store_token=token)
        _need(result['status'] == 'COMPLETE')


def run_route(*, route, compute_sha, expected_commitment, token, key, destination):
    """Private return-free composition except fixed success status; never retries.

No authorization or concurrency bypass flag is supported. Every invocation still requires the reviewed workflow and exact execution grant.
"""
    old_bytecode = sys.dont_write_bytecode
    old_path = list(sys.path)
    try:
        with _quiet(), _environment():
            sys.dont_write_bytecode = True
            _validate_identity(route, compute_sha, os.environ)
            if route == 'eleven_symbol_conditional_append':
                _need(os.environ.get('GITHUB_WORKFLOW') == 'Eleven Symbol Append')
            _need(type(expected_commitment) is str and re.fullmatch('[0-9a-f]{64}',expected_commitment))
            _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{8,4096}',token))
            _need(type(key) is str and 8 <= len(key) <= 4096 and all(33 <= ord(c) <= 126 for c in key))
            compute = Path(__file__).resolve().parents[1]
            _source(compute, compute_sha)
            selection = prepare_promoted_authority(token=token, route=route, compute_sha=compute_sha,
                expected_commitment=expected_commitment, destination=destination, env=os.environ,
                source_recheck=lambda: _source(compute, compute_sha))
            _source(compute, compute_sha)
            # Activate auth only AFTER the strict selector rejects ambient overrides.
            with _environment(token):
                _store_binding(Path(destination), route, compute_sha, selection)
                _invoke(route=route,compute=compute,compute_sha=compute_sha,store=Path(destination),
                    selection=selection,request_id=os.environ['GITHUB_RUN_ID']+'-1',key=key,token=token)
        return 'PRIVATE_ROUTE_COMPLETE'
    except Exception:
        pass
    finally:
        sys.dont_write_bytecode = old_bytecode
        sys.path[:] = old_path
    raise PrivateRouteError(STOP)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError()


def main(argv=None):
    """Only public route/source/opaque commitment may appear on this command line."""
    try:
        parser = _Parser(add_help=False)
        for name in ('route','compute-sha','receipt-commitment'):
            parser.add_argument('--'+name, required=True)
        args = parser.parse_args(argv)
        # Keep uncertain runs for private inspection; no deletion, reuse or repair.
        parent = Path(tempfile.mkdtemp(prefix='private-authority-'))
        status = run_route(route=args.route,compute_sha=args.compute_sha,
            expected_commitment=args.receipt_commitment, destination=parent/'store',
            token=os.environ.get('MARKET_DATA_STORE_TOKEN',''),key=os.environ.get('TWELVE_DATA_API_KEY',''))
        print(status)
        return 0
    except Exception:
        print(STOP)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
