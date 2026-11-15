"""Offline credential separation: no secret lookup, network or provider."""
import json,urllib.error
from unittest.mock import Mock,patch
from pathlib import Path
import pytest,yaml
from scripts import run_daily_basis_route as H,run_one_shot_soxx_basis_gap as M
from test_soxx_readiness_diagnostics import setup as readiness_setup
PATH='/repos/yuatom/stock-dairy/contents/state/runtime-release.json?ref=main'

def test_adapter_only_fixed_single_get_interface():
    with patch.object(H,'GitHubAPI') as cls:
        reader=H.ReportReleaseReader('synthetic-source')
        reader.request('GET',PATH)
        cls.assert_called_once_with('synthetic-source');cls.return_value.request.assert_called_once_with('GET',PATH)
        for method,path in [('POST',PATH),('GET','/user'),('GET','/repos/yuatom/stock-market-data-store')]:
            with pytest.raises(H.HostedDailyError):reader.request(method,path)
        assert cls.return_value.request.call_count==1

def test_explicit_source_credential_empty_or_invalid_never_falls_back():
    for value in ('','bad token',42):
        with pytest.raises(H.HostedDailyError):H.report_release_reader({'MARKET_DATA_SOURCE_TOKEN':value})
    with pytest.raises(H.HostedDailyError):H.report_release_reader({})

def test_private_readiness_uses_release_api_only_for_report():
    api,store,a,g,calls=readiness_setup();payload=api.request('GET',PATH);calls.clear()
    release=Mock();release.request.return_value=payload
    H.check_private_readiness(api,store,a,g,now='2026-11-15T09:30:00Z',release_api=release)
    release.request.assert_called_once_with('GET',PATH)
    assert 'READINESS_CONSUMER_RELEASE' not in calls
    assert calls==['READINESS_REPOSITORY','READINESS_TOKEN_IDENTITY','READINESS_DISPATCH_CAPABILITY','READINESS_WORKFLOW','READINESS_SOURCE_REF','READINESS_SUCCESS_RUN']

def test_release_404_does_not_retry_with_actions_token():
    api,store,a,g,calls=readiness_setup();release=Mock();release.request.side_effect=urllib.error.HTTPError('private',404,'private',{},None)
    with pytest.raises(H.ReadinessFailure) as e:H.check_private_readiness(api,store,a,g,now='2026-11-15T09:30:00Z',release_api=release)
    release.request.assert_called_once();assert 'READINESS_CONSUMER_RELEASE' not in calls
    output=M.failure_result(M.OneShotFailure('PRIVATE_READINESS',e.value))
    assert output['http_status']==404 and 'private' not in json.dumps(output)

def test_release_original_content_hash_and_pin_still_checked():
    api,store,a,g,_=readiness_setup();payload=api.request('GET',PATH)
    release=Mock();release.request.return_value={**payload,'sha':'0'*40}
    with pytest.raises(H.HostedDailyError):H.check_private_readiness(api,store,a,g,now='2026-11-15T09:30:00Z',release_api=release)

def test_only_one_shot_workflow_injects_source_secret():
    root=Path(__file__).resolve().parents[1]
    workflow=yaml.load((root/'.github/workflows/one-shot-soxx-basis-gap.yml').read_text(),Loader=yaml.BaseLoader)
    steps=workflow['jobs']['soxx_gap']['steps']
    selected=[s for s in steps if 'MARKET_DATA_SOURCE_TOKEN' in s.get('env',{})]
    assert len(selected)==1 and selected[0]['name']=='Execute one reviewed finite SOXX slot'
    assert selected[0]['env']['MARKET_DATA_SOURCE_TOKEN']=='${{ secrets.MARKET_DATA_SOURCE_TOKEN }}'

def test_cli_separates_actions_store_and_report_credentials(monkeypatch,capsys):
    import sys
    from contextlib import nullcontext
    seen={}
    monkeypatch.setattr(M.os,'environ',{'MARKET_DATA_STORE_TOKEN':'synthetic-store','MARKET_DATA_BASIS_ACTIONS_TOKEN':'synthetic-actions','MARKET_DATA_SOURCE_TOKEN':'synthetic-source','GITHUB_RUN_ID':'123'})
    monkeypatch.setattr(sys,'argv',['offline','--store','unused','--compute-sha','a'*40,'--expected-store-sha','b'*40,'--scope-commit','c'*40,'--scope-sha','d'*64,'--preflight-only','true'])
    monkeypatch.setattr(M,'_quiet',nullcontext);monkeypatch.setattr(M,'_environment',nullcontext);monkeypatch.setattr(M,'_source',lambda *a:None);monkeypatch.setattr(M,'validate_surface',lambda *a:None)
    actions=Mock();source=Mock()
    def actions_api(token):seen['actions']=token;return actions
    def source_api(token):seen['source']=token;return source
    def store(path,token,**kw):seen['store']=token;return Mock()
    monkeypatch.setattr(M,'GitHubAPI',actions_api);monkeypatch.setattr(H,'GitHubAPI',source_api);monkeypatch.setattr(M,'RemoteGitStore',store)
    def run(**kw):
        assert kw['api'] is actions and kw['release_api']._api is source
        return dict(status='OFFLINE')
    monkeypatch.setattr(M,'run_once',run)
    assert M.main()==0 and seen=={'actions':'synthetic-actions','source':'synthetic-source','store':'synthetic-store'}
    assert 'synthetic' not in capsys.readouterr().out

@pytest.mark.parametrize('with_probe',[False,True])
def test_run_once_passes_report_reader_on_both_paths(with_probe):
    from test_one_shot_soxx_basis_gap import OneShotScope
    from test_soxx_same_process_readiness import setup as same_process_setup
    if with_probe:args,u=same_process_setup()
    else:
        args=OneShotScope().hosted_mocks();u=Mock()
        u.run_slot.return_value=Mock(selector={'basis_id':M.BASIS},receipt_commitment='9'*64)
        u.read_current.return_value=dict(record_count=320,last_trade_date='2026-11-13',volume_status='BLOCKED_UNRESOLVED')
    reader=Mock();args['release_api']=reader
    def check(api,store,scope,grant,**kw):assert kw['release_api'] is reader;return {}
    with patch.object(M,'DailyUpdater',return_value=u),patch.object(M,'wait_for_daily_budget_window'),patch.object(M.readiness_probe,'probe',return_value=dict(status='BASIS_ACTIONS_READINESS_VERIFIED',provider_requests=0,run_id=456)),patch.object(M,'check_private_readiness',side_effect=check) as gate,patch.object(M,'dispatch_verify_once',return_value=Mock(run_id=789)):
        M.run_once(**args)
    gate.assert_called_once()
