"""Manual SOXX evidence-only hosted route; no schedules or recovery authority.

Deployment requires fresh approval of the exact immutable private approval file
and dispatch digest. File existence alone is not user consent. This runtime does
not create approvals and never changes market history or accepts basis revisions.
"""
import argparse,hashlib,json,math,os,re,subprocess,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path
import admit_market_data_baseline as admission
from capture_private_response import _stamp
from capture_store_retention import retain_capture_object,_git as _retention_git,GIT_SAFETY
from raw_response_evidence import _load,validate_request
from rebase_market_data_baseline import capture_daily_response

REPOSITORY='yuatom/stock-market-data-store'
APPROVAL_KEYS=set('schema_version mode compute_sha baseline_store_sha execution_day request valid_from valid_until request_limit credit_limit evidence_repository evidence_repository_id evidence_prefix market_fact_write'.split())

class HostedCaptureError(RuntimeError):pass

def _git(repo,*args):
    return _retention_git(repo,'--no-replace-objects',*args)

def assert_immutable_object_view(repo):
    require(os.environ.get('GIT_REPLACE_REF_BASE') in (None,'refs/replace/'))
    require(not _git(repo,'for-each-ref','--format=%(refname)','refs/replace'))
    path=Path(_git(repo,'rev-parse','--git-path','info/grafts'))
    if not path.is_absolute():path=Path(repo)/path
    require(not os.path.lexists(path))

def encode(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode()

def require(condition):
    if not condition:raise ValueError('capture contract invalid')

def validate_repository(value):
    require(type(value) is dict and type(value.get('id')) is int and value['id']>0
        and value.get('full_name')==REPOSITORY and value.get('private') is True and value.get('default_branch')=='main')

def validate_surface(env,compute_sha,request_id):
    expected={'GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'workflow_dispatch',
        'GITHUB_REPOSITORY':'yuatom/stock-market-data','GITHUB_REF':'refs/heads/main',
        'GITHUB_SHA':compute_sha,'GITHUB_RUN_ATTEMPT':'1',
        'GITHUB_WORKFLOW_REF':'yuatom/stock-market-data/.github/workflows/soxx-capture-only.yml@refs/heads/main',
        'GITHUB_ACTOR':'yuatom','GITHUB_TRIGGERING_ACTOR':'yuatom'}
    require(all(env.get(k)==v for k,v in expected.items()))
    require(re.fullmatch('[0-9]+-1',request_id) and request_id==env.get('GITHUB_RUN_ID','')+'-1')

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('redirect forbidden')

def private_identity(token):
    request=urllib.request.Request('https://api.github.com/repos/'+REPOSITORY,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                 'User-Agent':'stock-market-data-capture'})
    with urllib.request.build_opener(_NoRedirect).open(request,timeout=30) as response:
        require(response.status==200 and response.geturl()=='https://api.github.com/repos/'+REPOSITORY)
        raw=response.read(65537)
    value=_load(raw,65536);validate_repository(value)
    return value

def approval_at_commit(store,commit,digest):
    require(re.fullmatch('[0-9a-f]{40}',commit) and re.fullmatch('[0-9a-f]{64}',digest))
    assert_immutable_object_view(store)
    relative=f'capture-approvals/{digest}.json'
    blob=_git(store,'rev-parse',f'{commit}:{relative}')
    require(_git(store,'ls-tree',commit,'--',relative)==f'100644 blob {blob}\t{relative}')
    result=subprocess.run(['git','-C',str(store),*GIT_SAFETY,'--no-replace-objects','cat-file','blob',blob],capture_output=True,timeout=30)
    require(result.returncode==0 and hashlib.sha256(result.stdout).hexdigest()==digest)
    value=_load(result.stdout,65536)
    require(type(value) is dict and set(value)==APPROVAL_KEYS and encode(value)==result.stdout)
    require(_git(store,'rev-list','--parents','-n','1',commit)==f"{commit} {value['baseline_store_sha']}")
    require(_git(store,'diff-tree','--no-commit-id','--name-status','-r',commit)==f'A\t{relative}')
    return value

