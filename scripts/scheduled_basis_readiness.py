"""Bound a zero-provider private readiness dispatch to a finite scheduled scope.

Called only from the existing authenticated scheduled owner, with the validated
private authority/slot and the same API instance later used for acquisition.
"""
from datetime import timedelta
import re
import time

from scripts import provider_basis_snapshot as B
from scripts.daily_basis_delivery import DeliveryExpectation, _verify_private_delivery, WORKFLOW

STORE = 'yuatom/stock-market-data-store'
WORKFLOW_ID = 415302427
PLATFORM = {'self-hosted', 'macOS', 'ARM64'}

def validate_scope(scope):
    B._closed(scope, ('commitment', 'runner_id', 'runner_name', 'labels', 'expires'))
    B._hex(scope['commitment'],64)
    B._need(scope['commitment'] != '0'*64 and type(scope['runner_id']) is int and scope['runner_id'] > 0)
    B._need(type(scope['runner_name']) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',scope['runner_name']))
    labels=scope['labels']
    B._need(type(labels) is list and len(labels)==4 and all(type(label) is str for label in labels))
    extra=set(labels)-PLATFORM
    B._need(PLATFORM <= set(labels) and len(extra)==1 and re.fullmatch(r'soxx-private-[a-z0-9-]{8,90}',next(iter(extra))))
    return B._stamp(scope['expires'])

def probe(*,api,store,scope,grant,slot,prior,head,request_id,repository,
          compute_sha,clock,sleep=time.sleep,monotonic=time.monotonic):
    expires=validate_scope(scope)
    source=B._hex(grant['workflow_source_sha'])
    B._need(grant['workflow_id']==WORKFLOW_ID
            and B._stamp(slot['valid_from'])<=clock()<expires<=B._stamp(slot['valid_until'])
            and expires<=clock()+timedelta(minutes=10))
    commitment=scope['commitment']
    ref=f'refs/heads/basis-read-receipts/{compute_sha}/{commitment}'
    receipt_commit=store.materialize_ref(ref)
    receipt_blob=store.g.normal_blob_identity(receipt_commit,'basis-authority/receipt.json')
    raw=B._blob(store.g,receipt_commit,'basis-authority/receipt.json',receipt_blob)
    receipt=B._receipt(raw)
    selector,_=B.verify_consumer_receipt(raw,commitment,compute_sha,now=clock().isoformat())
    # An unrelated later Store commit may change read_sha, never the exact
    # admitted identity, snapshot and admission proved by the previous gate.
    B._need({k:v for k,v in selector.items() if k!='read_sha'}=={k:v for k,v in prior.items() if k!='read_sha'}
            and expires<=B._stamp(receipt['valid_until']) and store.head()==head and clock()<expires)
    # Create-only ref, not a main write, budget debit, or provider claim. An
    # uncertain reservation/dispatch is never retried with this receipt.
    attempted=f'refs/heads/basis-daily-readiness-attempt/{compute_sha}/{commitment}'
    B._need(store.remote_ref(attempted) is None)
    claim=B.encode(dict(schema_version=1,kind='scheduled_basis_readiness_attempt',
                        request_id=request_id,receipt_commit=receipt_commit,claimed_at=clock().isoformat()))
    claim_commit=store.commit(head,{'basis-authority/readiness-attempt.json':claim},replace=set())
    store.cas(attempted,claim_commit,'0'*40)
    B._need(store.remote_ref(attempted)==claim_commit and store.head()==head and clock()<expires)
    branch='basis-consumer-delivery/'+source
    base='/repos/'+STORE+'/actions'
    started=clock(); B._need(started<expires)
    reply=api.request('POST',f'{base}/workflows/{WORKFLOW_ID}/dispatches',
        {'ref':branch,'inputs':{'compute_sha':compute_sha,'receipt_commitment':commitment}})
    B._closed(reply,('workflow_run_id','run_url','html_url'))
    run_id=reply['workflow_run_id']
    B._need(type(run_id) is int and run_id>0 and
        reply['run_url']==f'https://api.github.com{base}/runs/{run_id}' and
        reply['html_url']==f'https://github.com/{STORE}/actions/runs/{run_id}')
    deadline=monotonic()+180
    while monotonic()+30<=deadline and clock()+timedelta(seconds=30)<expires:
        run=api.request('GET',f'{base}/runs/{run_id}')
        B._need(run.get('id')==run_id and run.get('workflow_id')==WORKFLOW_ID and
                run.get('head_sha')==source and run.get('head_branch')==branch and
                run.get('event')=='workflow_dispatch' and run.get('run_attempt')==1 and
                started.replace(microsecond=0)<=B._stamp(run['created_at'])<=clock())
        if run.get('status')=='completed':
            break
        B._need(run.get('status') in ('queued','in_progress','waiting','pending','requested'))
        sleep(3)
    else:
        raise TimeoutError()
    B._need(run.get('conclusion')=='success' and
            B._stamp(run['created_at'])<=B._stamp(run['run_started_at'])<=clock()<expires)
    jobs=api.request('GET',f'{base}/runs/{run_id}/jobs?per_page=2')
    B._need(jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1)
    job=jobs['jobs'][0]
    B._need(job['runner_id']==scope['runner_id'] and job['runner_name']==scope['runner_name']
            and set(job['labels'])==set(scope['labels']) and clock()<expires)
    expected=DeliveryExpectation(grant['repository_id'],grant['owner_id'],WORKFLOW_ID,source,compute_sha,commitment,run_id)
    proof=_verify_private_delivery(expected,repository,run,jobs,api.job_log(job['id']),now=clock().isoformat(),
        calendar_authority={'finite_scheduled_readiness': True})
    B._need(proof['receipt_commit']==receipt_commit and proof['receipt_blob_sha']==receipt_blob
        and proof['selector']==selector and proof['valid_until']==receipt['valid_until'] and clock()<expires)
    return run_id,{k:scope[k] for k in ('runner_id','runner_name','labels','expires')}
