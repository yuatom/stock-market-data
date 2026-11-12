"""Approved finite scheduled owner. No self-authorizing defaults or new secrets.

The only public activation setting is an independently approved salted opaque
commitment. Its distinct private authority ref contains exact finite grant,
calendar, source and readiness pins. This executable is inert without that
setting. A configured but invalid route fails closed; it never falls back.
All production calls require the reviewed shared collector workflow. Local tests
inject transport/API doubles and synthetic Git repositories, never credentials.
"""
import argparse
import base64
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_updates import DailyUpdater,GrantAuthority,Candidate,provider_capture
from scripts.daily_basis_delivery import verify_daily_delivery
from scripts.run_private_authority_route import _quiet,_source,_environment,SAFETY

STORE='yuatom/stock-market-data-store'
COMPUTE='yuatom/stock-market-data'
AUTH_PATH='basis-authority/daily-authority.json'
DOMAIN=b'finite-daily-basis-authority:v1\0'
CALLER='.github/workflows/market-data-collector.yml'
VERIFIER='.github/workflows/basis-consumer-delivery.yml'
STOP='FINITE_DAILY_BASIS_STOPPED_INSPECT_NO_RETRY'


class HostedDailyError(RuntimeError):pass
class ScopeUnavailable(HostedDailyError):pass


def need(value):
    if not value:raise HostedDailyError(STOP)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class GitHubAPI:
    """Fixed GitHub API, bounded responses, no retries, no ambient proxy."""
    def __init__(self,token):
        need(type(token) is str and re.fullmatch('[A-Za-z0-9_.-]{8,4096}',token));self._token=token
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())

    def request(self,method,path,payload=None,*,raw=False):
        need(method in ('GET','POST') and path.startswith('/repos/') or method=='GET' and path=='/user')
        need('..' not in path and '\\' not in path)
        url='https://api.github.com'+path
        req=urllib.request.Request(url,method=method,data=B.encode(payload) if payload is not None else None,
            headers={'Authorization':'Bearer '+self._token,'Accept':'application/vnd.github+json',
            'X-GitHub-Api-Version':'2026-03-10','User-Agent':'finite-daily-basis','Content-Type':'application/json'})
        with self.opener.open(req,timeout=30) as response:
            need(response.geturl()==url and response.status==200)
            self._last_oauth_scopes=response.headers.get('X-OAuth-Scopes','')
            data=response.read(1024*1024+1)
        need(len(data)<=1024*1024)
        return data if raw else B._parse(data,canonical=False)

    def dispatch_capability(self,workflow_id):
        """Actual current-token scope evidence; no inference from past actors.

        GitHub documents classic/OAuth `repo` for private workflow dispatch.
        Fine-grained tokens need Actions(write), which this read API cannot
        introspect reliably: absent classic scope stops BEFORE provider debit.
        """
        self.request('GET','/repos/'+STORE+'/actions/workflows/'+str(workflow_id))
        scopes=self._last_oauth_scopes
        return type(scopes) is str and 'repo' in {part.strip() for part in scopes.split(',')}

    def job_log(self,job_id):
        need(type(job_id) is int and job_id>0)
        target='https://api.github.com/repos/'+STORE+'/actions/jobs/'+str(job_id)+'/logs'
        request=urllib.request.Request(target,headers={'Authorization':'Bearer '+self._token,
            'Accept':'application/vnd.github+json','User-Agent':'finite-daily-basis'})
        try:
            with self.opener.open(request,timeout=30) as response:
                need(response.status==200 and response.geturl()==target);raw=response.read(1024*1024+1)
        except urllib.error.HTTPError as error:
            need(error.code==302 and error.geturl()==target)
            location=error.headers.get('Location','');u=urllib.parse.urlsplit(location)
            # Signed Actions log storage only. NEVER forward the GitHub bearer.
            need(u.scheme=='https' and not u.username and not u.password and not u.fragment and
                 re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net',u.hostname or ''))
            with self.opener.open(urllib.request.Request(location,headers={'User-Agent':'finite-daily-basis'}),timeout=30) as response:
                need(response.status==200 and response.geturl()==location);raw=response.read(1024*1024+1)
        need(0<len(raw)<=1024*1024)
        return dict(job_id=job_id,text=raw.decode('utf-8'))


def authority_commitment(raw):return B.sha(DOMAIN+raw)


def validate_authority_receipt(raw,commitment,compute_sha,*,now,allow_expired_hold=False):
    value=B._parse(raw)
    B._closed(value,('schema_version','kind','compute_sha','consumer_sha','workflow_source_sha','grant_ref',
        'calendar_ref','nonce','valid_from','valid_until','actions_free_until','readiness_run_id',
        'repository_id','owner_id','runtime_workflow_id','trigger_kind','schedule','old_writers_drained'))
    need(value['schema_version']==1 and value['kind']=='finite_daily_basis_authority' and
         authority_commitment(raw)==B._hex(commitment,64) and value['compute_sha']==B._hex(compute_sha))
    for key in ('consumer_sha','workflow_source_sha'):B._hex(value[key])
    B._hex(value['nonce'],64);need(value['nonce']!='0'*64)
    need(value['trigger_kind']=='schedule' and value['schedule']=='5 7 * * 1-5' and value['old_writers_drained'] is True)
    for key in ('readiness_run_id','repository_id','owner_id','runtime_workflow_id'):need(type(value[key]) is int and value[key]>0)
    instant=B._stamp(now)
    need(type(allow_expired_hold) is bool and B._stamp(value['valid_from'])<=instant and B._stamp(value['valid_from'])<B._stamp(value['valid_until'])<=B._stamp(value['actions_free_until']))
    need(instant<B._stamp(value['valid_until']) or allow_expired_hold)
    for key in ('grant_ref','calendar_ref'):
        ref=value[key];B._closed(ref,('commit','path','blob_sha','sha256'))
        B._hex(ref['commit']);B._hex(ref['blob_sha']);B._hex(ref['sha256'],64)
        need(ref['path'].startswith('basis-authority/'))
    return value


def validate_runtime_surface(env,authority,run,*,compute_sha,trade_date):
    required=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=COMPUTE,GITHUB_SHA=compute_sha,
        GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='schedule',GITHUB_RUN_ATTEMPT='1',
        GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='collect',
        DAILY_BASIS_TRIGGER_KIND='schedule',DAILY_BASIS_SCHEDULE='5 7 * * 1-5')
    need(all(env.get(k)==v for k,v in required.items()))
    need(env.get('GITHUB_WORKFLOW_REF')==COMPUTE+'/'+CALLER+'@refs/heads/main')
    run_id=env.get('GITHUB_RUN_ID','');need(re.fullmatch('[1-9][0-9]*',run_id))
    need(run.get('id')==int(run_id) and run.get('workflow_id')==authority['runtime_workflow_id'] and
         run.get('path')==CALLER and run.get('head_sha')==compute_sha and run.get('event')=='schedule' and
         run.get('run_attempt')==1 and run.get('head_branch')=='main' and run.get('status')=='in_progress')
    need(run['repository']['full_name']==COMPUTE and run['head_repository']['full_name']==COMPUTE)
    need(run['actor']['id']==run['triggering_actor']['id']==authority['owner_id'])
    B._day(trade_date)


