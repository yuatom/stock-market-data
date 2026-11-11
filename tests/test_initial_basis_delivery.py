import importlib.util,copy,base64,subprocess
from pathlib import Path
import pytest
from scripts import provider_basis_snapshot as B
from test_initial_basis_read import fixture,runtime
from test_daily_basis_remote import BareTransport,TOKEN



def test_get_only_delivery_exists():assert importlib.util.find_spec('scripts.initial_basis_delivery') is not None


class ReadAPI:
    def __init__(self,c):self.c=c;self.calls=[]
    def get(self,path):
        self.calls.append(path);c=self.c;g=c['grant'];base='/repos/'+g['repository']
        if path==base:return copy.deepcopy(c['repository'])
        if path==base+'/actions/runs/555':return copy.deepcopy(c['run'])
        if path==base+'/actions/runs/555/jobs?per_page=2':return copy.deepcopy(c['jobs'])
        if path==base+'/actions/workflows/14':return dict(id=14,path='.github/workflows/basis-consumer-delivery.yml',state='active')
        if path=='/repos/yuatom/stock-dairy/contents/state/runtime-release.json?ref=main':
            raw=B.encode(dict(repository_commit_sha=c.get('release_sha',g['consumer_sha'])));return dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=B.blob_sha(raw))
        raise AssertionError(path)
    def job_log(self,job_id):assert job_id==556;return copy.deepcopy(self.c['log'])


def fresh_case(tmp_path):
    c=fixture(tmp_path);remote=tmp_path/'remote.git';repo=tmp_path/'reader'
    subprocess.run(['git','clone','--bare',str(c['repo']),str(remote)],check=True,capture_output=True)
    subprocess.run(['git','clone','--no-local',str(remote),str(repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','set-url','origin','https://github.com/yuatom/stock-market-data-store'],check=True,capture_output=True)
    transport=BareTransport(repo,remote);transport.record['permissions']['push']=False
    from scripts.initial_basis_remote import InitialReadGitStore
    store=InitialReadGitStore(repo,TOKEN,source_recheck=lambda:None,platform_guard=lambda:None,expected_repository_id=11,expected_owner_id=12,transport=transport)
    return c,store,transport


def read(c,store):
    from scripts.initial_basis_delivery import current_initial_delivery
    return current_initial_delivery(store=store,read_api=ReadAPI(c),approval=runtime(c).approval,grant_raw=B.encode(c['grant']),calendar_raw=B.encode(c['calendar']),consumer_sha=c['grant']['consumer_sha'],trade_date=c['grant']['trade_date'],knowledge_cutoff=c['now'],now=c['now'])


def test_actual_fresh_clone_get_only_read_materializes_exact_receipt(tmp_path):
    c,s,t=fresh_case(tmp_path)
    def forbidden(*a,**k):raise AssertionError('write forbidden')
    s.cas=s.publish=s.commit=forbidden
    result=read(c,s)
    assert result['selector']['read_sha']==c['current'] and len(result['view']['records'])==60
    assert not any('push' in cmd for cmd in t.calls)
    assert result['runtime'].grant_raw==B.encode(c['grant'])


@pytest.mark.parametrize('attack',['expired','wrong_release','wrong_run','wrong_receipt','calendar','after_daily_catalog'])
def test_get_only_errors_do_not_dispatch_acquire_or_publish(tmp_path,attack):
    c,s,t=fresh_case(tmp_path)
    def forbidden(*a,**k):raise AssertionError('write forbidden')
    s.cas=s.publish=s.commit=forbidden
    if attack=='expired':c['now']=c['grant']['valid_until']
    if attack=='wrong_release':c['release_sha']='e'*40
    if attack=='wrong_run':c['run']['id']=777
    if attack=='wrong_receipt':c['grant']['receipt_commit']='e'*40
    if attack=='calendar':c['calendar']={}
    if attack=='after_daily_catalog':c['grant']['selector']['basis_id']='e'*64
    with pytest.raises(ValueError):read(c,s)
    assert not any('push' in cmd for cmd in t.calls)


def test_read_only_store_blocks_all_remote_and_object_writers(tmp_path):
    c,s,t=fresh_case(tmp_path)
    from scripts.daily_basis_remote import RemoteStoreError,STORE_URL
    for operation in (lambda:s.commit('0'*40,{},replace=set()),lambda:s.cas('refs/heads/main','0'*40,'0'*40),lambda:s.publish('0'*40,{},replace=set()),lambda:s._transport.git('push',STORE_URL,'main')):
        with pytest.raises(RemoteStoreError):operation()
    assert not t.pushes and t.record['permissions']['push'] is False


def test_read_only_api_has_no_dispatch_method_and_never_leaks_secret(monkeypatch):
    from scripts.initial_basis_delivery import ReadOnlyGitHubAPI
    import io,contextlib,traceback
    secret='synthetic-private-sentinel'
    class Broken:
        def open(self,*a,**kw):raise RuntimeError(secret)
    api=ReadOnlyGitHubAPI(secret);api._opener=Broken()
    assert not hasattr(api,'request') and not hasattr(api,'post')
    out=io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
        for call in (lambda:api.get('/repos/yuatom/stock-market-data-store'),lambda:api.job_log(556)):
            try:call()
            except ValueError:traceback.print_exc()
    assert secret not in out.getvalue()


def test_github_wrapped_release_content_reaches_actual_read_owner(tmp_path,monkeypatch):
    c,s,t=fresh_case(tmp_path);original=ReadAPI.get
    def wrapped(self,path):
        value=original(self,path)
        if '/contents/' in path:
            raw=base64.b64decode(value['content'],validate=True)
            value['content']=base64.encodebytes(raw).decode('ascii')
            assert '\n' in value['content']
        return value
    monkeypatch.setattr(ReadAPI,'get',wrapped)
    result=read(c,s)
    assert result['view']['record_count']==60 and not t.pushes


@pytest.mark.parametrize('whitespace',['\n','\r\n',' \t\r\n'])
def test_release_content_permits_only_bounded_ascii_wrapping(whitespace):
    from scripts.initial_basis_delivery import _release_bytes
    raw=B.encode({'repository_commit_sha':'b'*40})
    encoded=base64.b64encode(raw).decode('ascii')
    wrapped=whitespace+whitespace.join(encoded[i:i+12] for i in range(0,len(encoded),12))+whitespace
    assert _release_bytes(wrapped)==raw


@pytest.mark.parametrize('bad',['',None,b'e30=','e30=%','e30=\x00','e30=\u00a0','e30=\v','e30=\f','!!!!',' '*1048577])
def test_release_content_rejects_invalid_alphabet_type_or_oversize(bad):
    from scripts.initial_basis_delivery import _release_bytes
    with pytest.raises((B.BasisError,ValueError,UnicodeError)):_release_bytes(bad)
