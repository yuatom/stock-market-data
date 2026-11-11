"""Enabled execution stays behind exact identity checks, never a bypass."""
from pathlib import Path
import pytest
import yaml
ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('filename,job_name,route',[
    ('soxx-capture-only.yml','private_authority_preflight','soxx_capture_only'),
    ('eleven-symbol-append.yml','append','eleven_symbol_conditional_append')])
def test_execution_pipeline_stays_after_identity(filename,job_name,route):
    doc=yaml.safe_load((ROOT/'.github/workflows'/filename).read_text());job=doc['jobs'][job_name]
    steps=job['steps'];gate=steps[0]
    assert 'PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED' not in str(doc)
    assert 'EXPECTED_RECEIPT_COMMITMENT' in gate['run']
    assert 'EXPECTED_COMPUTE_SHA' in gate['run']
    assert 'SystemExit' in gate['run']
    assert 'continue-on-error' not in gate
    following=steps[1:]
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
    assert not any('secrets.' in str(step) for step in steps[:1])


@pytest.mark.parametrize('filename,job_name',[
    ('soxx-capture-only.yml','private_authority_preflight'),('eleven-symbol-append.yml','append')])
@pytest.mark.parametrize('mutation',[None,'GITHUB_REPOSITORY','GITHUB_REPOSITORY_OWNER',
    'GITHUB_ACTOR','GITHUB_TRIGGERING_ACTOR','GITHUB_EVENT_NAME','GITHUB_REF','GITHUB_RUN_ATTEMPT',
    'GITHUB_SHA','EXPECTED_RECEIPT_COMMITMENT'])
def test_first_step_rejects_unsupported_identity_before_private_access(filename,job_name,mutation,capsys):
    import os
    from unittest.mock import patch
    doc=yaml.safe_load((ROOT/'.github/workflows'/filename).read_text())
    step=doc['jobs'][job_name]['steps'][0]
    assert 'secrets.' not in str(step) and 'checkout' not in str(step)
    env=dict(EXPECTED_COMPUTE_SHA='c'*40,EXPECTED_RECEIPT_COMMITMENT='d'*64,GITHUB_SHA='c'*40,
        GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY='yuatom/stock-market-data',
        GITHUB_REPOSITORY_OWNER='yuatom',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',
        GITHUB_REF='refs/heads/main',GITHUB_RUN_ATTEMPT='1')
    if mutation:env[mutation]='unsupported'
    with patch.dict(os.environ,env,clear=True):
        if mutation:
            with pytest.raises(SystemExit):exec(step['run'],{})
        else:exec(step['run'],{})
    assert capsys.readouterr()==('','')
