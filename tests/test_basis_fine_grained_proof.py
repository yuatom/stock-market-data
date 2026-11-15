"""Synthetic HTTP only; no live token, dispatch or provider."""
import json,urllib.error
from email.message import Message
import pytest
from scripts import run_daily_basis_route as H
PATH='/repos/'+H.STORE+'/actions/workflows/415302427/dispatches'
PAYLOAD={'ref':'basis-consumer-delivery/'+'a'*40,'inputs':{'compute_sha':'b'*40,'receipt_commitment':'c'*64}}
GOOD={'workflow_run_id':123,'run_url':'https://api.github.com/repos/'+H.STORE+'/actions/runs/123','html_url':'https://github.com/'+H.STORE+'/actions/runs/123'}
class Reply:
    status=200
    def __init__(self,url,value):self.url=url;self.value=value;self.headers=Message()
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def geturl(self):return self.url
    def read(self,n):return json.dumps(self.value).encode()
class Transport:
    def __init__(self,value=GOOD,status=200):self.value=value;self.status=status;self.calls=[]
    def open(self,req,timeout):
        self.calls.append(req.method)
        if req.method=='POST' and self.status!=200:raise urllib.error.HTTPError(req.full_url,self.status,'synthetic',{},None)
        return Reply(req.full_url,self.value if req.method=='POST' else {'id':415302427})
def client(value=GOOD,status=200):
    api=H.GitHubAPI('synthetic-token');api.opener=Transport(value,status);return api

def test_only_same_instance_successful_target_dispatch_proves_write():
    api=client();assert api.dispatch_capability(415302427) is False
    assert api.request('POST',PATH,PAYLOAD)==GOOD
    assert api.dispatch_capability(415302427) is True
    assert api.dispatch_capability(999) is False
    assert client().dispatch_capability(415302427) is False

@pytest.mark.parametrize('value',[{},dict(GOOD,workflow_run_id=True),dict(GOOD,run_url='https://wrong/123'),dict(GOOD,html_url='https://wrong/123'),dict(GOOD,unexpected=True)])
def test_ambiguous_dispatch_reply_does_not_create_proof(value):
    api=client(value)
    with pytest.raises((H.HostedDailyError,H.B.BasisError)):api.request('POST',PATH,PAYLOAD)
    assert api.dispatch_capability(415302427) is False
    assert api.opener.calls.count('POST')==1

@pytest.mark.parametrize('status',[403,429,500])
def test_dispatch_http_failure_never_retries_or_proves_permission(status):
    api=client(status=status)
    with pytest.raises(urllib.error.HTTPError):api.request('POST',PATH,PAYLOAD)
    assert api.dispatch_capability(415302427) is False
    assert api.opener.calls.count('POST')==1

def test_other_repository_dispatch_never_proves_store_write():
    api=client();api.request('POST',PATH.replace(H.STORE,H.COMPUTE),PAYLOAD)
    assert api.dispatch_capability(415302427) is False

def test_get_cannot_create_proof_even_with_dispatch_shaped_response():
    api=client();api.request('GET',PATH)
    assert api.dispatch_capability(415302427) is False


def test_proof_expires_and_does_not_survive_backward_clock(monkeypatch):
    now=[1000.0];monkeypatch.setattr(H.time,'monotonic',lambda:now[0])
    api=client();api.request('POST',PATH,PAYLOAD)
    now[0]=999;assert api.dispatch_capability(415302427) is False
    now[0]=1301;assert api.dispatch_capability(415302427) is False

def test_later_failed_dispatch_invalidates_earlier_proof():
    api=client();api.request('POST',PATH,PAYLOAD)
    api.opener.status=403
    with pytest.raises(urllib.error.HTTPError):api.request('POST',PATH,PAYLOAD)
    assert api.dispatch_capability(415302427) is False
