"""Synthetic Git only; all private/provider/remote boundaries are replaced."""
import base64
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def module():
    from scripts import run_private_authority_route
    return run_private_authority_route


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    m = module()
    root = tmp_path/'compute'; root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.email', 'synthetic@example.invalid')
    git(root, 'config', 'user.name', 'Synthetic')
    (root/'scripts').mkdir()
    (root/'scripts/run_private_authority_route.py').write_text('# synthetic reviewed source\n')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'synthetic')
    sha = git(root, 'rev-parse', 'HEAD')
    git(root, 'remote', 'add', 'origin', m.COMPUTE_URL)
    monkeypatch.setattr(m, '__file__', str(root/'scripts/run_private_authority_route.py'))
    env = dict(GITHUB_ACTIONS='true', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main',
        GITHUB_SHA=sha, GITHUB_RUN_ATTEMPT='1', GITHUB_RUN_ID='123', GITHUB_REPOSITORY='yuatom/stock-market-data',
        GITHUB_ACTOR='yuatom', GITHUB_TRIGGERING_ACTOR='yuatom',
        GITHUB_WORKFLOW_REF='yuatom/stock-market-data/.github/workflows/soxx-capture-only.yml@refs/heads/main')
    for key, value in env.items(): monkeypatch.setenv(key, value)
    selection=SimpleNamespace(approval_raw=b'PRIVATE APPROVAL', approval_sha256='a'*64,
        authority_store_sha='b'*40, descriptor_commit='d'*40)
    def materialize(**kwargs):
        store=Path(kwargs['destination']);store.mkdir()
        git(store,'init','-q','-b','main');git(store,'config','user.name','Synthetic')
        git(store,'config','user.email','synthetic@example.invalid')
        (store/'authority').write_text('synthetic');git(store,'add','.');git(store,'commit','-qm','synthetic')
        selection.authority_store_sha=git(store,'rev-parse','HEAD')
        git(store,'remote','add','origin',m.STORE_URL)
        return selection
    prepare=Mock(side_effect=materialize); monkeypatch.setattr(m, 'prepare_promoted_authority', prepare)
    monkeypatch.setattr(m,'_store_remote_refs',lambda store,ref:
        selection.authority_store_sha+'\trefs/heads/main\n'+selection.descriptor_commit+'\t'+ref)
    remote=Mock(return_value=sha); monkeypatch.setattr(m, '_remote_main', remote)
    invoke=Mock(return_value=None); monkeypatch.setattr(m, '_invoke', invoke)
    args=dict(route='soxx_capture_only', compute_sha=sha, expected_commitment='e'*64,
        token='SYNTHETIC_TOKEN', key='SYNTHETIC_KEY', destination=tmp_path/'store')
    return m, root, args, prepare, invoke, selection


def test_missing_implementation_is_explicit():
    assert callable(module().run_route)


def test_source_verified_before_private_access_and_dispatch(setup):
    m, root, args, prepare, invoke, selection=setup
    assert m.run_route(**args) == 'PRIVATE_ROUTE_COMPLETE'
    prepare.assert_called_once()
    assert callable(prepare.call_args.kwargs['source_recheck'])
    assert invoke.call_count == 1
    assert invoke.call_args.kwargs['selection'] is selection
    assert invoke.call_args.kwargs['request_id'] == '123-1'
    assert git(root,'config','--local','--get-regexp','remote.origin.url') == 'remote.origin.url '+m.COMPUTE_URL


@pytest.mark.parametrize('mutation',['dirty','untracked','ignored','wrong_origin','pushurl','rewrite','replace','graft','wrong_sha','remote_changed','identity'])
def test_source_failures_stop_before_private_access(setup,mutation,monkeypatch):
    m, root, args, prepare, invoke, _=setup
    if mutation=='dirty': (root/'scripts/run_private_authority_route.py').write_text('dirty')
    if mutation=='untracked': (root/'surprise').write_text('untracked')
    if mutation=='ignored':
        (root/'.git/info/exclude').write_text('hidden.py\n'); (root/'hidden.py').write_text('ignored')
    if mutation=='wrong_origin': git(root,'remote','set-url','origin','https://example.invalid/repo')
    if mutation=='pushurl': git(root,'config','remote.origin.pushurl','https://example.invalid/repo')
    if mutation=='rewrite': git(root,'config','url.https://example.invalid/.insteadOf','https://github.com/')
    if mutation=='replace': git(root,'update-ref','refs/replace/'+args['compute_sha'],args['compute_sha'])
    if mutation=='graft': (root/'.git/info/grafts').write_text(args['compute_sha'])
    if mutation=='wrong_sha': args['compute_sha']='f'*40
    if mutation=='remote_changed': monkeypatch.setattr(m,'_remote_main',Mock(return_value='f'*40))
    if mutation=='identity': monkeypatch.setenv('GITHUB_RUN_ATTEMPT','2')
    with pytest.raises(m.PrivateRouteError) as caught: m.run_route(**args)
    assert str(caught.value)=='PRIVATE_ROUTE_STOPPED_INSPECT_BEFORE_RETRY'
    assert caught.value.__context__ is None
    prepare.assert_not_called(); invoke.assert_not_called()


