"""Offline source-token read probe. No credential/network/provider access."""
import base64,json,urllib.error
from pathlib import Path
from unittest.mock import Mock
import pytest,yaml
from scripts import run_source_release_readiness as P

@pytest.fixture
def case():
    sha='a'*40;consumer='b'*40;raw=P.B.encode({'repository_commit_sha':consumer})
    env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_REPOSITORY=P.COMPUTE,GITHUB_REF='refs/heads/main',GITHUB_SHA=sha,GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_ACTOR_ID=str(P.OWNER),GITHUB_REPOSITORY_OWNER_ID=str(P.OWNER),GITHUB_JOB='source_readiness',GITHUB_WORKFLOW_REF=P.COMPUTE+'/'+P.WORKFLOW+'@refs/heads/main')
    reader=Mock();reader.request.return_value=dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=P.B.blob_sha(raw))
    return dict(reader=reader,compute_sha=sha,consumer_sha=consumer,env=env,source_check=Mock())

def test_single_fixed_get_success(case):
    assert P.run_probe(**case)=={'status':'SOURCE_RELEASE_READINESS_VERIFIED','provider_requests':0,'automatic_retry_allowed':False}
    case['reader'].request.assert_called_once_with('GET',P.REPORT_RELEASE_PATH)
    case['source_check'].assert_called_once()

@pytest.mark.parametrize('field,value',[('GITHUB_EVENT_NAME','schedule'),('GITHUB_RUN_ATTEMPT','2'),('GITHUB_ACTOR','other'),('GITHUB_REF','refs/heads/other'),('GITHUB_SHA','c'*40)])
def test_exact_manual_identity_no_request(case,field,value):
    case['env'][field]=value
    with pytest.raises(P.ReadinessError):P.run_probe(**case)
    case['reader'].request.assert_not_called()

@pytest.mark.parametrize('field,value',[('sha','0'*40),('path','other'),('encoding','utf8'),('type','dir'),('content','bad!')])
def test_release_contract_rejects(case,field,value):
    case['reader'].request.return_value[field]=value
    with pytest.raises(P.ReadinessError):P.run_probe(**case)
    case['reader'].request.assert_called_once()

def test_wrong_consumer_and_unknown_source_do_not_pass(case):
    case['consumer_sha']='c'*40
    with pytest.raises(P.ReadinessError):P.run_probe(**case)
    case['reader'].request.reset_mock();case['source_check'].side_effect=RuntimeError('private')
    with pytest.raises(P.ReadinessError):P.run_probe(**case)
    case['reader'].request.assert_not_called()

def test_http_error_safe_no_retry(case):
    case['reader'].request.side_effect=urllib.error.HTTPError('private-url',403,'private-token',{},None)
    with pytest.raises(P.ReadinessError) as err:P.run_probe(**case)
    result=P.failure_result(err.value)
    assert result['http_status']==403 and result['failure_stage']=='REPORT_READ'
    assert 'private' not in json.dumps(result)
    case['reader'].request.assert_called_once()

def test_workflow_has_only_source_secret_no_dispatch():
    root=Path(__file__).resolve().parents[1];text=(root/'.github/workflows/source-release-readiness.yml').read_text()
    w=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(w['on'])=={'workflow_dispatch'} and w['permissions']=={'contents':'read'}
    step=w['jobs']['source_readiness']['steps'][-1]
    assert step['env']['MARKET_DATA_SOURCE_TOKEN']=='${{ secrets.MARKET_DATA_SOURCE_TOKEN }}'
    for denied in ('TWELVE','MARKET_DATA_STORE_TOKEN','MARKET_DATA_BASIS_ACTIONS_TOKEN','--method POST','self-hosted'):
        assert denied not in text
    assert 'python -B' in step['run']

def test_real_cli_imports_and_safe_single_get(case):
    import os,subprocess,sys
    root=Path(__file__).resolve().parents[1]
    program=r'''
import json,sys,base64
from scripts import run_source_release_readiness as P
calls=[]
class Reader:
 def __init__(self,token):assert token=='synthetic-source'
 def request(self,method,path):
  assert method=='GET' and path==P.REPORT_RELEASE_PATH;calls.append(path)
  raw=P.B.encode({'repository_commit_sha':'b'*40})
  return dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=P.B.blob_sha(raw))
P.ReportReleaseReader=Reader
P._source=lambda *a:None
sys.argv=['offline','--expected-compute-sha','a'*40,'--expected-consumer-sha','b'*40]
code=P.main();assert calls==[P.REPORT_RELEASE_PATH];raise SystemExit(code)
'''
    env={'PATH':os.environ['PATH'],'PYTHONPATH':str(root),'MARKET_DATA_SOURCE_TOKEN':'synthetic-source',**case['env']}
    result=subprocess.run([sys.executable,'-B','-c',program],cwd=root,env=env,capture_output=True,text=True)
    assert result.returncode==0 and result.stderr==''
    assert json.loads(result.stdout)['status']=='SOURCE_RELEASE_READINESS_VERIFIED'
    assert 'synthetic' not in result.stdout and 'repository_commit_sha' not in result.stdout

def test_cli_missing_secret_stops_without_get(monkeypatch,capsys):
    import sys
    monkeypatch.delenv('MARKET_DATA_SOURCE_TOKEN',raising=False)
    monkeypatch.setattr(sys,'argv',['offline','--expected-compute-sha','a'*40,'--expected-consumer-sha','b'*40])
    with pytest.MonkeyPatch.context() as m:
        reader=Mock(side_effect=ValueError('private credential'))
        m.setattr(P,'ReportReleaseReader',reader)
        assert P.main()==2
    output=capsys.readouterr().out
    assert json.loads(output)['failure_stage']=='CREDENTIAL' and 'private' not in output
