"""Real local Git fixture, with network boundary replaced by local fetch only."""
import os
import subprocess
from unittest.mock import patch
import pytest
from tests.test_private_authority_input import fixture, COMPUTE, git


def module():
    from scripts import private_authority_materialize
    return private_authority_materialize


def run(f, destination, *, fail=False, drift=False, metadata_failure=None):
    m = module()
    real_run = subprocess.run
    seen = []
    def local_only(argv, **kwargs):
        seen.append((argv, kwargs))
        if 'fetch' in argv:
            assert 'synthetic-token' not in ' '.join(argv)
            assert kwargs['env']['GIT_CONFIG_VALUE_0'].startswith('AUTHORIZATION: basic ')
            if fail == 'timeout':
                raise subprocess.TimeoutExpired(argv, 120, output=b'PRIVATE-CREDENTIAL')
            if fail == 'exit':
                return subprocess.CompletedProcess(argv, 128, b'', b'PRIVATE-CREDENTIAL')
            if fail:
                raise RuntimeError('PRIVATE-CREDENTIAL')
            argv = list(argv)
            argv[argv.index('origin')] = str(f['repo'])
            argv.insert(1, '-c')
            argv.insert(2, 'protocol.file.allow=always')
        if 'ls-remote' in argv:
            argv = list(argv)
            argv[argv.index('origin')] = str(f['repo'])
            argv.insert(1, '-c')
            argv.insert(2, 'protocol.file.allow=always')
            if drift:
                return subprocess.CompletedProcess(argv, 0, b'changed', b'')
        return real_run(argv, **kwargs)
    with patch.object(m, 'resolve_private_reference') as resolve, patch.object(m.subprocess, 'run', side_effect=local_only):
        if metadata_failure is not None:
            resolve.side_effect = [RuntimeError('PRIVATE-METADATA')] if metadata_failure == 0 else [None, RuntimeError('PRIVATE-METADATA')]
        result = m.materialize_store('synthetic-token', f['route'], COMPUTE, f['head'], f['commit'], destination)
        assert resolve.call_count == 2
    return result, seen


def test_materialize_exact_private_objects_without_credentials_or_output(fixture, tmp_path, capsys):
    destination = tmp_path / 'new-store'
    result, seen = run(fixture, destination)
    assert result.approval_raw == fixture['approval_raw']
    assert result.production_activation is False
    assert git(destination, 'rev-parse', 'HEAD') == fixture['head']
    assert git(destination, 'config', '--get', 'remote.origin.url') == module().STORE_URL
    assert 'AUTHORIZATION' not in (destination / '.git/config').read_text()
    assert 'synthetic-token' not in (destination / '.git/config').read_text()
    assert all(x[1].get('capture_output') for x in seen)
    assert capsys.readouterr() == ('', '')


def test_existing_destination_never_reused(fixture, tmp_path):
    target = tmp_path / 'occupied';target.mkdir();(target/'keep').write_text('unchanged')
    with pytest.raises(module().PrivateMaterializeError):
        run(fixture, target)
    assert (target/'keep').read_text() == 'unchanged'


@pytest.mark.parametrize('failure', ['fail', 'drift'])
def test_failure_is_value_free_and_never_returns_authority(fixture, tmp_path, failure, capsys):
    with pytest.raises(module().PrivateMaterializeError) as caught:
        run(fixture, tmp_path/'new-store', **{failure: True})
    assert str(caught.value) == 'PRIVATE_MATERIALIZATION_INVALID'
    assert caught.value.__context__ is None
    assert capsys.readouterr() == ('', '')


def test_children_do_not_inherit_proxy_or_unrelated_secrets(fixture,tmp_path,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','https://proxy.invalid')
    monkeypatch.setenv('all_proxy','https://proxy.invalid')
    monkeypatch.setenv('UNRELATED_SECRET','private-unrelated')
    result,seen=run(fixture,tmp_path/'materialized')
    materializer_calls=[(argv,kwargs) for argv,kwargs in seen if 'protocol.https.allow=always' in argv]
    assert materializer_calls
    for argv,kwargs in materializer_calls:
        assert 'http.proxy=' in argv
        assert not any(k.lower().endswith('_proxy') for k in kwargs['env'])
        assert 'UNRELATED_SECRET' not in kwargs['env']
        assert ('GIT_CONFIG_VALUE_0' in kwargs['env']) == ('fetch' in argv or 'ls-remote' in argv)
    assert result.production_activation is False


@pytest.mark.parametrize('position',[0,1])
def test_metadata_failure_is_not_recovered(fixture,tmp_path,position,capsys):
    target=tmp_path/'materialized'
    with pytest.raises(module().PrivateMaterializeError) as caught:
        run(fixture,target,metadata_failure=position)
    assert caught.value.__context__ is None
    if position == 0:
        assert not target.exists()
    assert capsys.readouterr() == ('','')


@pytest.mark.parametrize('field',['head','commit'])
def test_wrong_fetched_pin_never_returns_selection(fixture,tmp_path,field):
    altered=dict(fixture);altered[field]='a'*40
    with pytest.raises(module().PrivateMaterializeError):
        run(altered,tmp_path/'materialized')


@pytest.mark.parametrize('failure',['timeout','exit'])
def test_git_failure_stays_quiet(fixture,tmp_path,failure,capsys):
    with pytest.raises(module().PrivateMaterializeError) as caught:
        run(fixture,tmp_path/'materialized',fail=failure)
    assert caught.value.__context__ is None
    assert str(caught.value) == 'PRIVATE_MATERIALIZATION_INVALID'
    assert capsys.readouterr() == ('','')


def test_fresh_directory_private_and_symlink_rejected(fixture,tmp_path):
    target=tmp_path/'materialized'
    before=os.umask(0)
    try:
        run(fixture,target)
    finally:
        os.umask(before)
    assert target.stat().st_mode & 0o777 == 0o700
    link=tmp_path/'link';link.symlink_to(target,target_is_directory=True)
    with pytest.raises(module().PrivateMaterializeError):
        run(fixture,link)