def read_authority(store,commitment,compute_sha,*,now,allow_expired_hold=False):
    try:ref=f'refs/heads/basis-daily-authority/{B._hex(compute_sha)}/{B._hex(commitment,64)}'
    except Exception:raise ScopeUnavailable('DAILY_SCOPE_UNAVAILABLE') from None
    if store.remote_ref(ref) is None:raise ScopeUnavailable('DAILY_SCOPE_UNAVAILABLE')
    commit=store.materialize_ref(ref)
    blob=store.g.normal_blob_identity(commit,AUTH_PATH);raw=store.read(commit,AUTH_PATH)
    try:authority=validate_authority_receipt(raw,commitment,compute_sha,now=now,allow_expired_hold=allow_expired_hold)
    except (B.BasisError,HostedDailyError,ValueError,KeyError):raise ScopeUnavailable('DAILY_SCOPE_UNAVAILABLE') from None
    loaded=[]
    for field in ('grant_ref','calendar_ref'):
        r=authority[field]
        # Scope provisioning must retain the exact referenced commits as
        # ancestors of the authority branch, not unspecified loose objects.
        need(store.g.text('merge-base',r['commit'],commit)==r['commit'])
        data=B._blob(store.g,r['commit'],r['path'],r['blob_sha']);need(B.sha(data)==r['sha256']);loaded.append(data)
    return authority,loaded[0],loaded[1],dict(ref=ref,commit=commit,blob_sha=blob,commitment=commitment)


