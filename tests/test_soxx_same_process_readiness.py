"""Offline control-flow tests; no live platform/permission or provider claims."""
from datetime import timedelta
from unittest.mock import Mock,patch
import pytest
from scripts import run_one_shot_soxx_basis_gap as M,run_daily_basis_route as H
from test_one_shot_soxx_basis_gap import OneShotScope


def setup():
    t=OneShotScope();args=t.hosted_mocks();s,g,c,n=t.fixture()
    r=dict(commitment='9'*64,runner_id=26,runner_name='soxx-home',expires=(n+timedelta(minutes=5)).isoformat())
    s['readiness']=r;s['workflow_source_sha']=g['workflow_source_sha']=M.readiness_probe.SOURCE
    g['workflow_id']=M.readiness_probe.WORKFLOW_ID;t.validate(s,g,c,n)
    raw=M.B.encode(s);args['scope_sha']=M.B.sha(raw)
    args['store'].read.side_effect=lambda commit,path:raw if path==M.SCOPE_PATH else M.B.encode(g if 'grant_ref' in path else c)
    args['readiness']=r;args['store'].repo='unused'
    u=Mock();u.run_slot.return_value=Mock(selector={'basis_id':M.BASIS},receipt_commitment='9'*64)
    u.read_current.return_value=dict(record_count=320,last_trade_date='2026-11-13',volume_status='BLOCKED_UNRESOLVED')
    return args,u

def test_same_api_order_one_capture_one_final_verify():
    args,u=setup();events=[]
    u.preflight_slot.side_effect=lambda *a:events.append('preclaim')
    u.run_slot.side_effect=lambda *a,**k:(events.append('capture') or Mock(selector={'basis_id':M.BASIS},receipt_commitment='9'*64))
    def probe(**kw):
        assert kw['api'] is args['api'] and kw['caller_profile']=='one_shot';events.append('probe')
        return dict(status='BASIS_ACTIONS_READINESS_VERIFIED',provider_requests=0,run_id=456)
    def check(api,store,scope,grant,**kw):
        assert api is args['api'] and scope['readiness_run_id']==456;events.append('permission');return {}
    def verify(api,updater,candidate,**kw):
        assert api is args['api'] and kw['runner_expectation']['runner_id']==26
        events.append('verify');return Mock(run_id=789)
    with patch.object(M,'DailyUpdater',return_value=u),patch.object(M,'wait_for_daily_budget_window',side_effect=lambda *a,**k:events.append('budget')),patch.object(M.readiness_probe,'probe',side_effect=probe),patch.object(M,'check_private_readiness',side_effect=check),patch.object(M,'dispatch_verify_once',side_effect=verify):
        result=M.run_once(**args)
    assert events==['budget','preclaim','probe','budget','permission','capture','verify']
    u.run_slot.assert_called_once();u.promote.assert_called_once();assert result['verification_run_id']==789 and result['readiness_run_id']==456

@pytest.mark.parametrize('where',['probe','permission','verify'])
def test_failure_no_later_capture_or_promote(where):
    args,u=setup();good=dict(status='BASIS_ACTIONS_READINESS_VERIFIED',provider_requests=0,run_id=456)
    with patch.object(M,'DailyUpdater',return_value=u),patch.object(M,'wait_for_daily_budget_window'),patch.object(M.readiness_probe,'probe',return_value=good,side_effect=TimeoutError() if where=='probe' else None) as probe,patch.object(M,'check_private_readiness',return_value={},side_effect=ValueError() if where=='permission' else None),patch.object(M,'dispatch_verify_once',side_effect=ValueError('runner mismatch') if where=='verify' else None) as verify:
        with pytest.raises(M.OneShotFailure):M.run_once(**args)
    probe.assert_called_once();u.promote.assert_not_called()
    if where!='verify':u.run_slot.assert_not_called();verify.assert_not_called()
    else:u.run_slot.assert_called_once();verify.assert_called_once()

