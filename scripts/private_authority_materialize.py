"""Unbound quiet Store materialization; no provider or execution entrypoint.

Only a fresh private directory may be used. Failures leave it for inspection,
never reuse or repair it. Caller must independently authenticate the receipt.
"""
import base64
import os
from pathlib import Path
import re
import subprocess
from scripts.private_authority_input import ROUTES, STORE_REPOSITORY, load_authority
from scripts.private_authority_transport import resolve_private_reference

STORE_URL = 'https://github.com/' + STORE_REPOSITORY + '.git'


class PrivateMaterializeError(RuntimeError):
    pass


def _need(condition):
    if not condition:
        raise ValueError()


def materialize_store(token, route, compute_sha, expected_store_head, descriptor_commit, destination):
    """Read remote objects and validate exact immutable local authority.

No live call is authorized by this function's existence. Credential lives only
in child environment for authenticated reads, never argv or persisted config.
No redirect, submodule, hook, custom credential helper, alternate protocol or
automatic retry is permitted. Caller owns receipt authentication, source and
lock verification, private workspace disposal and final execution authorization.
"""
    try:
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,4096}', token))
        _need(type(route) is str and route in ROUTES)
        for value in (compute_sha, expected_store_head, descriptor_commit):
            _need(type(value) is str and re.fullmatch(r'[0-9a-f]{40}', value))
        target = Path(destination)
        _need(not os.path.lexists(target))
        parent = target.parent.resolve(strict=True)
        _need(parent.is_dir())
        target = parent / target.name
        _need(not os.path.lexists(target) and target.name not in ('', '.', '..'))
        ref = f'refs/capture-authority/{route}/{compute_sha}'
        resolve_private_reference(token, route, compute_sha, descriptor_commit)
        target.mkdir(mode=0o700)
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT'}}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0', LC_ALL='C')
        auth = dict(env, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                    GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode())
        safety = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                  '-c', 'credential.helper=', '-c', 'http.followRedirects=false', '-c', 'http.proxy=',
                  '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always']

        def git(*args, authenticated=False):
            result = subprocess.run(['git', '-C', str(target), *safety, *args],
                env=auth if authenticated else env, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
            _need(result.returncode == 0)
            return result.stdout

        git('init', '--quiet', '--template=', '--initial-branch=materialization')
        git('remote', 'add', 'origin', STORE_URL)
        git('fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin',
            'refs/heads/main:refs/heads/main', ref + ':' + ref, authenticated=True)
        _need(git('rev-parse', '--verify', 'refs/heads/main').strip() == expected_store_head.encode())
        _need(git('rev-parse', '--verify', ref).strip() == descriptor_commit.encode())
        git('checkout', '--quiet', '--detach', expected_store_head)
        selection = load_authority(target, route, compute_sha, expected_store_head, descriptor_commit)
        resolve_private_reference(token, route, compute_sha, descriptor_commit)
        remote = git('ls-remote', '--refs', 'origin', 'refs/heads/main', ref, authenticated=True)
        expected = sorted([f'{expected_store_head}\trefs/heads/main', f'{descriptor_commit}\t{ref}'])
        _need(sorted(remote.decode('ascii').splitlines()) == expected)
        return selection
    except Exception:
        pass
    raise PrivateMaterializeError('PRIVATE_MATERIALIZATION_INVALID')
