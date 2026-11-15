"""Synthetic credential wiring only: no network, stored secrets or provider."""
import contextlib,sys
from pathlib import Path
import pytest,yaml
from scripts import run_daily_basis_route as H,run_one_shot_soxx_basis_gap as M

@pytest.mark.parametrize('extra,expected',[(None,'original-store'),('','original-store'),('dedicated-actions','dedicated-actions')])
def test_select_optional_actions_secret_without_changing_store(extra,expected):
    env={'MARKET_DATA_STORE_TOKEN':'original-store'}
    if extra is not None:env['MARKET_DATA_BASIS_ACTIONS_TOKEN']=extra
    assert H.basis_actions_token(env)==expected
    assert env['MARKET_DATA_STORE_TOKEN']=='original-store'

@pytest.mark.parametrize('invalid',[' ','bad\nvalue','short',42])
def test_invalid_explicit_actions_token_never_falls_back(invalid):
    with pytest.raises(H.HostedDailyError):H.basis_actions_token({'MARKET_DATA_STORE_TOKEN':'original-store','MARKET_DATA_BASIS_ACTIONS_TOKEN':invalid})

@pytest.mark.parametrize('module',[H,M])
@pytest.mark.parametrize('dedicated',[False,True])
def test_cli_uses_actions_for_api_and_original_for_store(monkeypatch,capsys,module,dedicated):
    sha='a'*40;seen={}
    env={'MARKET_DATA_STORE_TOKEN':'synthetic-store','DAILY_BASIS_GRANT_COMMITMENT':'b'*64,'GITHUB_RUN_ID':'1','GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'schedule','GITHUB_RUN_ATTEMPT':'1','GITHUB_REPOSITORY':H.COMPUTE,'GITHUB_SHA':sha,'GITHUB_JOB':'collect'}
    if dedicated:env['MARKET_DATA_BASIS_ACTIONS_TOKEN']='synthetic-actions'
    monkeypatch.setattr(module.os,'environ',env)
    monkeypatch.setattr(module,'_quiet',contextlib.nullcontext)
    monkeypatch.setattr(module,'_environment',contextlib.nullcontext)
    monkeypatch.setattr(module,'_source',lambda *args:None)
    monkeypatch.setattr(M,'validate_surface',lambda *args:None)
    class API:
        def __init__(self,token):seen['api']=token
        def request(self,method,path):
            if '/actions/runs/' not in path:return {'full_name':H.COMPUTE,'id':1,'owner':{'id':2}}
            return {'repository':{'id':1},'head_repository':{'id':1},'actor':{'id':2},'triggering_actor':{'id':2},'head_sha':sha,'path':H.CALLER,'event':'schedule','run_attempt':1,'status':'in_progress'}
    def store(path,token,**kwargs):
        seen['store']=token
        raise RuntimeError('offline stop before store access')
    monkeypatch.setattr(module,'GitHubAPI',API)
    if module is M:monkeypatch.setattr(M,'RemoteGitStore',store)
    else:
        from scripts import daily_basis_remote
        monkeypatch.setattr(daily_basis_remote,'RemoteGitStore',store)
    args=['--store','synthetic','--compute-sha',sha]
    if module is M:
        args+=['--expected-store-sha',sha,'--scope-commit',sha,'--scope-sha','b'*64,'--preflight-only','true']
        monkeypatch.setattr(sys,'argv',['offline-test']+args);result=module.main()
    else:result=module.main(args+['--trade-date','2026-11-13','--delegation-output','unused'])
    assert result==2
    assert seen=={'api':'synthetic-actions' if dedicated else 'synthetic-store','store':'synthetic-store'}
    output=capsys.readouterr().out
    assert 'synthetic-store' not in output and 'synthetic-actions' not in output

def test_workflow_secret_is_optional_and_only_injected_into_basis_steps():
    root=Path(__file__).resolve().parents[1];name='MARKET_DATA_BASIS_ACTIONS_TOKEN'
    runtime=yaml.load((root/'.github/workflows/market-data-collector-runtime.yml').read_text(),Loader=yaml.BaseLoader)
    assert runtime['on']['workflow_call']['secrets'][name]['required']=='false'
    steps=runtime['jobs']['collect']['steps']
    assert [s['name'] for s in steps if name in s.get('env',{})]==['Run finite daily basis owner']
    one=yaml.load((root/'.github/workflows/one-shot-soxx-basis-gap.yml').read_text(),Loader=yaml.BaseLoader)
    assert [s['name'] for s in one['jobs']['soxx_gap']['steps'] if name in s.get('env',{})]==['Execute one reviewed finite SOXX slot']
