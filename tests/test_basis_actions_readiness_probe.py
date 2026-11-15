"""Pure offline fixtures. No live permission or delivery claims."""
import base64,json,socket
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import Mock
import pytest,yaml
from scripts import run_basis_actions_readiness_probe as P

@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(socket,'socket',Mock(side_effect=AssertionError('network forbidden')))
    now=datetime(2026,11,15,12,tzinfo=timezone.utc);sha='a'*40;commitment='b'*64
    selector=dict(schema_version=1,provider='twelve_data_basic',symbol='SOXX',basis_id=P.BASIS,read_sha='c'*40,snapshot_blob_sha='d'*40,admission_blob_sha='e'*40)
    evidence=dict(selector=selector,receipt_ref=f'refs/heads/basis-read-receipts/{sha}/{commitment}',receipt_commit='f'*40,receipt_blob_sha='1'*40,valid_until=(now+timedelta(minutes=10)).isoformat())
    monkeypatch.setattr(P,'receipt_view',lambda *args:evidence)
    env=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_REPOSITORY=P.COMPUTE,GITHUB_REF='refs/heads/main',GITHUB_SHA=sha,GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='probe',GITHUB_WORKFLOW_REF=P.COMPUTE+'/'+P.WORKFLOW+'@refs/heads/main',GITHUB_RUN_ID='123')
    repo=dict(id=P.STORE_ID,private=True,full_name=P.STORE,owner={'id':P.OWNER})
    run=dict(id=456,workflow_id=P.WORKFLOW_ID,head_sha=P.SOURCE,path=P.PRIVATE_WORKFLOW,event='workflow_dispatch',run_attempt=1,head_branch='basis-consumer-delivery/'+P.SOURCE,repository={'id':P.STORE_ID},head_repository={'id':P.STORE_ID},actor={'id':P.OWNER},triggering_actor={'id':P.OWNER},status='completed',conclusion='success',run_started_at=now.isoformat())
    caller=dict(run,id=123,head_sha=sha,path=P.WORKFLOW,head_branch='main',status='in_progress',repository={'id':2},head_repository={'id':2})
    job=dict(id=789,run_id=456,name='verify-basis-consumer',head_sha=P.SOURCE,status='completed',conclusion='success',runner_id=25,runner_name='synthetic-runner',labels=sorted(P.LABELS),completed_at=now.isoformat(),steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])
    context=dict(repository=P.STORE,repository_id=P.STORE_ID,owner_id=P.OWNER,private=True,workflow_source_sha=P.SOURCE,run_id=456,run_attempt=1,event='workflow_dispatch',actor_id=P.OWNER,actor='yuatom',triggering_actor='yuatom',workflow_path=P.PRIVATE_WORKFLOW,job='verify-basis-consumer')
    delivery=dict(schema_version=1,kind='private_basis_consumer_delivery',compute_sha=sha,receipt_commitment=commitment,context=context,verified_at=now.isoformat(),**evidence)
    log={'job_id':789,'text':'BASIS_PRIVATE_DELIVERY_V1 '+base64.b64encode(P.B.encode(delivery)).decode()}
    replies={
        '/repos/'+P.COMPUTE:dict(id=2,full_name=P.COMPUTE,owner={'id':P.OWNER}),
        '/repos/'+P.COMPUTE+'/actions/runs/123':caller,'/user':{'id':P.OWNER},'/repos/'+P.STORE:repo,
        f'/repos/{P.STORE}/actions/workflows/{P.WORKFLOW_ID}':dict(id=P.WORKFLOW_ID,path=P.PRIVATE_WORKFLOW,state='active'),
        f'/repos/{P.STORE}/git/ref/heads/basis-consumer-delivery/{P.SOURCE}':dict(ref='refs/heads/basis-consumer-delivery/'+P.SOURCE,object={'type':'commit','sha':P.SOURCE}),
        f'/repos/{P.STORE}/actions/runners/25':dict(id=25,name='synthetic-runner',status='online',busy=False,labels=[{'name':n} for n in P.LABELS]),
        f'/repos/{P.STORE}/actions/runs/456':run,
        f'/repos/{P.STORE}/actions/runs/456/jobs?per_page=2':dict(total_count=1,jobs=[job])}
    calls=[]
    def request(method,path,payload=None):
        calls.append((method,path,payload))
        if method=='POST':return dict(workflow_run_id=456,run_url=f'https://api.github.com/repos/{P.STORE}/actions/runs/456',html_url=f'https://github.com/{P.STORE}/actions/runs/456')
        return replies[path]
    api=Mock(request=request,job_log=lambda job_id:log)
    args=dict(api=api,store=Path('unused'),compute=sha,commitment=commitment,runner_id=25,runner_name='synthetic-runner',expires=(now+timedelta(minutes=5)).isoformat(),env=env,source_check=lambda:None,clock=lambda:now,sleep=lambda _:None)
    return args,calls,replies,log

