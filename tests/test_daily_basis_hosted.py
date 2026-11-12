"""Hosted glue tests use fabricated metadata and no provider/network access."""
from datetime import datetime,timezone
import copy
import importlib
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts import provider_basis_snapshot as B


def test_hosted_route_has_bounded_owner_and_private_grant_entrypoints():
    from scripts import run_daily_basis_route as H
    assert callable(H.run_route) and callable(H.validate_runtime_surface)


def test_existing_collector_delegates_only_soxx_and_keeps_other_symbols(tmp_path):
    import market_data_collectors as C
    config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':256}}}
    with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'synthetic-only','DAILY_BASIS_DELEGATION_FILE':'/synthetic/receipt'}),patch.object(C,'_daily_universe',return_value=[('SOXX','etf'),('OTHER','etf')]),patch.object(C,'_load_daily_basis_owner',return_value={'SOXX':'OWNED_IMMUTABLE_DAILY'}),patch.object(C,'wait_for_twelve_budget') as wait,patch.object(C,'consume_twelve_credit') as debit,patch.object(C,'fetch_twelve_daily',return_value=[]) as fetch:
        result=C.collect_previous_session_eod(trade_date='2026-11-10',store_root=tmp_path,watchlist={},completeness={},config=config,access={})
        assert fetch.call_count==wait.call_count==debit.call_count==1
        assert fetch.call_args.args[0]=='OTHER'
        assert result['delegated_daily_symbols']==[{'symbol':'SOXX','status':'OWNED_IMMUTABLE_DAILY'}]
        assert result['failures']==[{'symbol':'OTHER','reason':'no_eligible_previous_session_daily_rows'}]


def test_unverified_owner_file_cannot_bypass_legacy_fence(tmp_path):
    from scripts.daily_basis_delegation import load_daily_basis_owner
    f=tmp_path/'forged.json';f.write_bytes(B.encode({'status':'OWNED_IMMUTABLE_DAILY','symbol':'SOXX'}))
    with pytest.raises(Exception):load_daily_basis_owner(tmp_path,'2026-11-10',str(f),{})


