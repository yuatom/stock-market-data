"""One owner-authorized manual SOXX slot; reuses finite daily acquisition core."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_updates import DailyUpdater,GrantAuthority,provider_capture
from scripts.run_daily_basis_route import (GitHubAPI,basis_actions_token,report_release_reader,check_private_readiness,dispatch_verify_once,
    wait_for_daily_budget_window,_quiet,_source,_environment,ReadinessFailure,READINESS_STEPS)
from scripts import run_basis_actions_readiness_probe as readiness_probe
from scripts.daily_basis_remote import RemoteGitStore,RemoteStoreError,SAFE_PHASES

COMPUTE='yuatom/stock-market-data';STORE='yuatom/stock-market-data-store'
WORKFLOW='.github/workflows/one-shot-soxx-basis-gap.yml'
SCOPE_PATH='basis-authority/one-shot-soxx-gap.json'
BASIS='812eefd58ef3cc569af6fc53fb6c1c54364234f0f1fa21d5c806bc8e385c9433'
DIAGNOSTIC='11b3101837be5a558eb4c67fca1ba28cdaad8babfabd30a82c033d7f0cf76ed1'
STOP='ONE_SHOT_SOXX_STOPPED_INSPECT_NO_RETRY'
SCOPE_KEYS=('schema_version','kind','compute_sha','consumer_sha','workflow_source_sha','repository_id','owner_id',
    'runtime_workflow_id','readiness_run_id','valid_from','valid_until','actions_free_until','grant_ref','calendar_ref')


STAGES=frozenset({'INITIALIZATION','SOURCE_GATE','PLATFORM_GATE','STORE_ADAPTER',
    'STORE_LOCK','STORE_HEAD','AUTHORITY_FETCH','AUTHORITY_READ','SCOPE_CONTRACT',
    'RUN_AUTHORITY','PRIVATE_READINESS','GRANT_BASELINE','BUDGET_WINDOW',
    'CAPTURE_AND_CANDIDATE','PRIVATE_VERIFICATION','PROMOTION','CONSUMER_READ','PRECLAIM_CHECK','SAME_PROCESS_READINESS'})

SAFE_DETAILS=SAFE_PHASES|READINESS_STEPS

class OneShotFailure(RuntimeError):
    def __init__(self,stage,error):
        self.stage=stage if stage in STAGES else 'INITIALIZATION'
        # Never serialize exception messages, arbitrary class names, URLs or values.
        self.category=('TIMEOUT' if isinstance(error,TimeoutError) else
                       'IO_OR_NETWORK' if isinstance(error,OSError) else
                       'CONTRACT_OR_INPUT' if isinstance(error,(ValueError,KeyError,TypeError)) else
                       'GUARDED_OPERATION')
        self.detail=error.safe_phase if isinstance(error,RemoteStoreError) and error.safe_phase in SAFE_PHASES else 'UNSPECIFIED'
        self.http_status=None
        if isinstance(error,ReadinessFailure):
            self.detail=error.safe_step if error.safe_step in READINESS_STEPS else 'UNSPECIFIED'
            self.http_status=error.http_status if type(error.http_status) is int and 100<=error.http_status<=599 else None
            self.category=error.safe_kind if error.safe_kind in {'HTTP_ERROR','TIMEOUT','NETWORK_ERROR','CONTRACT_OR_INPUT'} else 'GUARDED_OPERATION'
        super().__init__(STOP)

def step(stage,fn,*args,**kwargs):
    try:return fn(*args,**kwargs)
    except OneShotFailure:raise
    except Exception as exc:raise OneShotFailure(stage,exc) from None

def failure_result(exc):
    safe=exc if isinstance(exc,OneShotFailure) else OneShotFailure('INITIALIZATION',exc)
    return dict(status=STOP,automatic_retry_allowed=False,
                failure_stage=safe.stage if safe.stage in STAGES else 'INITIALIZATION',
                failure_detail=safe.detail if safe.detail in SAFE_DETAILS else 'UNSPECIFIED',
                error_category=safe.category if safe.category in {'TIMEOUT','IO_OR_NETWORK','CONTRACT_OR_INPUT','GUARDED_OPERATION','HTTP_ERROR','NETWORK_ERROR'} else 'GUARDED_OPERATION',
                http_status=safe.http_status if type(safe.http_status) is int and 100<=safe.http_status<=599 else None)


def need(value):
    if not value:raise ValueError(STOP)


def validate_surface(env,run,repository,compute_sha):
    expected=dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',
        GITHUB_REPOSITORY=COMPUTE,GITHUB_SHA=compute_sha,GITHUB_REF='refs/heads/main',GITHUB_JOB='soxx_gap',
        GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom')
    need(all(env.get(k)==v for k,v in expected.items()))
    need(env.get('GITHUB_WORKFLOW_REF')==COMPUTE+'/'+WORKFLOW+'@refs/heads/main')
    need(re.fullmatch('[1-9][0-9]*',env.get('GITHUB_RUN_ID','')))
    need(repository['full_name']==COMPUTE and run['repository']['id']==run['head_repository']['id']==repository['id'])
    need(run['id']==int(env['GITHUB_RUN_ID']) and run['head_sha']==compute_sha and run['path']==WORKFLOW
         and run['event']=='workflow_dispatch' and run['run_attempt']==1 and run['head_branch']=='main' and run['status']=='in_progress')
    need(run['actor']['id']==run['triggering_actor']['id']==repository['owner']['id'])


def validate_scope(raw,expected_sha,grant_raw,calendar_raw,*,compute_sha,now):
    need(B.sha(raw)==B._hex(expected_sha,64));scope=B._parse(raw);B._closed(scope,SCOPE_KEYS+(('readiness',) if 'readiness' in scope else ()))
    if 'readiness' in scope:
        validate_readiness(scope['readiness'])
        need(now<B._stamp(scope['readiness']['expires'])<=B._stamp(scope['valid_until']))
    need(scope['schema_version']==1 and scope['kind']=='one_shot_soxx_gap_authority' and scope['compute_sha']==compute_sha)
    for key in ('consumer_sha','workflow_source_sha'):B._hex(scope[key])
    for key in ('repository_id','owner_id','runtime_workflow_id','readiness_run_id'):need(type(scope[key]) is int and scope[key]>0)
    need(B._stamp(scope['valid_from'])<=now<B._stamp(scope['valid_until'])<=B._stamp(scope['actions_free_until']))
    for key,data in (('grant_ref',grant_raw),('calendar_ref',calendar_raw)):
        ref=scope[key];B._closed(ref,('commit','path','blob_sha','sha256'));B._hex(ref['commit'])
        need(ref['path'].startswith('basis-authority/') and B.blob_sha(data)==ref['blob_sha'] and B.sha(data)==ref['sha256'])
    grant=B._parse(grant_raw);calendar=B._parse(calendar_raw)
    for key in ('compute_sha','consumer_sha','workflow_source_sha','repository_id','owner_id'):need(grant[key]==scope[key])
    need(grant['request_limit']==grant['credit_limit']==grant['verification_limit']==1 and len(grant['slots'])==1)
    need(grant['initial_selector']['basis_id']==BASIS and grant['baseline_ref']['sha256']==DIAGNOSTIC)
    slot=grant['slots'][0]
    need(slot['slot_id']=='soxx-nov14-gap' and slot['execution_day'] in ('2026-11-14','2026-11-15') and slot['execution_day']==now.astimezone(ZoneInfo('America/New_York')).date().isoformat() and slot['cutoff']=='2026-11-13')
    need(B._stamp(scope['valid_from'])<=B._stamp(slot['valid_from']) and B._stamp(slot['receipt_until'])<=B._stamp(scope['valid_until']))
    req=grant['request_template'];need(req['start_date']=='2025-08-08' and req['end_date']=='2026-11-11')
    need(len(calendar['sessions'])==320 and calendar['sessions'][-1]['trade_date']=='2026-11-13'
         and calendar['sessions'][-2]['trade_date']=='2026-11-12')
    return scope,grant


def validate_readiness(value):
    need(type(value) is dict);B._closed(value,('commitment','runner_id','runner_name','expires'))
    B._hex(value['commitment'],64)
    need(type(value['runner_id']) is int and value['runner_id']>0)
    need(type(value['runner_name']) is str and re.fullmatch('[A-Za-z0-9_.-]{1,128}',value['runner_name']))
    B._stamp(value['expires'])
    return value


def readiness_arguments(commitment,runner_id,runner_name,expires,preflight_only):
    values=(commitment,runner_id,runner_name,expires)
    if all(v in (None,'') for v in values):return None
    need(not preflight_only and all(v not in (None,'') for v in values))
    return validate_readiness(dict(commitment=commitment,runner_id=int(runner_id),runner_name=runner_name,expires=expires))


def run_once(*,store,api,scope_commit,scope_sha,expected_store_sha,compute_sha,env,clock,key,access,source_check,capture=None,preflight_only=False,readiness=None,release_api=None):
    stage="INITIALIZATION"
    try:
        need(type(preflight_only) is bool)
        need(not(preflight_only and readiness is not None))
        if readiness is not None:validate_readiness(readiness)
        stage="STORE_LOCK"
        with store.lock():
            stage="STORE_HEAD"
            source_check();need(store.head()==expected_store_sha)
            stage="AUTHORITY_FETCH"
            ref=f'refs/heads/basis-daily-authority/{compute_sha}/{B._hex(scope_sha,64)}'
            need(store.materialize_ref(ref)==B._hex(scope_commit))
            stage="AUTHORITY_READ"
            raw=store.read(scope_commit,SCOPE_PATH);scope=B._parse(raw);loaded=[]
            for key in ('grant_ref','calendar_ref'):
                r=scope[key];need(store.g.text('merge-base',r['commit'],scope_commit)==r['commit']);loaded.append(store.read(r['commit'],r['path']))
            stage="SCOPE_CONTRACT"
            scope,grant=validate_scope(raw,scope_sha,*loaded,compute_sha=compute_sha,now=clock())
            need(scope.get('readiness')==readiness)
            stage="RUN_AUTHORITY"
            repository=api.request('GET','/repos/'+COMPUTE);run=api.request('GET','/repos/'+COMPUTE+'/actions/runs/'+env['GITHUB_RUN_ID'])
            validate_surface(env,run,repository,compute_sha)
            need(run['workflow_id']==scope['runtime_workflow_id'] and repository['owner']['id']==scope['owner_id'])
            stage="PRIVATE_READINESS"
            if readiness is None:private=check_private_readiness(api,store,scope,grant,now=clock().isoformat(),**({'release_api':release_api} if release_api is not None else {}))
            stage="GRANT_BASELINE"
            u=DailyUpdater(store,*loaded,GrantAuthority(B.sha(loaded[0]),compute_sha,scope['consumer_sha'],scope['workflow_source_sha'],B.sha(loaded[1])),clock=clock)
            stage="BUDGET_WINDOW"
            slot=grant['slots'][0];source_check();wait_for_daily_budget_window(u,slot,clock=clock)
            if preflight_only:
                stage="PRECLAIM_CHECK"
                u.preflight_slot(slot['slot_id'],env['GITHUB_RUN_ID']+'-1')
                source_check();need(store.head()==expected_store_sha)
                return dict(status='ONE_SHOT_SOXX_PREFLIGHT_COMPLETE',provider_requests=0,
                            credits_reserved=0,production_data_writes=0,
                            execution_performed=False,automatic_retry_allowed=False)
            runner_expectation=None
            if readiness is not None:
                stage="PRECLAIM_CHECK"
                u.preflight_slot(slot['slot_id'],env['GITHUB_RUN_ID']+'-1')
                need(grant['workflow_id']==readiness_probe.WORKFLOW_ID and grant['workflow_source_sha']==readiness_probe.SOURCE)
                source_check();need(store.head()==expected_store_sha)
                stage="SAME_PROCESS_READINESS"
                proof=readiness_probe.probe(api=api,store=store.repo,compute=compute_sha,
                    commitment=readiness['commitment'],runner_id=readiness['runner_id'],runner_name=readiness['runner_name'],
                    expires=readiness['expires'],env=env,source_check=source_check,clock=clock,caller_profile='one_shot')
                need(proof['status']=='BASIS_ACTIONS_READINESS_VERIFIED' and proof['provider_requests']==0)
                stage="BUDGET_WINDOW"
                source_check();need(store.head()==expected_store_sha and clock()<B._stamp(readiness['expires']))
                wait_for_daily_budget_window(u,slot,clock=clock)
                need(clock()<B._stamp(readiness['expires']))
                stage="PRIVATE_READINESS"
                private=check_private_readiness(api,store,{**scope,'readiness_run_id':proof['run_id']},grant,now=clock().isoformat(),**({'release_api':release_api} if release_api is not None else {}))
                source_check();need(store.head()==expected_store_sha and clock()<B._stamp(readiness['expires']))
                runner_expectation=dict(runner_id=readiness['runner_id'],runner_name=readiness['runner_name'],labels=sorted(readiness_probe.LABELS),expires=readiness['expires'])
            stage="CAPTURE_AND_CANDIDATE"
            candidate=u.run_slot(slot['slot_id'],env['GITHUB_RUN_ID']+'-1',capture=capture or provider_capture(key,access))
            stage="PRIVATE_VERIFICATION"
            verified=dispatch_verify_once(api,u,candidate,repository=private,clock=clock,**({'runner_expectation':runner_expectation} if runner_expectation else {}))
            stage="PROMOTION"
            if readiness is not None:need(clock()<B._stamp(readiness['expires']))
            source_check();u.promote(candidate,verified)
            stage="CONSUMER_READ"
            view=u.read_current(verified,fields=B.PRICE_FIELDS)
            need(view['record_count']==320 and view['last_trade_date']=='2026-11-13' and view['volume_status']=='BLOCKED_UNRESOLVED')
            return dict(status='ONE_SHOT_SOXX_GAP_COMPLETE',requests_attempted=1,additional_credits=1,store_sha=store.head(),basis_id=candidate.selector['basis_id'],last_trade_date=view['last_trade_date'],volume_policy='BLOCKED_UNRESOLVED',verification_run_id=verified.run_id,receipt_commitment=candidate.receipt_commitment,**({'readiness_run_id':proof['run_id']} if readiness is not None else {}))
    except OneShotFailure:raise
    except Exception as exc:raise OneShotFailure(stage,exc) from None


def main():
    p=argparse.ArgumentParser()
    for name in ('store','compute-sha','expected-store-sha','scope-commit','scope-sha'):p.add_argument('--'+name,required=True)
    p.add_argument('--preflight-only',choices=('true','false'),required=True)
    for name in ('readiness-receipt-commitment','readiness-runner-id','readiness-runner-name','readiness-expires'):p.add_argument('--'+name)
    args=p.parse_args();os.umask(0o077)
    try:
        import yaml
        readiness=readiness_arguments(args.readiness_receipt_commitment,args.readiness_runner_id,args.readiness_runner_name,args.readiness_expires,args.preflight_only=='true')
        compute=Path(__file__).resolve().parents[1];token=os.environ.get('MARKET_DATA_STORE_TOKEN','');key=os.environ.get('TWELVE_DATA_API_KEY','')
        with _quiet():
            def source_check():
                with _environment():step('SOURCE_GATE',_source,compute,args.compute_sha)
            api=step('INITIALIZATION',lambda:GitHubAPI(basis_actions_token(os.environ)))
            release_api=step('INITIALIZATION',report_release_reader,os.environ)
            def platform_guard():
                repo=api.request('GET','/repos/'+COMPUTE);run=api.request('GET','/repos/'+COMPUTE+'/actions/runs/'+os.environ['GITHUB_RUN_ID'])
                validate_surface(dict(os.environ),run,repo,args.compute_sha)
            source_check();step('PLATFORM_GATE',platform_guard)
            store=step('STORE_ADAPTER',RemoteGitStore,Path(args.store),token,source_recheck=source_check,platform_guard=platform_guard)
            result=run_once(store=store,api=api,scope_commit=args.scope_commit,scope_sha=args.scope_sha,expected_store_sha=args.expected_store_sha,compute_sha=args.compute_sha,env=dict(os.environ),clock=lambda:datetime.now(timezone.utc),key=key,access=yaml.safe_load((compute/'config/market-data-collector-access.yaml').read_text()),source_check=source_check,preflight_only=args.preflight_only=='true',readiness=readiness,release_api=release_api)
        print(json.dumps(result));return 0
    except Exception as exc:print(json.dumps(failure_result(exc)));return 2

if __name__=='__main__':raise SystemExit(main())
