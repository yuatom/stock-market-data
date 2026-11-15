"""Synthetic readonly API responses; no network, token or provider access."""
import base64,json,traceback,urllib.error
from unittest.mock import Mock
import pytest
from scripts import run_daily_basis_route as H,run_one_shot_soxx_basis_gap as M


def setup(fail=None,error=None):
    grant=dict(repository_id=11,owner_id=12,workflow_id=14,workflow_source_sha='c'*40,consumer_sha='b'*40)
    authority=dict(repository_id=11,owner_id=12,readiness_run_id=444,actions_free_until='2026-11-16T00:00:00Z')
    repo=dict(private=True,id=11,full_name=H.STORE,owner={'id':12})
    raw=H.B.encode({'repository_commit_sha':'b'*40})
    responses={
        '/repos/'+H.STORE:('READINESS_REPOSITORY',repo),
        '/user':('READINESS_TOKEN_IDENTITY',{'id':12}),
        '/repos/'+H.STORE+'/actions/workflows/14':('READINESS_WORKFLOW',dict(id=14,path=H.VERIFIER,state='active')),
        '/repos/'+H.STORE+'/actions/runs/444':('READINESS_SUCCESS_RUN',dict(workflow_id=14,head_sha='c'*40,path=H.VERIFIER,status='completed',conclusion='success',run_attempt=1,event='workflow_dispatch',actor={'id':12},triggering_actor={'id':12})),
        '/repos/yuatom/stock-dairy/contents/state/runtime-release.json?ref=main':('READINESS_CONSUMER_RELEASE',dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=H.B.blob_sha(raw)))}
    calls=[]
    def guard(step):
        calls.append(step)
        if step==fail:raise error
    def request(method,path):
        assert method=='GET';step,data=responses[path];guard(step);return data
    def capability(workflow):guard('READINESS_DISPATCH_CAPABILITY');return True
    def remote(ref):guard('READINESS_SOURCE_REF');return 'c'*40
    return Mock(request=request,dispatch_capability=capability),Mock(remote_ref=remote),authority,grant,calls

@pytest.mark.parametrize('step',sorted(H.READINESS_STEPS))
def test_http_endpoint_failure_emits_only_fixed_step_and_numeric_status(step):
    secret='private-token-and-url'
    api,store,authority,grant,calls=setup(step,urllib.error.HTTPError(secret,403,secret,{},None))
    with pytest.raises(H.ReadinessFailure) as error:H.check_private_readiness(api,store,authority,grant,now='2026-11-15T09:30:00Z')
    output=M.failure_result(M.OneShotFailure('PRIVATE_READINESS',error.value))
    assert output['failure_detail']==step and output['http_status']==403 and output['error_category']=='HTTP_ERROR'
    assert output['automatic_retry_allowed'] is False
    assert secret not in json.dumps(output)+''.join(traceback.format_exception(error.value))
    assert calls[-1]==step and calls.count(step)==1

@pytest.mark.parametrize('error,kind',[(urllib.error.URLError('private-url'),'NETWORK_ERROR'),(TimeoutError('secret'),'TIMEOUT')])
def test_network_failure_has_no_invented_http_status(error,kind):
    api,store,a,g,_=setup('READINESS_TOKEN_IDENTITY',error)
    with pytest.raises(H.ReadinessFailure) as failure:H.check_private_readiness(api,store,a,g,now='2026-11-15T09:30:00Z')
    output=M.failure_result(M.OneShotFailure('PRIVATE_READINESS',failure.value))
    assert output['http_status'] is None and output['error_category']==kind

def test_success_read_order_unchanged():
    api,store,a,g,calls=setup()
    assert H.check_private_readiness(api,store,a,g,now='2026-11-15T09:30:00Z')['id']==11
    assert calls==['READINESS_REPOSITORY','READINESS_TOKEN_IDENTITY','READINESS_DISPATCH_CAPABILITY','READINESS_WORKFLOW','READINESS_SOURCE_REF','READINESS_SUCCESS_RUN','READINESS_CONSUMER_RELEASE']