def check_private_readiness(api,store,authority,grant,*,now):
    repository=api.request('GET','/repos/'+STORE)
    need(repository.get('private') is True and repository.get('id')==grant['repository_id']==authority['repository_id'] and
         repository.get('full_name')==STORE and repository['owner']['id']==grant['owner_id']==authority['owner_id'])
    # The authenticated credential must already be the approved owner identity.
    # This does not expand scopes. A fine-grained token lacking dispatch access
    # remains a deployment blocker, not a reason to provision a new credential.
    need(api.request('GET','/user')['id']==grant['owner_id'])
    need(api.dispatch_capability(grant['workflow_id']))
    workflow=api.request('GET','/repos/'+STORE+'/actions/workflows/'+str(grant['workflow_id']))
    need(workflow.get('id')==grant['workflow_id'] and workflow.get('path')==VERIFIER and workflow.get('state')=='active')
    need(store.remote_ref('refs/heads/basis-consumer-delivery/'+grant['workflow_source_sha'])==grant['workflow_source_sha'])
    ready=api.request('GET','/repos/'+STORE+'/actions/runs/'+str(authority['readiness_run_id']))
    need(ready.get('workflow_id')==grant['workflow_id'] and ready.get('head_sha')==grant['workflow_source_sha'] and
         ready.get('path')==VERIFIER and ready.get('status')=='completed' and ready.get('conclusion')=='success' and
         ready.get('run_attempt')==1 and ready.get('event')=='workflow_dispatch' and
         ready['actor']['id']==ready['triggering_actor']['id']==grant['owner_id'])
    release=api.request('GET','/repos/yuatom/stock-dairy/contents/state/runtime-release.json?ref=main')
    need(release.get('type')=='file' and release.get('path')=='state/runtime-release.json' and release.get('encoding')=='base64')
    raw=base64.b64decode(release['content'].replace('\n',''),validate=True)
    need(B.blob_sha(raw)==release['sha'] and B._parse(raw,canonical=False)['repository_commit_sha']==grant['consumer_sha'])
    need(B._stamp(now)<B._stamp(authority['actions_free_until']))
    return repository


def fetch_delivery(api,updater,candidate,run_id,*,now,repository):
    run=api.request('GET','/repos/'+STORE+'/actions/runs/'+str(run_id))
    jobs=api.request('GET','/repos/'+STORE+'/actions/runs/'+str(run_id)+'/jobs?per_page=2')
    need(jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1)
    log=api.job_log(jobs['jobs'][0]['id'])
    expectation=updater.delivery_expectation(candidate,run_id=run_id,for_read=True)
    return verify_daily_delivery(expectation,repository,run,jobs,log,now=now)


def dispatch_verify_once(api,updater,candidate,*,repository,clock,sleep=time.sleep,monotonic=time.monotonic):
    # Reserve BEFORE the POST. Expiry or an uncertain response never redispatches.
    updater.reserve_verification(candidate)
    grant=updater.grant
    branch='basis-consumer-delivery/'+grant['workflow_source_sha']
    # Reservation round trips can consume the remaining slot. Authorize the
    # external POST now, and record its start rather than its delayed response.
    started=updater._fresh(updater._slot(candidate.slot_id));deadline=monotonic()+180
    reply=api.request('POST','/repos/'+STORE+'/actions/workflows/'+str(grant['workflow_id'])+'/dispatches',
        {'ref':branch,'inputs':{'compute_sha':grant['compute_sha'],'receipt_commitment':candidate.receipt_commitment}})
    # Explicit GitHub REST 2026-03-10 response contract; never infer the run
    # from a list of concurrent jobs or retry a timeout/204/ambiguous response.
    B._closed(reply,('workflow_run_id','run_url','html_url'))
    run_id=reply['workflow_run_id'];need(type(run_id) is int and run_id>0)
    need(reply['run_url']=='https://api.github.com/repos/'+STORE+'/actions/runs/'+str(run_id) and
         reply['html_url']=='https://github.com/'+STORE+'/actions/runs/'+str(run_id))
    mapping=B.encode(dict(schema_version=1,kind='finite_daily_delivery_mapping',candidate=asdict(candidate),run_id=run_id,dispatched_at=started.isoformat()))
    mapping_ref=f'refs/heads/basis-daily-deliveries/{updater.digest}/{candidate.slot_id}'
    mapping_commit=updater.store.commit(candidate.selector['read_sha'],{'basis-authority/daily-delivery.json':mapping},replace=set())
    updater.store.cas(mapping_ref,mapping_commit,'0'*40)
    while monotonic()<deadline:
        run=api.request('GET','/repos/'+STORE+'/actions/runs/'+str(run_id))
        need(run.get('id')==run_id and run.get('workflow_id')==grant['workflow_id'] and run.get('head_sha')==grant['workflow_source_sha'] and run.get('head_branch')==branch and run.get('event')=='workflow_dispatch' and run.get('run_attempt')==1)
        if run.get('status')=='completed':
            need(run.get('conclusion')=='success')
            return fetch_delivery(api,updater,candidate,run_id,now=clock().isoformat(),repository=repository)
        sleep(5)
    raise HostedDailyError(STOP)


