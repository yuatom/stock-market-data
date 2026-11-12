from pathlib import Path
import os
from unittest.mock import patch
import pytest,yaml
ROOT=Path(__file__).parent
RAW=(ROOT/'twelve-symbol-append.yml.template').read_text()
DOC=yaml.safe_load(RAW)
JOB=DOC['jobs']['append']

def test_one_manual_job_holds_exact_shared_lock():
    assert set(DOC.get('on',DOC.get(True)))=={'workflow_dispatch'}
    assert set(DOC['jobs'])=={'append'}
    assert JOB['concurrency']=={'group':'market-data-collector','cancel-in-progress':False,'queue':'max'}
    assert DOC['permissions']=={'contents':'read'}
    assert not (ROOT/'.github/workflows').exists()
    for forbidden in ('upload-artifact','continue-on-error','--approval-json','workflow_run:','schedule:','push:'):
        assert forbidden not in RAW

def test_source_and_secrets_are_after_gate_and_only_wrapper_invoked():
    steps=JOB['steps']; assert len(steps)==5
    assert 'secrets.' not in str(steps[0])
    assert steps[1]['with']=={'ref':'${{ github.sha }}','fetch-depth':0,'persist-credentials':False,'path':'compute'}
    assert '--route twelve_symbol_conditional_append' in steps[-1]['run']
    assert 'scripts.run_private_authority_route' in steps[-1]['run']
    assert set(DOC.get('on',DOC.get(True))['workflow_dispatch']['inputs'])=={'expected_compute_sha','expected_receipt_commitment'}
    assert not any('secrets.' in str(s) for s in steps[:-1])
    assert all('if' not in s and 'continue-on-error' not in s for s in steps)

@pytest.mark.parametrize('mutation',[None,'EXPECTED_COMPUTE_SHA','EXPECTED_RECEIPT_COMMITMENT','GITHUB_SHA','GITHUB_EVENT_NAME','GITHUB_REF','GITHUB_REPOSITORY','GITHUB_REPOSITORY_OWNER','GITHUB_ACTOR','GITHUB_TRIGGERING_ACTOR','GITHUB_RUN_ATTEMPT'])
def test_inline_owner_first_attempt_gate_without_io(mutation,capsys):
    env=dict(EXPECTED_COMPUTE_SHA='c'*40,EXPECTED_RECEIPT_COMMITMENT='d'*64,GITHUB_SHA='c'*40,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY='yuatom/stock-market-data',GITHUB_REPOSITORY_OWNER='yuatom',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_RUN_ATTEMPT='1')
    if mutation:env[mutation]='unsupported'
    with patch.dict(os.environ,env,clear=True):
        if mutation:
            with pytest.raises(SystemExit):exec(JOB['steps'][0]['run'],{})
        else:exec(JOB['steps'][0]['run'],{})
    assert capsys.readouterr()==('','')