def test_private_failure_is_quiet_and_not_retried(setup, capfd):
    m, _, args, prepare, invoke, _=setup
    def fail(**kwargs):
        print('PRIVATE PIN'); os.write(2,b'PRIVATE TOKEN'); raise ValueError('PRIVATE RECEIPT')
    prepare.side_effect=fail
    with pytest.raises(m.PrivateRouteError) as caught: m.run_route(**args)
    assert caught.value.__context__ is None
    assert capfd.readouterr()==('','')
    assert prepare.call_count==1; invoke.assert_not_called()


def test_ephemeral_auth_is_scoped_sanitized_and_restored(setup,monkeypatch):
    m, _, args, _, invoke, _=setup
    monkeypatch.setenv('GIT_TRACE','/tmp/must-not-be-written')
    monkeypatch.setenv('HTTPS_PROXY','https://example.invalid')
    before=dict(os.environ)
    def check(**kwargs):
        assert 'GIT_TRACE' not in os.environ and 'HTTPS_PROXY' not in os.environ
        settings={os.environ['GIT_CONFIG_KEY_'+str(i)]:os.environ['GIT_CONFIG_VALUE_'+str(i)]
                  for i in range(int(os.environ['GIT_CONFIG_COUNT']))}
        assert settings['http.'+m.STORE_URL+'.extraheader']=='AUTHORIZATION: basic '+base64.b64encode(b'x-access-token:SYNTHETIC_TOKEN').decode()
        assert settings['http.followRedirects']=='false'
        assert settings['credential.helper']==''
        assert settings['protocol.allow']=='never'
        raise RuntimeError('PRIVATE')
    invoke.side_effect=check
    with pytest.raises(m.PrivateRouteError): m.run_route(**args)
    assert dict(os.environ)==before


def test_recheck_source_after_private_prepare_stops_coordinator(setup):
    m, root, args, prepare, invoke, selection=setup
    def mutate(**kwargs):
        (root/'changed').write_text('mutation'); return selection
    prepare.side_effect=mutate
    with pytest.raises(m.PrivateRouteError): m.run_route(**args)
    invoke.assert_not_called()


@pytest.mark.parametrize('route',['soxx_capture_only','eleven_symbol_conditional_append'])
def test_dispatch_preserves_coordinator_arguments(monkeypatch,tmp_path,route):
    m=module()
    execute=Mock(return_value={'status':'CAPTURE_RETAINED_UNQUALIFIED'})
    official=Mock(return_value={'status':'COMPLETE'})
    helpers=SimpleNamespace(collectors=SimpleNamespace(load_yaml=Mock(return_value={})))
    soxx=SimpleNamespace(execute=execute,private_identity=Mock(return_value={'private':True}),
        admission=SimpleNamespace(load_helpers=Mock(return_value=helpers)))
    eleven=SimpleNamespace(run_official=official)
    monkeypatch.setattr(m,'_coordinator',lambda r: soxx if r=='soxx_capture_only' else eleven)
    selection=SimpleNamespace(approval_raw=b'private',approval_sha256='a'*64,authority_store_sha='b'*40)
    m._invoke(route=route,compute=tmp_path,compute_sha='c'*40,store=tmp_path/'store',
        selection=selection,request_id='123-1',key='key',token='token')
    call=execute.call_args if route=='soxx_capture_only' else official.call_args
    assert call.kwargs['request_id']=='123-1'
    if route=='soxx_capture_only':
        assert call.kwargs['approval_commit']==selection.authority_store_sha
        assert call.kwargs['approval_sha256']==selection.approval_sha256
    else:
        assert call.kwargs['approval_raw'] is selection.approval_raw
        assert call.kwargs['approval_digest']==selection.approval_sha256
    assert 'surface_guard' not in call.kwargs and 'transport' not in call.kwargs


@pytest.mark.parametrize('status',['STOPPED','PARTIAL','UNKNOWN'])
def test_noncomplete_coordinator_results_fail_closed(monkeypatch,tmp_path,status):
    m=module()
    monkeypatch.setattr(m,'_coordinator',lambda route: SimpleNamespace(run_official=lambda **kw: {'status':status}))
    with pytest.raises(ValueError):
        m._invoke(route='eleven_symbol_conditional_append',compute=tmp_path,compute_sha='c'*40,
            store=tmp_path/'store', selection=SimpleNamespace(approval_raw=b'p',approval_sha256='a'*64),
            request_id='123-1',key='key',token='token')


