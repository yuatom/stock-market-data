"""Isolated owner-authorized single private delivery probe. No provider or Store writer."""
import argparse,json,os,re,time,urllib.error
from datetime import datetime,timezone,timedelta
from pathlib import Path
from scripts import provider_basis_snapshot as B
from scripts.run_daily_basis_route import GitHubAPI,basis_actions_token
from scripts.run_private_authority_route import _quiet,_source,_environment
from scripts.emit_basis_consumer_delivery import materialize_local_receipt_ref
from scripts.daily_basis_delivery import DeliveryExpectation,_verify_private_delivery,WORKFLOW as PRIVATE_WORKFLOW
COMPUTE='yuatom/stock-market-data';STORE='yuatom/stock-market-data-store'
WORKFLOW='.github/workflows/basis-actions-readiness-probe.yml'
OWNER=10399225;STORE_ID=1335101677;WORKFLOW_ID=415302427
SOURCE='cb7c3302d131278f5564eb702ce29ba4ef8ee08d'
BASIS='812eefd58ef3cc569af6fc53fb6c1c54364234f0f1fa21d5c806bc8e385c9433'
LABELS={'self-hosted','macOS','ARM64','soxx-private-once-20261114'}
STAGES={'INPUT','SOURCE','CALLER','RECEIPT','PRIVATE_TARGET','DISPATCH','POLL','DELIVERY'}
INPUT_DETAILS={'UNSPECIFIED','PIN_PARAMETERS','TIME_WINDOW','RUNNER_PARAMETERS','CREDENTIAL_SELECTION'}
STOP='BASIS_ACTIONS_READINESS_STOPPED_NO_RETRY'
class ProbeFailure(RuntimeError):
    def __init__(self,stage,error,run_id=None,detail='UNSPECIFIED'):
        self.stage=stage if stage in STAGES else 'INPUT'
        self.detail=detail if detail in INPUT_DETAILS else 'UNSPECIFIED'
        self.http_status=error.code if isinstance(error,urllib.error.HTTPError) and type(error.code) is int and 100<=error.code<=599 else None
        self.run_id=run_id if type(run_id) is int and run_id>0 else None
        super().__init__(STOP)
def need(value):
    if not value:raise ValueError(STOP)
def receipt_view(store,compute,commitment,now):
    materialize_local_receipt_ref(store,commitment,compute)
    selector,authority=B.load_consumer_receipt(store,commitment,compute,now=now.isoformat())
    need(selector['symbol']=='SOXX' and selector['provider']=='twelve_data_basic' and selector['basis_id']==BASIS)
    g=B._store(store);ref=f'refs/heads/basis-read-receipts/{compute}/{commitment}'
    commit=g.text('rev-parse',ref);blob=g.normal_blob_identity(commit,'basis-authority/receipt.json')
    receipt=B._receipt(g.object(blob,'blob',B.MAX_BYTES))
    snap=B._validate_snapshot(B._parse(B._blob(g,selector['read_sha'],B.snapshot_path(selector['provider'],'SOXX',BASIS),selector['snapshot_blob_sha'])),BASIS)
    dates=[r['datetime'] for r in snap['records']]
    view=B.read_price_view(store,selector,authority,fields=B.PRICE_FIELDS,required_sessions=dates,knowledge_cutoff=now.isoformat())
    need(view['record_count']==318 and view['last_trade_date']=='2026-11-11' and view['volume_status']=='BLOCKED_UNRESOLVED')
    return dict(selector=selector,receipt_ref=ref,receipt_commit=commit,receipt_blob_sha=blob,valid_until=receipt['valid_until'])
def check_run(run,run_id):
    expected=dict(id=run_id,workflow_id=WORKFLOW_ID,head_sha=SOURCE,path=PRIVATE_WORKFLOW,event='workflow_dispatch',run_attempt=1,head_branch='basis-consumer-delivery/'+SOURCE)
    need(all(run.get(k)==v for k,v in expected.items()))
    need(run['repository']['id']==run['head_repository']['id']==STORE_ID and run['actor']['id']==run['triggering_actor']['id']==OWNER)
