"""Value-free diagnostics only; no provider or external Git operation is permitted."""
import json

import pytest

from scripts import reconcile_retained_daily as r


def invoke(capsys, *extra):
    assert r.main(['--request-json', json.dumps(r.APPROVED_REQUEST), *extra]) == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Traceback' not in output.err and 'PRIVATE_VALUE' not in output.err
    return json.loads(output.err)


def test_invalid_arguments_have_fixed_reason_and_stage(capsys):
    result = invoke(capsys, '--unexpected', 'PRIVATE_VALUE')
    assert result == {'status': 'REJECTED', 'reason': 'RETAINED_ARGUMENTS_INVALID',
                      'stage': 'ARGUMENT_VALIDATION'}


def test_invalid_request_has_fixed_reason_and_stage(capsys):
    assert r.main(['--request-json', '{"private":"PRIVATE_VALUE"}']) == 1
    output = capsys.readouterr()
    assert 'PRIVATE_VALUE' not in output.out + output.err
    assert json.loads(output.err) == {'status': 'REJECTED', 'reason': 'RETAINED_REQUEST_INVALID',
                                     'stage': 'REQUEST_VALIDATION'}


@pytest.mark.parametrize('error,expected', [
    (r.ReconciliationError('RETAINED_CHECKOUT_DIRTY'), 'RETAINED_CHECKOUT_DIRTY'),
    (r.ReconciliationError('RETAINED_PRIVATE_VALUE'), 'RETAINED_RECONCILIATION_REJECTED'),
    (RuntimeError('RETAINED_CHECKOUT_DIRTY'), 'RETAINED_RECONCILIATION_REJECTED'),
    (RuntimeError('https://user:PRIVATE_VALUE@example.invalid'), 'RETAINED_RECONCILIATION_REJECTED'),
])
def test_only_allowlisted_reconciliation_errors_cross_cli(error, expected, monkeypatch, capsys):
    def fail(*args):
        raise error
    monkeypatch.setattr(r, 'execute', fail)
    result = invoke(capsys)
    assert result == {'status': 'REJECTED', 'reason': expected, 'stage': 'EXECUTION'}


@pytest.mark.parametrize('failed,expected_stage', [
    ('compute', 'COMPUTE_GUARD'), ('store_origin', 'STORE_ORIGIN_GUARD'),
    ('source_origin', 'SOURCE_ORIGIN_GUARD'), ('store_main', 'STORE_MAIN_GUARD'),
    ('prepare', 'CANDIDATE_PREPARATION'), ('commit', 'CANDIDATE_COMMIT'),
    ('publish', 'PUBLICATION'),
])
def test_execution_reports_exact_failed_stage_without_continuing(failed, expected_stage, monkeypatch, capsys, tmp_path):
    calls = []
    def step(name, result=None):
        calls.append(name)
        if name == failed:
            raise r.ReconciliationError('RETAINED_GIT_ROUTING_INVALID')
        return result
    monkeypatch.setattr(r, 'require_execution_surface', lambda *a: step('compute'))
    monkeypatch.setattr(r, '_require_origin', lambda root, repository: step(
        'store_origin' if root.name == 'store' else 'source_origin'))
    monkeypatch.setattr(r, 'resolve_store_base', lambda *a: step('store_main', r.PinnedInputs.from_request(r.APPROVED_REQUEST)))
    monkeypatch.setattr(r, 'prepare_candidate', lambda *a, **kw: step('prepare', object()))
    monkeypatch.setattr(r, 'commit_candidate', lambda *a, **kw: step('commit', 'c' * 40))
    monkeypatch.setattr(r, 'publish_candidate', lambda *a, **kw: step('publish'))
    result = invoke(capsys, '--compute-sha', 'c' * 40, '--request-id', '987-1',
                    '--store-repo', str(tmp_path / 'store'), '--candidate-repo', str(tmp_path / 'source'))
    assert result == {'status': 'REJECTED', 'reason': 'RETAINED_GIT_ROUTING_INVALID',
                      'stage': expected_stage}
    expected = ['compute', 'store_origin', 'source_origin', 'store_main', 'prepare', 'commit', 'publish']
    assert calls == expected[:expected.index(failed) + 1]


def test_unapproved_stage_is_not_echoed(monkeypatch, capsys):
    error = r.ReconciliationError('RETAINED_GIT_FAILED')
    error.stage = 'PRIVATE_VALUE'
    def fail(*args):
        raise error
    monkeypatch.setattr(r, 'execute', fail)
    assert invoke(capsys)['stage'] == 'EXECUTION'


def test_hosted_workflow_isolates_inherited_git_configuration():
    import yaml
    workflow = yaml.safe_load((r.Path(__file__).resolve().parents[1] / '.github/workflows/reconcile-retained-daily.yml').read_text())
    env = workflow['jobs']['reconcile']['steps'][-1]['env']
    assert env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert env['GIT_CONFIG_GLOBAL'] == '/dev/null'


@pytest.mark.parametrize('scope', ['SYSTEM', 'GLOBAL'])
@pytest.mark.parametrize('setting', ['filter.lfs.process', 'core.hooksPath', 'url.file:///tmp/forbidden/.insteadOf'])
def test_workflow_git_isolation_disables_inherited_executable_configuration(scope, setting, monkeypatch, tmp_path):
    import subprocess
    import yaml
    repo = tmp_path / 'checkout'
    repo.mkdir()
    config = tmp_path / 'inherited.gitconfig'
    config.write_text('')
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.delenv('GIT_CONFIG_NOSYSTEM', raising=False)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin',
                    'https://github.com/yuatom/stock-market-data'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'http.https://github.com/.extraheader',
                    'AUTHORIZATION: synthetic-fixture-only'], check=True)
    value = 'https://github.com/' if setting.startswith('url.') else 'PRIVATE_VALUE'
    subprocess.run(['git', 'config', '--file', str(config), setting, value], check=True)
    monkeypatch.setenv('GIT_CONFIG_' + scope, str(config))
    with pytest.raises(r.ReconciliationError):
        r._require_origin(repo, r.COMPUTE_REPOSITORY)
    workflow = yaml.safe_load((r.Path(__file__).resolve().parents[1] / '.github/workflows/reconcile-retained-daily.yml').read_text())
    env = workflow['jobs']['reconcile']['steps'][-1]['env']
    for key in ['GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL']:
        monkeypatch.setenv(key, env[key])
    r._require_origin(repo, r.COMPUTE_REPOSITORY)
    assert r.git_text(repo, 'config', '--get', 'http.https://github.com/.extraheader') == 'AUTHORIZATION: synthetic-fixture-only'
    # Isolation applies equally to the unchanged terminal verifier subprocess.
    config_text = r.verifier._git(repo, 'config', '--list').stdout
    assert 'PRIVATE_VALUE' not in config_text and setting.lower() + '=' not in config_text.lower()
    # Identical repo-local executable configuration is still forbidden.
    subprocess.run(['git', '-C', str(repo), 'config', setting, value], check=True)
    with pytest.raises(r.ReconciliationError):
        r._require_origin(repo, r.COMPUTE_REPOSITORY)