@pytest.mark.parametrize('case',['preflight','wrong','omitted'])
def test_bound_scope_rejects_before_probe(case):
    args,u=setup()
    if case=='preflight':args['preflight_only']=True
    elif case=='wrong':args['readiness']={**args['readiness'],'runner_id':99}
    else:args['readiness']=None
    with patch.object(M.readiness_probe,'probe') as probe,patch.object(M,'DailyUpdater',return_value=u):
        with pytest.raises(M.OneShotFailure):M.run_once(**args)
    probe.assert_not_called();u.run_slot.assert_not_called()

@pytest.mark.parametrize('values',[('9'*64,None,'home','2026-11-15T12:00:00Z'),('','26','home',''),('9'*64,'bad','home','2026-11-15T12:00:00Z')])
def test_partial_arguments_rejected(values):
    with pytest.raises((ValueError,M.HostedDailyError) if hasattr(M,'HostedDailyError') else ValueError):M.readiness_arguments(*values,False)

def test_optional_arguments_empty_preserve_legacy():
    assert M.readiness_arguments(None,None,None,None,True) is None
    assert M.readiness_arguments('','','','',False) is None

def test_final_fetch_wrong_runner_before_log():
    api=Mock();api.request.side_effect=[{},dict(total_count=1,jobs=[dict(runner_id=99,runner_name='bad',labels=[])])]
    u=Mock()
    with pytest.raises(H.HostedDailyError):H.fetch_delivery(api,u,Mock(),123,now='2026-11-15T12:00:00Z',repository={},runner_expectation=dict(runner_id=26,runner_name='home',labels=sorted(M.readiness_probe.LABELS),expires='2026-11-15T12:05:00Z'))
    api.job_log.assert_not_called();u.delivery_expectation.assert_not_called()

def test_readiness_scope_is_closed_and_time_bounded():
    t=OneShotScope();s,g,c,n=t.fixture()
    s['readiness']=dict(commitment='9'*64,runner_id=26,runner_name='home',expires=(n+timedelta(minutes=5)).isoformat(),arbitrary='no')
    with pytest.raises(Exception):t.validate(s,g,c,n)
    del s['readiness']['arbitrary'];s['readiness']['expires']=(n+timedelta(days=1)).isoformat()
    with pytest.raises(Exception):t.validate(s,g,c,n)

def test_cli_group_reaches_run_once_without_reconstructing_api(monkeypatch,capsys):
    import sys
    from contextlib import nullcontext
    expected=dict(commitment='9'*64,runner_id=26,runner_name='home',expires='2026-11-15T16:05:00Z')
    monkeypatch.setattr(sys,'argv',['offline','--store','unused','--compute-sha','a'*40,'--expected-store-sha','b'*40,'--scope-commit','c'*40,'--scope-sha','d'*64,'--preflight-only','false','--readiness-receipt-commitment',expected['commitment'],'--readiness-runner-id','26','--readiness-runner-name','home','--readiness-expires',expected['expires']])
    monkeypatch.setenv('MARKET_DATA_SOURCE_TOKEN','synthetic-source');monkeypatch.setenv('GITHUB_RUN_ID','123');monkeypatch.setenv('MARKET_DATA_STORE_TOKEN','synthetic-store');monkeypatch.setattr(M,'_quiet',nullcontext);monkeypatch.setattr(M,'_environment',nullcontext)
    monkeypatch.setattr(M,'_source',lambda *a:None);monkeypatch.setattr(M,'validate_surface',lambda *a:None)
    api=Mock();monkeypatch.setattr(M,'GitHubAPI',lambda token:api);monkeypatch.setattr(M,'RemoteGitStore',Mock())
    def run(**kw):
        assert kw['readiness']==expected and kw['api'] is api and not kw['preflight_only'];return {'status':'OFFLINE'}
    monkeypatch.setattr(M,'run_once',run)
    assert M.main()==0