def probe(*,api,store,compute,commitment,runner_id,runner_name,expires,env,source_check,clock=lambda:datetime.now(timezone.utc),sleep=time.sleep,monotonic=time.monotonic):
    stage='INPUT';run_id=None;detail='PIN_PARAMETERS'
    try:
        B._hex(compute);B._hex(commitment,64)
        detail='TIME_WINDOW';until=B._stamp(expires);now=clock()
        need(now<until<=now+timedelta(minutes=10))
        detail='RUNNER_PARAMETERS'
        need(type(runner_id) is int and runner_id>0 and type(runner_name) is str and re.fullmatch('[A-Za-z0-9_.-]{1,128}',runner_name))
        stage='SOURCE';source_check()
        stage='CALLER'
        required=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_REPOSITORY=COMPUTE,GITHUB_REF='refs/heads/main',GITHUB_SHA=compute,GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='probe',GITHUB_WORKFLOW_REF=COMPUTE+'/'+WORKFLOW+'@refs/heads/main')
        need(all(env.get(k)==v for k,v in required.items()) and re.fullmatch('[1-9][0-9]*',env.get('GITHUB_RUN_ID','')))
        repo=api.request('GET','/repos/'+COMPUTE);caller=api.request('GET','/repos/'+COMPUTE+'/actions/runs/'+env['GITHUB_RUN_ID'])
        need(repo['full_name']==COMPUTE and repo['owner']['id']==OWNER and caller['repository']['id']==caller['head_repository']['id']==repo['id'])
        need(all(caller.get(k)==v for k,v in dict(id=int(env['GITHUB_RUN_ID']),head_sha=compute,path=WORKFLOW,event='workflow_dispatch',run_attempt=1,head_branch='main',status='in_progress').items()))
        need(caller['actor']['id']==caller['triggering_actor']['id']==OWNER and api.request('GET','/user')['id']==OWNER)
        stage='RECEIPT';evidence=receipt_view(store,compute,commitment,clock());need(until<=B._stamp(evidence['valid_until']))
        stage='PRIVATE_TARGET';repository=api.request('GET','/repos/'+STORE)
        need(repository['id']==STORE_ID and repository['owner']['id']==OWNER and repository['full_name']==STORE and repository['private'] is True)
        workflow=api.request('GET',f'/repos/{STORE}/actions/workflows/{WORKFLOW_ID}')
        need(workflow['id']==WORKFLOW_ID and workflow['path']==PRIVATE_WORKFLOW and workflow['state']=='active')
        ref=api.request('GET',f'/repos/{STORE}/git/ref/heads/basis-consumer-delivery/{SOURCE}')
        need(ref['ref']=='refs/heads/basis-consumer-delivery/'+SOURCE and ref['object']['type']=='commit' and ref['object']['sha']==SOURCE)
        # Runner inventory is verified by the approved external controller.
        # This job needs no Administration permission; exact runner is checked
        # against the authenticated completed job below.
        stage='SOURCE';source_check()
        stage='DISPATCH';need(clock()<until)
        # Exactly one POST. No exception path or poll can return to this statement.
        reply=api.request('POST',f'/repos/{STORE}/actions/workflows/{WORKFLOW_ID}/dispatches',{'ref':'basis-consumer-delivery/'+SOURCE,'inputs':{'compute_sha':compute,'receipt_commitment':commitment}})
        B._closed(reply,('workflow_run_id','run_url','html_url'));run_id=reply['workflow_run_id'];need(type(run_id) is int and run_id>0)
        need(reply['run_url']==f'https://api.github.com/repos/{STORE}/actions/runs/{run_id}' and reply['html_url']==f'https://github.com/{STORE}/actions/runs/{run_id}')
        stage='POLL';deadline=monotonic()+180
        # Existing API has a 30-second per-read timeout; do not start a read
        # whose allowed duration could exceed the finite polling deadline.
        while monotonic()+30<=deadline and clock()+timedelta(seconds=30)<until:
            run=api.request('GET',f'/repos/{STORE}/actions/runs/{run_id}');check_run(run,run_id)
            if run['status']=='completed':break
            need(run['status'] in ('queued','in_progress','waiting','pending','requested'));sleep(min(3,max(0,deadline-monotonic()),max(0,(until-clock()).total_seconds())))
        else:raise TimeoutError()
        need(run.get('conclusion')=='success' and clock()<until)
        stage='DELIVERY';jobs=api.request('GET',f'/repos/{STORE}/actions/runs/{run_id}/jobs?per_page=2')
        need(jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1);job=jobs['jobs'][0]
        need(job['runner_id']==runner_id and job['runner_name']==runner_name and set(job['labels'])==LABELS)
        log=api.job_log(job['id'])
        value=_verify_private_delivery(DeliveryExpectation(STORE_ID,OWNER,WORKFLOW_ID,SOURCE,compute,commitment,run_id),repository,run,jobs,log,now=clock().isoformat(),calendar_authority={'readiness_probe':True})
        need(clock()<until and all(value[k]==v for k,v in evidence.items()))
        return dict(status='BASIS_ACTIONS_READINESS_VERIFIED',run_id=run_id,provider_requests=0,execution_performed=False,automatic_retry_allowed=False)
    except Exception as exc:raise ProbeFailure(stage,exc,run_id,detail if stage=='INPUT' else 'UNSPECIFIED') from None

def main():
    p=argparse.ArgumentParser()
    for k in ('store','compute-sha','receipt-commitment','expected-runner-name','expires'):p.add_argument('--'+k,required=True)
    p.add_argument('--expected-runner-id',type=int,required=True);a=p.parse_args()
    try:
        compute=Path(__file__).resolve().parents[1]
        with _quiet():
            def source_check():
                with _environment():_source(compute,a.compute_sha)
            try:api=GitHubAPI(basis_actions_token(os.environ))
            except Exception as exc:raise ProbeFailure('INPUT',exc,detail='CREDENTIAL_SELECTION') from None
            result=probe(api=api,store=Path(a.store),compute=a.compute_sha,commitment=a.receipt_commitment,runner_id=a.expected_runner_id,runner_name=a.expected_runner_name,expires=a.expires,env=dict(os.environ),source_check=source_check)
        print(json.dumps(result));return 0
    except Exception as exc:
        safe=exc if isinstance(exc,ProbeFailure) else ProbeFailure('INPUT',exc)
        print(json.dumps(dict(status=STOP,failure_stage=safe.stage,failure_detail=safe.detail if safe.detail in INPUT_DETAILS else 'UNSPECIFIED',http_status=safe.http_status,run_id=safe.run_id,provider_requests=0,automatic_retry_allowed=False)));return 2
if __name__=='__main__':raise SystemExit(main())