def _save_private(path,payload):
    p=Path(path).absolute();need(not any(x.is_symlink() for x in (p,*p.parents)))
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(B.encode(payload));out.flush();os.fsync(out.fileno())
    need(p.read_bytes()==B.encode(payload))


def _delegation(updater,binding,*,trade_date,request_id,status,selector):
    return dict(schema_version=1,kind='trusted_daily_basis_delegation',status=status,symbol='SOXX',
        compute_sha=updater.authority.compute_sha,store_sha=updater.store.head(),execution_day=trade_date,
        request_id=request_id,authority_binding=binding,grant_sha256=updater.digest,selector=selector,
        volume_policy='BLOCKED_UNRESOLVED')


def wait_for_daily_budget_window(updater,slot,*,clock,sleep=time.sleep,monotonic=time.monotonic):
    """Bounded no-debit wait, rechecking authority/head before atomic claim."""
    deadline=monotonic()+90
    for _ in range(3):
        now=updater._fresh(slot);head=updater.store.head();day=slot['execution_day']
        path=f'data/market-data/collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        state=B._parse(updater.store.read(head,path)) if updater.store.exists(head,path) else dict(schema_version=1,trade_date=day,credits=0,minutes={})
        need(type(state.get('credits')) is int and 0<=state['credits']<200 and type(state.get('minutes')) is dict)
        minute=now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
        used=state['minutes'].get(minute,0);need(type(used) is int and 0<=used<=8)
        if now.second+now.microsecond/1_000_000<38 and used<8:return
        delay=61-(now.second+now.microsecond/1_000_000)
        need(monotonic()+delay<deadline and now+timedelta(seconds=delay)<B._stamp(slot['valid_until']))
        sleep(delay)
    raise HostedDailyError(STOP)