def test_real_delivery_contract_success(case):
    args,calls,_,_=case;result=P.probe(**args)
    assert result['provider_requests']==0 and result['run_id']==456
    assert len([c for c in calls if c[0]=='POST'])==1
    assert not any('/actions/runners/' in c[1] for c in calls)

def test_unknown_post_never_repeated(case):
    args,calls,_,_=case;old=args['api'].request
    def request(method,*a):
        if method=='POST':calls.append(('POST','unknown',None));raise TimeoutError('private-secret')
        return old(method,*a)
    args['api'].request=request
    with pytest.raises(P.ProbeFailure) as err:P.probe(**args)
    assert err.value.stage=='DISPATCH' and str(err.value)==P.STOP
    assert len([c for c in calls if c[0]=='POST'])==1

@pytest.mark.parametrize('kind',['runner','actor','source','expired','attempt','receipt'])
def test_pre_dispatch_gate_no_post(case,monkeypatch,kind):
    args,calls,replies,_=case
    if kind=='runner':args['runner_id']=0
    elif kind=='actor':args['env']['GITHUB_ACTOR']='other'
    elif kind=='source':replies[f'/repos/{P.STORE}/git/ref/heads/basis-consumer-delivery/{P.SOURCE}']['object']['sha']='0'*40
    elif kind=='expired':args['expires']='2020-01-01T00:00:00Z'
    elif kind=='attempt':args['env']['GITHUB_RUN_ATTEMPT']='2'
    else:monkeypatch.setattr(P,'receipt_view',Mock(side_effect=ValueError('private')))
    with pytest.raises(P.ProbeFailure):P.probe(**args)
    assert not any(c[0]=='POST' for c in calls)

@pytest.mark.parametrize('kind',['wrong_runner','bad_marker','wrong_run','poll_timeout','unknown_poll'])
def test_post_dispatch_failure_never_reposts(case,kind):
    args,calls,replies,log=case
    if kind=='wrong_runner':replies[f'/repos/{P.STORE}/actions/runs/456/jobs?per_page=2']['jobs'][0]['runner_id']=26
    elif kind=='bad_marker':log['text']='invalid'
    elif kind=='wrong_run':replies[f'/repos/{P.STORE}/actions/runs/456']['head_sha']='0'*40
    elif kind=='poll_timeout':args['monotonic']=iter([0,181]).__next__
    else:
        old=args['api'].request
        def request(method,path,payload=None):
            if path.endswith('/runs/456'):raise TimeoutError('private')
            return old(method,path,payload)
        args['api'].request=request
    with pytest.raises(P.ProbeFailure):P.probe(**args)
    assert len([c for c in calls if c[0]=='POST'])==1

