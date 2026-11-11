"""The reviewed executing steps remain unreachable behind unconditional gates."""
from pathlib import Path
import pytest
import yaml
ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('filename,job_name,gate_index,route',[
    ('soxx-capture-only.yml','private_authority_preflight',1,'soxx_capture_only'),
    ('eleven-symbol-append.yml','append',0,'eleven_symbol_conditional_append')])
def test_dormant_execution_pipeline_stays_after_gate(filename,job_name,gate_index,route):
    doc=yaml.safe_load((ROOT/'.github/workflows'/filename).read_text());job=doc['jobs'][job_name]
    steps=job['steps'];gate=steps[gate_index]
    assert 'PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED' in gate['run']
    assert 'exit 2' in gate['run'] or 'SystemExit(2)' in gate['run']
    assert 'continue-on-error' not in gate
    following=steps[gate_index+1:]
    assert len(following)==4
    checkout,python,dependency,execution=following
    assert checkout['uses']=='actions/checkout@v4'
    assert checkout['with']=={'ref':'${{ github.sha }}','fetch-depth':0,'persist-credentials':False,'path':'compute'}
    assert python['uses']=='actions/setup-python@v5'
    assert python['with']['python-version']=='3.12'
    assert 'PyYAML==6.0.3' in dependency['run']
    assert execution['working-directory']=='compute'
    assert 'python -B -m scripts.run_private_authority_route' in execution['run']
    assert '--route '+route in execution['run']
    assert execution['env']['PYTHONDONTWRITEBYTECODE']=='1'
    assert execution['env']['MARKET_DATA_STORE_TOKEN']=='${{ secrets.MARKET_DATA_STORE_TOKEN }}'
    assert execution['env']['TWELVE_DATA_API_KEY']=='${{ secrets.TWELVE_DATA_API_KEY }}'
    assert job['timeout-minutes']==20
    for step in following:
        assert 'if' not in step and 'continue-on-error' not in step
    assert not any('secrets.' in str(step) for step in steps[:gate_index+1])