def run_route(*,store,api,compute,compute_sha,commitment,trade_date,env,key,access,
              delegation_output,clock=lambda:datetime.now(timezone.utc),capture=None,source_check=None):
    """One finite approved slot inside the existing shared collector owner.

    store is the concrete RemoteGitStore in the official CLI. Injected local
    stores/platform doubles are for synthetic tests, never hosted attestation.
    """
    try:
        with store.lock():
            if source_check:source_check()
            run=api.request('GET','/repos/'+COMPUTE+'/actions/runs/'+env['GITHUB_RUN_ID'])
            repository=api.request('GET','/repos/'+STORE)
            need(repository.get('private') is True and repository.get('full_name')==STORE)
            # Basic immutable workflow/platform checks remain global even when
            # optional SOXX authority is absent, malformed or exhausted.
            bootstrap=dict(runtime_workflow_id=run.get('workflow_id'),owner_id=repository['owner']['id'])
            validate_runtime_surface(env,bootstrap,run,compute_sha=compute_sha,trade_date=trade_date)
            def scope_hold(status,*,binding=None,grant_digest=None):
                head=store.head();path='data/market-data/series-active/daily/twelve_data_basic/SOXX.json'
                selector=B._parse(store.read(head,path))|{'read_sha':head} if store.exists(head,path) else None
                if selector is not None:B._selection(selector)
                _save_private(delegation_output,dict(schema_version=1,kind='trusted_daily_basis_delegation',status=status,symbol='SOXX',compute_sha=compute_sha,store_sha=head,execution_day=trade_date,request_id=env['GITHUB_RUN_ID']+'-1',authority_binding=binding,grant_sha256=grant_digest,selector=selector,volume_policy='BLOCKED_UNRESOLVED'))
                return 'FINITE_DAILY_BASIS_HELD'
            try:
                authority,grant_raw,calendar_raw,binding=read_authority(store,commitment,compute_sha,now=clock().isoformat(),allow_expired_hold=True)
                grant=B._parse(grant_raw)
            except (ScopeUnavailable,B.BasisError,ValueError,KeyError):
                return scope_hold('ACQUISITION_HELD_AUTHORITY_UNAVAILABLE')
            validate_runtime_surface(env,authority,run,compute_sha=compute_sha,trade_date=trade_date)
            if clock()>=B._stamp(authority['valid_until']):
                return scope_hold('ACQUISITION_HELD_GRANT_EXHAUSTED',binding=binding,grant_digest=B.sha(grant_raw))
            repository=check_private_readiness(api,store,authority,grant,now=clock().isoformat())
            auth=GrantAuthority(B.sha(grant_raw),compute_sha,authority['consumer_sha'],authority['workflow_source_sha'],B.sha(calendar_raw))
            updater=DailyUpdater(store,grant_raw,calendar_raw,auth,clock=clock)
            request_id=env['GITHUB_RUN_ID']+'-1'
            slots=[s for s in grant['slots'] if s['execution_day']==trade_date and B._stamp(s['valid_from'])<=clock()<B._stamp(s['valid_until'])]
            if not slots:return scope_hold('ACQUISITION_HELD_REVIEW_REQUIRED',binding=binding,grant_digest=B.sha(grant_raw))
            need(len(slots)==1);slot=slots[0]
            for prior in grant['slots'][:grant['slots'].index(slot)]:
                if not store.exists(store.head(),updater._transition_path(prior)):
                    return scope_hold('ACQUISITION_HELD_REVIEW_REQUIRED',binding=binding,grant_digest=updater.digest)
            if not store.exists(store.head(),'data/market-data/series-active/daily/twelve_data_basic/SOXX.json'):
                # Only an approved isolated first candidate can bootstrap. The
                # catalog stays absent on main until new data verifies and CAS wins.
                try:updater._previous(slot,store.head())
                except Exception:
                    _save_private(delegation_output,_delegation(updater,binding,trade_date=trade_date,request_id=request_id,status='ACQUISITION_HELD_PENDING_INITIAL_BASIS',selector=None))
                    return 'FINITE_DAILY_BASIS_HELD'
            if source_check:source_check()
            wait_for_daily_budget_window(updater,slot,clock=clock)
            candidate=updater.run_slot(slot['slot_id'],request_id,capture=capture or provider_capture(key,access))
            verified=dispatch_verify_once(api,updater,candidate,repository=repository,clock=clock)
            if source_check:source_check()
            updater.promote(candidate,verified);updater.read_current(verified,fields=('close',))
            _save_private(delegation_output,_delegation(updater,binding,trade_date=trade_date,request_id=request_id,status='OWNED_IMMUTABLE_DAILY',selector=candidate.selector))
            return 'FINITE_DAILY_BASIS_COMPLETE'
    except Exception:raise HostedDailyError(STOP) from None