def test_workflow_no_provider_no_store_writer():
    path=Path(__file__).resolve().parents[1]/'.github/workflows/basis-actions-readiness-probe.yml'
    text=path.read_text();w=yaml.load(text,Loader=yaml.BaseLoader)
    assert 'TWELVE' not in text and 'run_slot' not in text and 'git push' not in text
    assert w['permissions']=={'contents':'read'} and w['concurrency']['group']=='market-data-collector'
    step=w['jobs']['probe']['steps'][-1]
    assert 'MARKET_DATA_STORE_TOKEN' not in step['env']
    assert 'MARKET_DATA_BASIS_ACTIONS_TOKEN' in step['env'] and 'python -B' in step['run']

def test_actual_shell_arguments_use_nonreserved_expected_runner_inputs():
    # GitHub's real hosted name from run 43717196211; no secret or network.
    import os,shlex,subprocess,sys
    workflow=Path(__file__).resolve().parents[1]/'.github/workflows/basis-actions-readiness-probe.yml'
    step=yaml.load(workflow.read_text(),Loader=yaml.BaseLoader)['jobs']['probe']['steps'][-1]
    inputs={'compute_sha':'a'*40,'receipt_commitment':'b'*64,'expected_runner_id':'26','expected_runner_name':'soxx-home-probe-26','expires':'2026-11-15T14:00:00Z'}
    env={'PATH':os.environ['PATH'],'RUNNER_NAME':'GitHub Actions 1000006253','RUNNER_ID':'1000006253'}
    for key,value in step['env'].items():
        if key.startswith(('RUNNER_','GITHUB_')):continue # Default GitHub vars cannot be overridden.
        for name,expected in inputs.items():
            if value=='${{ inputs.'+name+' }}':env[key]=expected
    # Run the actual workflow shell expansion, replacing only Python with an argv observer.
    observer=shlex.quote(sys.executable)+' -c '+shlex.quote('import json,sys; print(json.dumps(sys.argv[1:]))')
    argv=json.loads(subprocess.check_output(['/bin/bash','-e','-c',step['run'].replace('python ',observer+' ',1)],env=env,text=True))
    assert argv[argv.index('--expected-runner-id')+1]=='26'
    assert argv[argv.index('--expected-runner-name')+1]=='soxx-home-probe-26'
    assert not any(k.startswith(('RUNNER_','GITHUB_')) for k in step['env'])

def test_hosted_name_reproduces_parameter_rejection_no_api(case):
    args,calls,_,_=case;args['runner_name']='GitHub Actions 1000006253'
    with pytest.raises(P.ProbeFailure) as err:P.probe(**args)
    assert err.value.stage=='INPUT' and err.value.detail=='RUNNER_PARAMETERS'
    assert calls==[]

def test_invalid_credential_diagnostic_does_not_leak(monkeypatch,capsys):
    import sys
    monkeypatch.setenv('MARKET_DATA_BASIS_ACTIONS_TOKEN','private invalid token')
    monkeypatch.setattr(sys,'argv',['probe','--store','unused','--compute-sha','a'*40,'--receipt-commitment','b'*64,'--expected-runner-id','26','--expected-runner-name','home','--expires','2026-11-15T14:00:00Z'])
    assert P.main()==2
    output=capsys.readouterr().out
    value=json.loads(output)
    assert value['failure_stage']=='INPUT' and value['failure_detail']=='CREDENTIAL_SELECTION'
    assert 'private' not in output and 'token' not in output

def test_closed_one_shot_caller_profile(case):
    args,calls,replies,_=case
    workflow,job=P.CALLER_PROFILES['one_shot']
    args['caller_profile']='one_shot';args['env']['GITHUB_JOB']=job
    args['env']['GITHUB_WORKFLOW_REF']=P.COMPUTE+'/'+workflow+'@refs/heads/main'
    replies['/repos/'+P.COMPUTE+'/actions/runs/123']['path']=workflow
    assert P.probe(**args)['run_id']==456

def test_caller_cannot_invent_workflow_profile(case):
    args,calls,_,_=case;args['caller_profile']='.github/workflows/arbitrary.yml'
    with pytest.raises(P.ProbeFailure):P.probe(**args)
    assert calls==[]