def test_cli_rejects_private_arguments_quietly(capsys,monkeypatch):
    m=module(); run=Mock();monkeypatch.setattr(m,'run_route',run)
    assert m.main(['--approval-json','PRIVATE'])==2
    assert capsys.readouterr()==(m.STOP+'\n','')
    run.assert_not_called()


@pytest.mark.parametrize('mutation',['head','origin','ref','dirty'])
def test_store_binding_rechecked_immediately_before_coordinator(setup,mutation,monkeypatch):
    m, _, args, prepare, invoke, selection=setup
    store=args['destination']; prepare.side_effect(destination=store)
    prepare.side_effect=None; prepare.return_value=selection
    ref='refs/heads/capture-authority/'+args['route']+'/'+args['compute_sha']
    expected=selection.authority_store_sha+'\trefs/heads/main\n'+selection.descriptor_commit+'\t'+ref
    monkeypatch.setattr(m,'_store_remote_refs',Mock(return_value=expected))
    if mutation=='head': selection.authority_store_sha='f'*40
    if mutation=='origin': git(store,'remote','set-url','origin','https://example.invalid')
    if mutation=='ref': monkeypatch.setattr(m,'_store_remote_refs',Mock(return_value=expected.replace('d'*40,'f'*40)))
    if mutation=='dirty': (store/'authority').write_text('changed')
    with pytest.raises(m.PrivateRouteError):m.run_route(**args)
    invoke.assert_not_called()


def test_auth_url_matching_never_sends_store_token_to_compute_or_foreign(tmp_path):
    m=module()
    with m._environment('SYNTHETIC_TOKEN'):
        def match(url):
            return subprocess.run(['git','config','--get-urlmatch','http.extraheader',url],capture_output=True)
        actual=match(m.STORE_URL+'/git-upload-pack')
        assert actual.returncode==0 and b'AUTHORIZATION: basic ' in actual.stdout
        for url in (m.COMPUTE_URL,m.STORE_URL+'-other', 'https://example.invalid/repo',
                    'https://github.com/yuatom/stock-market-data-store-other.git'):
            assert match(url).returncode==1
    assert 'GIT_CONFIG_COUNT' not in os.environ


def test_actual_git_remote_parser_is_exact(monkeypatch,tmp_path):
    m=module()
    monkeypatch.setattr(m,'_git',lambda *args:'a'*40+'\trefs/heads/main')
    assert m._remote_main(tmp_path)=='a'*40
    monkeypatch.setattr(m,'_git',lambda *args:'a'*40+'\trefs/heads/main\n'+'b'*40+'\trefs/heads/main')
    with pytest.raises(ValueError):m._remote_main(tmp_path)


def test_append_route_requires_fixed_platform_workflow(setup,monkeypatch):
    m, _, args, _, invoke, _=setup
    args['route']='eleven_symbol_conditional_append'
    monkeypatch.setenv('GITHUB_WORKFLOW_REF','yuatom/stock-market-data/.github/workflows/eleven-symbol-append.yml@refs/heads/main')
    monkeypatch.setenv('GITHUB_WORKFLOW','Eleven Symbol Append')
    assert m.run_route(**args)=='PRIVATE_ROUTE_COMPLETE'
    assert invoke.call_args.kwargs['route']=='eleven_symbol_conditional_append'


def test_coordinator_failure_is_quiet_no_retry_and_restores_bytecode(setup,capfd):
    import sys
    m, _, args, _, invoke, _=setup
    old=sys.dont_write_bytecode
    def fail(**kwargs):
        print('PRIVATE');os.write(2,b'PRIVATE');raise RuntimeError('PRIVATE')
    invoke.side_effect=fail
    with pytest.raises(m.PrivateRouteError):m.run_route(**args)
    assert invoke.call_count==1
    assert capfd.readouterr()==('','')
    assert sys.dont_write_bytecode==old


def test_fresh_checkout_origin_and_gc_config_compatibility(setup):
    m, root, args, _, _, _=setup
    git(root,'remote','set-url','origin',m.COMPUTE_URL+'.git')
    git(root,'config','gc.auto','0')
    assert m.run_route(**args)=='PRIVATE_ROUTE_COMPLETE'


def test_append_wrong_workflow_name_stops_before_private_access(setup,monkeypatch):
    m, _, args, prepare, invoke, _=setup
    args['route']='eleven_symbol_conditional_append'
    monkeypatch.setenv('GITHUB_WORKFLOW_REF','yuatom/stock-market-data/.github/workflows/eleven-symbol-append.yml@refs/heads/main')
    monkeypatch.setenv('GITHUB_WORKFLOW','other')
    with pytest.raises(m.PrivateRouteError):m.run_route(**args)
    prepare.assert_not_called();invoke.assert_not_called()