def hosted_case(tmp_path):
    from scripts import run_daily_basis_route as H
    from scripts.daily_basis_store import LocalGitStore
    from test_daily_basis_updates import fixture,git,capture
    from scripts.emit_basis_consumer_delivery import encode_delivery
    repo,g,cal,old=fixture(tmp_path)
    class Store(LocalGitStore):
        def remote_ref(self,ref):return self.ref(ref)
        def materialize_ref(self,ref):
            oid=self.ref(ref);B._hex(oid);return oid
    store=Store(repo);now=datetime(2026,11,10,12,0,5,tzinfo=timezone.utc)
    gr=B.encode(g);cr=B.encode(cal)
    with store.lock():
        scope=store.publish(store.head(),{'basis-authority/grant.json':gr,'basis-authority/calendar.json':cr},replace=set())
        authority=dict(schema_version=1,kind='finite_daily_basis_authority',compute_sha='a'*40,consumer_sha='b'*40,workflow_source_sha='c'*40,grant_ref=dict(commit=scope,path='basis-authority/grant.json',blob_sha=B.blob_sha(gr),sha256=B.sha(gr)),calendar_ref=dict(commit=scope,path='basis-authority/calendar.json',blob_sha=B.blob_sha(cr),sha256=B.sha(cr)),nonce='e'*64,valid_from='2026-11-10T12:00:00Z',valid_until='2026-11-12T00:00:00Z',actions_free_until='2026-11-12T00:00:00Z',readiness_run_id=444,repository_id=11,owner_id=12,runtime_workflow_id=33,trigger_kind='schedule',schedule='5 7 * * 1-5',old_writers_drained=True)
        ar=B.encode(authority);commitment=H.authority_commitment(ar)
        ac=store.commit(scope,{H.AUTH_PATH:ar},replace=set())
        store.cas('refs/heads/basis-daily-authority/'+'a'*40+'/'+commitment,ac,'0'*40)
        store.cas('refs/heads/basis-consumer-delivery/'+'c'*40,ac,'0'*40)
    # The workflow ref source SHA is a fixture literal. Only its metadata read
    # is substituted; all capture/candidate/receipt/CAS operations use real Git.
    original=store.remote_ref
    store.remote_ref=lambda ref:'c'*40 if ref=='refs/heads/basis-consumer-delivery/'+'c'*40 else original(ref)
    env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=H.COMPUTE,GITHUB_SHA='a'*40,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='schedule',GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='collect',DAILY_BASIS_TRIGGER_KIND='schedule',DAILY_BASIS_SCHEDULE='5 7 * * 1-5',GITHUB_WORKFLOW_REF=H.COMPUTE+'/'+H.CALLER+'@refs/heads/main',GITHUB_RUN_ID='101')
    public_run=dict(id=101,workflow_id=33,path=H.CALLER,head_sha='a'*40,event='schedule',run_attempt=1,head_branch='main',status='in_progress',repository={'full_name':H.COMPUTE},head_repository={'full_name':H.COMPUTE},actor={'id':12},triggering_actor={'id':12})
    repository=dict(id=11,private=True,full_name=H.STORE,owner={'id':12})
    private_run=dict(id=555,workflow_id=14,path=H.VERIFIER,head_sha='c'*40,event='workflow_dispatch',run_attempt=1,head_branch='basis-consumer-delivery/'+'c'*40,status='completed',conclusion='success',repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},run_started_at=now.isoformat())
    jobs=dict(total_count=1,jobs=[dict(id=556,run_id=555,name='verify-basis-consumer',head_sha='c'*40,status='completed',conclusion='success',completed_at=now.isoformat(),steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
    class API:
        posts=[]
        def __init__(self):self.posts=[];self.uncertain=False
        def dispatch_capability(self,workflow_id):return workflow_id==14
        def request(self,method,path,payload=None,**kw):
            if method=='POST':
                self.posts.append((path,payload))
                if self.uncertain:raise TimeoutError('dispatch outcome uncertain')
                return dict(workflow_run_id=555,run_url='https://api.github.com/repos/'+H.STORE+'/actions/runs/555',html_url='https://github.com/'+H.STORE+'/actions/runs/555')
            if path=='/user':return {'id':12}
            if path=='/repos/'+H.STORE:return copy.deepcopy(repository)
            if path=='/repos/'+H.COMPUTE+'/actions/runs/101':return copy.deepcopy(public_run)
            if path=='/repos/'+H.STORE+'/actions/workflows/14':return {'id':14,'path':H.VERIFIER,'state':'active'}
            if path=='/repos/'+H.STORE+'/actions/runs/444':return copy.deepcopy(private_run)
            if '/contents/state/runtime-release.json' in path:
                import base64
                raw=B.encode({'repository_commit_sha':'b'*40});return dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=B.blob_sha(raw))
            if '/actions/workflows/14/runs?' in path:return dict(total_count=int(bool(self.posts)),workflow_runs=[copy.deepcopy(private_run)] if self.posts else [])
            if path.endswith('/actions/runs/555'):return copy.deepcopy(private_run)
            if path.endswith('/actions/runs/555/jobs?per_page=2'):return copy.deepcopy(jobs)
            raise AssertionError(path)
        def job_log(self,job_id):
            commitment=self.posts[0][1]['inputs']['receipt_commitment'];ref=f'refs/heads/basis-read-receipts/{"a"*40}/{commitment}';rc=store.ref(ref);raw=store.read(rc,'basis-authority/receipt.json');receipt=B._parse(raw)
            context=dict(repository=H.STORE,repository_id=11,owner_id=12,private=True,workflow_source_sha='c'*40,run_id=555,run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path=H.VERIFIER,job='verify-basis-consumer')
            v=dict(schema_version=1,kind='private_basis_consumer_delivery',compute_sha='a'*40,receipt_commitment=commitment,context=context,receipt_ref=ref,receipt_commit=rc,receipt_blob_sha=B.blob_sha(raw),selector=receipt['selector'],verified_at=now.isoformat(),valid_until=receipt['valid_until'])
            return dict(job_id=job_id,text=encode_delivery(v))
    return H,store,API(),env,authority,public_run,repository,commitment,now,capture


def test_full_scheduled_owner_and_read_only_current_delivery(tmp_path):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path);calls=[];source=[]
    output=tmp_path/'owner.json'
    result=H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date='2026-11-10',env=env,key='unused',access={},delegation_output=output,clock=lambda:now,capture=capture(calls=calls),source_check=lambda:source.append(True))
    assert result=='FINITE_DAILY_BASIS_COMPLETE' and len(calls)==len(api.posts)==1 and len(source)==3
    owner=B._parse(output.read_bytes());assert owner['status']=='OWNED_IMMUTABLE_DAILY'
    current=H.current_delivery(store=store,api=api,compute_sha='a'*40,commitment=commitment,clock=lambda:now)
    assert current['selector']==owner['selector'] and len(current['view']['records'])==5
    assert len(calls)==len(api.posts)==1


