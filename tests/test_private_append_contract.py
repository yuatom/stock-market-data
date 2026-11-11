"""Private runtime scope must be immutable and approval-bound, never public pins."""
import json
from pathlib import Path
import pytest
from scripts import run_eleven_symbol_append as r


def test_public_source_has_no_real_store_pin_file():
    assert not (r.ROOT/'config/eleven-symbol-append-pins.json').exists()
    assert hasattr(r,'load_private_contract')


def test_private_contract_loader_is_required_by_official_route():
    source=(r.ROOT/'scripts/run_eleven_symbol_append.py').read_text()
    assert 'load_private_contract(repo,approval)' in source
    assert 'PINS.read_bytes' not in source


def test_deployment_template_always_exits_before_private_operations(monkeypatch,capsys):
    import yaml
    workflow=yaml.safe_load((r.ROOT/'.github/workflows/eleven-symbol-append.yml').read_text())
    steps=workflow['jobs']['append']['steps']
    assert len(steps)==1
    env={'EXPECTED_COMPUTE_SHA':'c'*40,'GITHUB_SHA':'c'*40,'GITHUB_EVENT_NAME':'workflow_dispatch',
         'GITHUB_REF':'refs/heads/main','GITHUB_ACTOR':'fixture-owner',
         'GITHUB_REPOSITORY_OWNER':'fixture-owner','GITHUB_RUN_ATTEMPT':'1'}
    for key,value in env.items():monkeypatch.setenv(key,value)
    with pytest.raises(SystemExit) as stopped:exec(steps[0]['run'],{})
    assert stopped.value.code==2
    assert capsys.readouterr().out.strip()=='PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED'