def current_delivery(*,store,api,compute_sha,commitment,clock=lambda:datetime.now(timezone.utc)):
    """Read-only current mapping for the actual consumer, no provider/dispatch.

    Every invocation freshly authenticates repository/run/jobs/log responses and
    current grant/release/catalog; the old frozen one-shot adapter is unchanged.
    """
    with store.lock():
        authority,gr,cal,binding=read_authority(store,commitment,compute_sha,now=clock().isoformat())
        g=B._parse(gr);repository=check_private_readiness(api,store,authority,g,now=clock().isoformat())
        updater=DailyUpdater(store,gr,cal,GrantAuthority(B.sha(gr),compute_sha,g['consumer_sha'],g['workflow_source_sha'],B.sha(cal)),clock=clock)
        catalog=B._parse(store.read(store.head(),'data/market-data/series-active/daily/twelve_data_basic/SOXX.json'))
        found=[]
        for slot in g['slots']:
            path=updater._transition_path(slot)
            if store.exists(store.head(),path):
                transition=B._parse(store.read(store.head(),path))
                if transition['catalog']==catalog:found.append(slot)
        need(len(found)==1);slot=found[0]
        oid=store.materialize_ref(f'refs/heads/basis-daily-deliveries/{updater.digest}/{slot["slot_id"]}')
        mapping=B._parse(store.read(oid,'basis-authority/daily-delivery.json'))
        B._closed(mapping,('schema_version','kind','candidate','run_id','dispatched_at'))
        need(mapping['schema_version']==1 and mapping['kind']=='finite_daily_delivery_mapping')
        candidate=Candidate(**mapping['candidate'])
        mapping_path='basis-authority/daily-delivery.json'
        store.g.single_addition(oid,candidate.selector['read_sha'],mapping_path,store.g.normal_blob_identity(oid,mapping_path))
        need(B._stamp(slot['valid_from'])<=B._stamp(mapping['dispatched_at'])<B._stamp(slot['valid_until']))
        for ref in (updater._candidate_ref(slot),updater._attempt_ref(slot),updater._verification_ref(slot),candidate.receipt_ref):store.materialize_ref(ref)
        verified=fetch_delivery(api,updater,candidate,mapping['run_id'],now=clock().isoformat(),repository=repository)
        need(B._stamp(mapping['dispatched_at'])<=B._stamp(verified.verified_at))
        view=updater.read_current(verified,fields=tuple(B.PRICE_FIELDS))
        return dict(selector=dict(candidate.selector,read_sha=view['read_sha']),admitted_selector=candidate.selector,receipt_commitment=candidate.receipt_commitment,compute_sha=compute_sha,consumer_sha=g['consumer_sha'],grant_sha256=updater.digest,slot_id=candidate.slot_id,receipt_ref=candidate.receipt_ref,receipt_commit=candidate.receipt_commit,receipt_blob_sha=candidate.receipt_blob_sha,grant_ref=authority['grant_ref'],calendar_ref=authority['calendar_ref'],authority_binding=binding,grant_raw=gr,calendar_raw=cal,verified_run_id=verified.run_id,verified_at=verified.verified_at,valid_until=candidate.receipt_until,current_store_sha=store.head(),calendar=B._parse(cal),view=view,volume_policy='BLOCKED_UNRESOLVED')


def main(argv=None):
    try:
        parser=argparse.ArgumentParser();parser.add_argument('--store',required=True);parser.add_argument('--compute-sha',required=True);parser.add_argument('--trade-date',required=True);parser.add_argument('--delegation-output',required=True)
        args=parser.parse_args(argv);os.umask(0o077)
        token=os.environ.get('MARKET_DATA_STORE_TOKEN','');key=os.environ.get('TWELVE_DATA_API_KEY','');commitment=os.environ.get('DAILY_BASIS_GRANT_COMMITMENT','')
        need(commitment)
        from scripts.daily_basis_remote import RemoteGitStore
        import yaml
        compute=Path(__file__).resolve().parents[1]
        with _quiet():
            def source_check():
                with _environment():_source(compute,args.compute_sha)
            source_check()
            api=GitHubAPI(token)
            def platform_guard():
                env=dict(os.environ)
                need(env.get('GITHUB_ACTIONS')=='true' and env.get('GITHUB_EVENT_NAME')=='schedule' and env.get('GITHUB_RUN_ATTEMPT')=='1' and env.get('GITHUB_REPOSITORY')==COMPUTE and env.get('GITHUB_SHA')==args.compute_sha and env.get('GITHUB_JOB')=='collect')
                repository=api.request('GET','/repos/'+COMPUTE)
                run=api.request('GET','/repos/'+COMPUTE+'/actions/runs/'+env['GITHUB_RUN_ID'])
                need(repository['full_name']==COMPUTE and run['repository']['id']==repository['id'] and run['head_repository']['id']==repository['id'])
                need(run['actor']['id']==run['triggering_actor']['id']==repository['owner']['id'] and run['head_sha']==args.compute_sha and run['path']==CALLER and run['event']=='schedule' and run['run_attempt']==1 and run['status']=='in_progress')
            platform_guard()
            store=RemoteGitStore(Path(args.store),token,source_recheck=source_check,platform_guard=platform_guard)
            status=run_route(store=store,api=api,compute=compute,compute_sha=args.compute_sha,commitment=commitment,trade_date=args.trade_date,env=dict(os.environ),key=key,access=yaml.safe_load((compute/'config/market-data-collector-access.yaml').read_text()),delegation_output=args.delegation_output,source_check=source_check)
        print(status);return 0
    except Exception:print(STOP);return 2


if __name__=='__main__':raise SystemExit(main())