@pytest.mark.parametrize('attack',['retry','actor','event','schedule','source','private','dispatch_uncertain'])
def test_hosted_route_stops_scope_or_uncertain_dispatch_without_new_request(tmp_path,attack):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path);calls=[]
    if attack=='retry':env['GITHUB_RUN_ATTEMPT']='2'
    if attack=='actor':run['actor']['id']=99
    if attack=='event':run['event']='workflow_dispatch'
    if attack=='schedule':env['DAILY_BASIS_SCHEDULE']='* * * * *'
    if attack=='source':run['head_sha']='d'*40
    if attack=='private':r['private']=False
    if attack=='dispatch_uncertain':api.uncertain=True
    with pytest.raises(H.HostedDailyError):H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date='2026-11-10',env=env,key='unused',access={},delegation_output=tmp_path/'owner.json',clock=lambda:now,capture=capture(calls=calls))
    assert len(calls)==(1 if attack=='dispatch_uncertain' else 0)
    assert len(api.posts)==(1 if attack=='dispatch_uncertain' else 0)
    assert not (tmp_path/'owner.json').exists()


def test_shared_workflow_runs_daily_owner_before_legacy_collection():
    text=(Path(__file__).parents[1]/'.github/workflows/market-data-collector-runtime.yml').read_text()
    assert 'group: market-data-collector' in text
    assert text.index('name: Run finite daily basis owner')<text.index('name: Collect qualified facts')
    assert '-m scripts.run_daily_basis_route' in text
    assert 'DAILY_BASIS_DELEGATION_FILE:' in text
    assert 'DAILY_BASIS_GRANT_COMMITMENT:' in text
    assert 'name: Materialize verified daily Store base' in text


def test_real_module_cli_imports_without_test_only_scripts_pythonpath():
    import subprocess,sys
    root=Path(__file__).parents[1]
    env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
    result=subprocess.run([sys.executable,'-B','-m','scripts.run_daily_basis_route','--help'],cwd=root,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert '--delegation-output' in result.stdout


def test_private_workflow_never_exposes_store_pins_or_checkout_logs():
    source=(Path(__file__).parents[1]/'.github/workflows/market-data-collector-runtime.yml').read_text()
    assert 'python -B -m scripts.quiet_private_store clone' in source
    assert "fh.write(f'market_data_store_input_sha=" not in source
    assert "'market_data_store_input_sha': store_sha" not in source
    assert 'MARKET_DATA_STORE_INPUT_SHA: ${{' not in source
    assert 'python -B ../compute/scripts/quiet_private_store.py run' in source


def test_quiet_private_git_env_never_stores_or_prints_credentials():
    from scripts.quiet_private_store import git_environment
    env=git_environment('synthetic-not-a-live-token')
    assert env['GIT_CONFIG_GLOBAL']=='/dev/null'
    assert 'synthetic-not-a-live-token' not in repr({k:v for k,v in env.items() if not k.startswith('GIT_CONFIG_VALUE_')})
    assert any('stock-market-data-store.git.extraheader' in v for k,v in env.items() if k.startswith('GIT_CONFIG_KEY_'))


def test_predebit_wait_never_debits_or_fetches_while_minute_is_full(tmp_path):
    from scripts.run_daily_basis_route import wait_for_daily_budget_window
    from test_daily_basis_updates import setup
    D,repo,g,c,paths,store,current,u=setup(tmp_path)
    current[0]=datetime(2026,11,10,12,0,45,tzinfo=timezone.utc)
    sleeps=[]
    def sleep(seconds):
        sleeps.append(seconds);current[0]=datetime(2026,11,10,12,1,1,tzinfo=timezone.utc)
    with store.lock():
        head=store.head()
        wait_for_daily_budget_window(u,u._slot('nov10'),clock=lambda:current[0],sleep=sleep)
        assert store.head()==head and len(sleeps)==1


@pytest.mark.parametrize('when,day,status',[(datetime(2026,11,12,12,0,5,tzinfo=timezone.utc),'2026-11-12','ACQUISITION_HELD_GRANT_EXHAUSTED'),(datetime(2026,11,11,12,0,5,tzinfo=timezone.utc),'2026-11-11','ACQUISITION_HELD_REVIEW_REQUIRED')])
def test_known_finite_end_or_missed_slot_holds_only_soxx_without_capture(tmp_path,when,day,status):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path);calls=[];output=tmp_path/'owner.json'
    result=H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date=day,env=env,key='unused',access={},delegation_output=output,clock=lambda:when,capture=capture(calls=calls))
    assert result=='FINITE_DAILY_BASIS_HELD' and not calls and not api.posts
    assert B._parse(output.read_bytes())['status']==status