def execute(*,compute,compute_sha,store,approval_commit,approval_sha256,request_id,h,config,access,key,
            store_token,repository_identity,clock=admission.utc_now,deadline=None):
    """Internal coordinator. Official CLI authenticates platform/repository first.

Tests exercise this coordinator with synthetic local Git. It is not itself a
platform/lock attestation. All real invocations must use the reviewed workflow.
"""
    try:
        deadline=deadline if deadline is not None else time.monotonic()+600
        require(type(deadline) in (int,float) and math.isfinite(deadline) and time.monotonic()<deadline)
        require(re.fullmatch('[0-9]+-1',request_id) and re.fullmatch('[0-9a-f]{40}',compute_sha))
        store=Path(store);compute=Path(compute)
        assert_immutable_object_view(store);assert_immutable_object_view(compute)
        require(_git(store,'rev-parse','HEAD')==approval_commit)
        require(_git(store,'ls-remote','origin','refs/heads/main').split()==[approval_commit,'refs/heads/main'])
        require(not _git(store,'status','--porcelain') and not _git(store,'ls-files','--others'))
        require(_git(compute,'rev-parse','HEAD')==compute_sha and not _git(compute,'status','--porcelain'))
        require(_git(compute,'ls-remote','origin','refs/heads/main').split()==[compute_sha,'refs/heads/main'])
        validate_repository(repository_identity)
        approval=approval_at_commit(store,approval_commit,approval_sha256)
        require(type(approval['schema_version']) is int and approval['schema_version']==1)
        require(approval['mode']=='soxx_capture_only' and approval['compute_sha']==compute_sha)
        require(approval['evidence_repository']==REPOSITORY and type(approval['evidence_repository_id']) is int
                and approval['evidence_repository_id']==repository_identity['id'] and approval['evidence_prefix']=='capture-evidence')
        require(approval['market_fact_write'] is False)
        for field in ('credit_limit','request_limit'):require(type(approval[field]) is int and approval[field]==1)
        request=validate_request(approval['request'])
        require(request['symbol']=='SOXX' and request['mic_code']=='XNMS')
        day=approval['execution_day']
        require(re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',day) and request['end_date']<day)
        for credential in (key,store_token):
            require(type(credential) is str and 8<=len(credential)<=4096 and all(33<=ord(c)<=126 for c in credential))
        spec=access['twelve_data_basic']
        require(spec['base_url']=='https://api.twelvedata.com' and spec['time_series_path']=='/time_series' and spec['interval_daily']=='1day')
        require(type(access['http']['timeout_seconds']) is int and 0<access['http']['timeout_seconds']<=120)
        maturity=config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
        start,end=_stamp(approval['valid_from']),_stamp(approval['valid_until'])
        def fresh():
            now=clock();require(start<=now<end and now.astimezone(admission.ET).date().isoformat()==day and time.monotonic()<deadline)
            admission.validate_historical_target(day,request['end_date'],maturity,now)
            return now
        fresh()
        head=approval_commit
        receipts=[]
        def retain(name,payload):
            nonlocal head
            receipt=retain_capture_object(repo=store,expected_commit=head,approval_sha256=approval_sha256,
                symbol='SOXX',name=name,payload=payload,execution_day=day,request_id=request_id,
                forbidden_bytes=(key.encode(),store_token.encode()))
            require(receipt['sha256']==hashlib.sha256(payload).hexdigest())
            head=receipt['store_commit_sha'];receipts.append(receipt)
            return receipt['sha256']
        for name,value in (
            ('claim.json',dict(schema_version=1,approval_sha256=approval_sha256,approval_commit=approval_commit,request_id=request_id,status='CONSUMED_NO_REPLAY')),
            ('request.json',request),
            ('attempt.json',dict(request_id=request_id,approval_sha256=approval_sha256,state='CLAIMED_BEFORE_DEBIT_NOT_PROOF_OF_HTTP'))):
            fresh();retain(name,encode(value))
        fresh()
        writer=admission.StorePublisher(store,head,day,request_id)
        debit=admission.debit_before_request(h,writer,store/'data/market-data',day,
            config['collector']['budgets']['twelve_data_basic'],clock=clock,
            deadline=min(deadline,time.monotonic()+(end-clock()).total_seconds()),
            maturity='00:00',compute=compute,compute_sha=compute_sha)  # Current ET accounting, cutoff guarded separately.
        head=debit.store_commit_sha
        def before_request():
            fresh()
            admission.guard_provider_request(writer,debit,compute,compute_sha,day,clock=clock,deadline=deadline)
            stamp=fresh().astimezone(timezone.utc)
            require(stamp.strftime('%Y-%m-%dT%H:%MZ')==debit.minute and stamp.second<58 and time.monotonic()<deadline)
        result=capture_daily_response(request=request,api_key=key,access=access,before_request=before_request,
            retain_raw=lambda raw:retain('response.raw',raw),retain_envelope=lambda raw:retain('envelope.json',raw),clock=clock)
        terminal=dict(result,schema_version=1,request_id=request_id,approval_sha256=approval_sha256,
            compute_sha=compute_sha,approval_commit=approval_commit,debit_commit_sha=debit.store_commit_sha,
            budget_blob_sha=debit.budget_blob_sha,budget_minute=debit.minute,receipts=receipts,
            production_activation=False,market_qualified=False)
        retain('terminal.json',encode(terminal))
        return dict(status='CAPTURE_RETAINED_UNQUALIFIED',requests_attempted=1,budget_debits=1,
            store_commit_sha=head,approval_sha256=approval_sha256,production_activation=False,
            market_qualified=False,raw_response_sha256=result['raw_response_sha256'])
    except Exception:
        pass
    raise HostedCaptureError('soxx_capture_stopped_inspect_before_retry')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('compute-sha','store-repo','approval-commit','approval-sha256','request-id'):parser.add_argument('--'+flag,required=True)
    args=parser.parse_args(argv);os.umask(0o077)
    try:
        validate_surface(os.environ,args.compute_sha,args.request_id)
        compute=Path(__file__).resolve().parents[1];store=Path(args.store_repo)
        require(_git(compute,'remote','get-url','origin')=='https://github.com/yuatom/stock-market-data')
        require(_git(store,'remote','get-url','origin') in ('https://github.com/'+REPOSITORY,'https://github.com/'+REPOSITORY+'.git'))
        token=os.environ.get('MARKET_DATA_STORE_TOKEN','');key=os.environ.get('TWELVE_DATA_API_KEY','')
        require(bool(token));identity=private_identity(token)
        h=admission.load_helpers()
        config=h.collectors.load_yaml(compute/'config/market-data-store.yaml')
        result=execute(compute=compute,compute_sha=args.compute_sha,store=store,approval_commit=args.approval_commit,
            approval_sha256=args.approval_sha256,request_id=args.request_id,h=h,config=config,
            access=h.collectors.load_yaml(compute/'config/market-data-collector-access.yaml'),key=key,store_token=token,repository_identity=identity)
        public={k:result[k] for k in ('status','requests_attempted','budget_debits','production_activation','market_qualified')}
        print(json.dumps(public,sort_keys=True));return 0
    except Exception:
        print(json.dumps({'mode':'soxx_capture_only','status':'BLOCKED','reason':'inspect_before_retry'},sort_keys=True));return 2

if __name__=='__main__':raise SystemExit(main())