def test_unproven_current_token_dispatch_scope_stops_before_provider(tmp_path):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path);calls=[]
    api.dispatch_capability=lambda workflow_id:False
    with pytest.raises(H.HostedDailyError):H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date='2026-11-10',env=env,key='unused',access={},delegation_output=tmp_path/'owner.json',clock=lambda:now,capture=capture(calls=calls))
    assert not calls and not api.posts


@pytest.mark.parametrize('scope,allowed',[('repo, read:user',True),('workflow',False),('',False)])
def test_dispatch_capability_comes_from_authenticated_current_token_headers(scope,allowed):
    from scripts.run_daily_basis_route import GitHubAPI
    api=GitHubAPI('synthetic-not-live-token');seen=[]
    class Response:
        status=200
        headers={'X-OAuth-Scopes':scope}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def geturl(self):return 'https://api.github.com/repos/yuatom/stock-market-data-store/actions/workflows/14'
        def read(self,bound):return b'{"id":14,"state":"active"}'
    class Opener:
        def open(self,request,timeout):seen.append(request);return Response()
    api.opener=Opener()
    assert api.dispatch_capability(14) is allowed
    assert seen[0].get_header('X-github-api-version')=='2026-03-10'


def test_known_missing_optional_authority_holds_without_disabling_other_collectors(tmp_path):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path);calls=[]
    original=store.remote_ref
    store.remote_ref=lambda ref:None if '/basis-daily-authority/' in ref else original(ref)
    output=tmp_path/'owner.json'
    result=H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date='2026-11-10',env=env,key='unused',access={},delegation_output=output,clock=lambda:now,capture=capture(calls=calls))
    assert result=='FINITE_DAILY_BASIS_HELD' and not calls and not api.posts
    assert B._parse(output.read_bytes())['status']=='ACQUISITION_HELD_AUTHORITY_UNAVAILABLE'


def test_delivery_mapping_must_be_exact_child_of_candidate(tmp_path):
    H,store,api,env,a,run,r,commitment,now,capture=hosted_case(tmp_path)
    from test_daily_basis_updates import git
    H.run_route(store=store,api=api,compute=tmp_path,compute_sha='a'*40,commitment=commitment,trade_date='2026-11-10',env=env,key='unused',access={},delegation_output=tmp_path/'owner.json',clock=lambda:now,capture=capture())
    ref='refs/heads/basis-daily-deliveries/'+B.sha(B.encode(__import__('json').loads(store.read(a['grant_ref']['commit'],a['grant_ref']['path']))))+'/nov10'
    old=store.ref(ref);tree=git(store.repo,'rev-parse',old+'^{tree}')
    root=git(store.repo,'commit-tree',tree,'-m','forged unrelated mapping root');git(store.repo,'update-ref',ref,root,old)
    with pytest.raises(Exception):H.current_delivery(store=store,api=api,compute_sha='a'*40,commitment=commitment,clock=lambda:now)


def test_actual_workflow_refresh_quiets_failing_private_argv_and_preserves_new_base():
    import subprocess,tempfile,pathlib,os,json,hashlib,re
    import yaml
    source=pathlib.Path(__file__).resolve().parents[1]
    workflow=source/'.github/workflows/market-data-collector-runtime.yml'
    data=yaml.safe_load(workflow.read_text());steps=data['jobs']['collect']['steps']
    selected=[s for s in steps if s.get('name')=='Materialize verified daily Store base'];assert len(selected)==1
    names=[s.get('name') for s in steps]
    # Substitute only the Git transport allowlist for this local-bare test.
    script=selected[0]['run'].replace("private_env=git_environment(os.environ['MARKET_DATA_STORE_TOKEN'])","private_env=git_environment(os.environ['MARKET_DATA_STORE_TOKEN']);private_env['GIT_ALLOW_PROTOCOL']='file'")
    assert names.index('Run finite daily basis owner')<names.index(selected[0]['name'])<names.index('Collect qualified facts into private Store checkout')
    def git(p,*args):
        return subprocess.check_output(['git','-C',str(p),'-c','user.name=Test','-c','user.email=test@example.invalid',*args],stderr=subprocess.DEVNULL).decode().strip()
    with tempfile.TemporaryDirectory() as temp:
        p=pathlib.Path(temp);remote=p/'remote.git';author=p/'author';w=p/'workspace';w.mkdir();author.mkdir();(w/'compute').symlink_to(source,target_is_directory=True);(w/'tmp').mkdir()
        subprocess.run(['git','init','-q','--bare',str(remote)],check=True)
        git(author,'init','-qb','main');git(author,'remote','add','origin',str(remote))
        (author/'other-symbol.json').write_text('unchanged prior other symbol\n');git(author,'add','.');git(author,'commit','-qm','before daily');git(author,'push','-q','origin','main')
        git(remote,'symbolic-ref','HEAD','refs/heads/main');git(w,'clone','-q',str(remote),'store')
        before=git(w/'store','rev-parse','HEAD')
        (author/'daily-immutable.json').write_text('new daily candidate\n');git(author,'add','.');git(author,'commit','-qm','synthetic daily promotion');git(author,'push','-q','origin','main');after=git(author,'rev-parse','HEAD')
        receipt=w/'owner.json';receipt.write_text(json.dumps({'store_sha':after}));receipt.chmod(0o600)
        env={k:v for k,v in os.environ.items() if 'TOKEN' not in k and 'KEY' not in k and not k.startswith('GIT_')};env.update(OWNER_FILE=str(receipt),RUNNER_TEMP=str(w/'tmp'),MARKET_DATA_STORE_TOKEN='synthetic-only')
        r=subprocess.run(['bash','-e','-c',script],cwd=w,env=env,capture_output=True,text=True)
        assert r.returncode==0,(r.stdout,r.stderr)
        assert r.stdout=='' and before not in r.stderr and after not in r.stderr
        assert git(w/'store','rev-parse','HEAD')==after
        assert (w/'tmp/market-data-store-input-sha').read_text().strip()==after
        assert ((w/'tmp/market-data-store-input-sha').stat().st_mode&0o777)==0o600
        assert (w/'store/other-symbol.json').read_text()=='unchanged prior other symbol\n'
        assert (w/'store/daily-immutable.json').read_text()=='new daily candidate\n'
        injected=re.sub(r'(?m)^([ ]*)def git\(\*args\):',lambda m:m[0]+'\n'+m[1]+"    if args[0]=='checkout':raise subprocess.CalledProcessError(128,['git','PRIVATE_PIN_SENTINEL',*args])",script)
        error=subprocess.run(['bash','-e','-c',injected],cwd=w,env=env,capture_output=True,text=True)
        assert error.returncode!=0
        assert after not in error.stdout+error.stderr and 'PRIVATE_PIN_SENTINEL' not in error.stdout+error.stderr,'PRIVATE_SHA_LEAKED_IN_REFRESH_EXCEPTION'
        assert error.stdout.strip()=='PRIVATE_STORE_BASE_REFRESH_STOPPED' and error.stderr==''
        # Ordinary subsequent collection starts from the new exact main and retains
        # the daily object, instead of attempting to publish from the stale clone.
        (w/'store/other-symbol.json').write_text('fresh ordinary other symbol\n');git(w/'store','add','.');git(w/'store','commit','-qm','ordinary collection');git(w/'store','push','-q','origin','HEAD:main')
        assert git(remote,'show','main:daily-immutable.json')=='new daily candidate'
        # A known unrelated movement aborts without printing either private SHA.
        (author/'moved.json').write_text('concurrent\n');git(author,'fetch','-q','origin');git(author,'reset','--hard','origin/main');(author/'moved.json').write_text('concurrent\n');git(author,'add','.');git(author,'commit','-qm','unrelated movement');git(author,'push','-q','origin','main')
        r=subprocess.run(['bash','-e','-c',script],cwd=w,env=env,capture_output=True,text=True)
        assert r.returncode!=0 and before not in r.stdout+r.stderr and after not in r.stdout+r.stderr
