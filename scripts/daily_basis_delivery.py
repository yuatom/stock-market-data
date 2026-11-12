"""Finite-grant adapter for the frozen authenticated private delivery contract.

Unlike the one-shot foundation adapter, an explicitly finite daily receipt
permits reuse of immutable attestation until its approved current-time expiry.
The current active catalog, grant/source and complete candidate lineage are
independently revalidated by DailyUpdater on every read. No historical backdating.

Platform responses must be obtained directly by the approved authenticated
connector, never loaded from a candidate/draft. Validating caller-supplied JSON
is not source authentication. This module performs no dispatch or credential I/O.
"""
from dataclasses import dataclass
import base64
import re
from datetime import timedelta
from scripts import provider_basis_snapshot as B

MARKER='BASIS_PRIVATE_DELIVERY_V1 '
WORKFLOW='.github/workflows/basis-consumer-delivery.yml'


@dataclass(frozen=True,repr=False)
class DeliveryExpectation:
    repository_id: int
    owner_id: int
    workflow_id: int
    workflow_source_sha: str
    compute_sha: str
    receipt_commitment: str
    run_id: int


def _verify_private_delivery(expected, repository, run, jobs, log, *, now, calendar_authority):
    try:
        B._need(type(expected) is DeliveryExpectation and type(calendar_authority) is dict and bool(calendar_authority))
        for k in ('repository_id','owner_id','workflow_id','run_id'):
            B._need(type(getattr(expected,k)) is int and getattr(expected,k)>0)
        B._hex(expected.workflow_source_sha);B._hex(expected.compute_sha);B._hex(expected.receipt_commitment,64)
        B._need(repository['id']==expected.repository_id and repository['private'] is True
            and repository['full_name']=='yuatom/stock-market-data-store' and repository['owner']['id']==expected.owner_id)
        required=dict(id=expected.run_id,workflow_id=expected.workflow_id,head_sha=expected.workflow_source_sha,
            path=WORKFLOW,event='workflow_dispatch',run_attempt=1,status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+expected.workflow_source_sha)
        B._need(all(run.get(k)==v for k,v in required.items()))
        B._need(run['repository']['id']==expected.repository_id and run['head_repository']['id']==expected.repository_id)
        B._need(run['actor']['id']==run['triggering_actor']['id']==expected.owner_id)
        B._need(type(jobs) is dict and jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1)
        job=jobs['jobs'][0]
        B._need(job['run_id']==expected.run_id and job['name']=='verify-basis-consumer'
            and job['head_sha']==expected.workflow_source_sha and job['status']=='completed' and job['conclusion']=='success')
        B._need(type(job['id']) is int and job['id']>0)
        steps=[s for s in job['steps'] if s.get('name')=='Verify and emit private delivery']
        B._need(len(steps)==1 and steps[0]['status']=='completed' and steps[0]['conclusion']=='success')
        # A decoded job log response must be fetched using this exact job id.
        B._need(type(log) is dict and set(log)=={'job_id','text'} and log['job_id']==job['id'])
        raw=log['text'];B._need(type(raw) is str and 0<len(raw)<=1024*1024)
        records=[]
        for line in raw.splitlines():
            if MARKER in line:
                match=re.fullmatch(r'(?:\d{4}-\d\d-\d\dT[0-9:.]+Z )?'+re.escape(MARKER)+r'([A-Za-z0-9+/=]+)',line)
                B._need(match);records.append(match.group(1))
        B._need(len(records)==1)
        decoded=base64.b64decode(records[0],validate=True);B._need(len(decoded)<=16384)
        value=B._parse(decoded)
        B._closed(value,('schema_version','kind','compute_sha','receipt_commitment','context','receipt_ref','receipt_commit','receipt_blob_sha','selector','verified_at','valid_until'))
        B._need(value['schema_version']==1 and value['kind']=='private_basis_consumer_delivery'
            and value['compute_sha']==expected.compute_sha and value['receipt_commitment']==expected.receipt_commitment)
        c=value['context'];B._closed(c,('repository','repository_id','owner_id','private','workflow_source_sha','run_id','run_attempt','event','actor_id','actor','triggering_actor','workflow_path','job'))
        B._need(c==dict(repository='yuatom/stock-market-data-store',repository_id=expected.repository_id,
            owner_id=expected.owner_id,private=True,workflow_source_sha=expected.workflow_source_sha,
            run_id=expected.run_id,run_attempt=1,event='workflow_dispatch',actor_id=expected.owner_id,
            actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer'))
        B._need(value['receipt_ref']==f'refs/heads/basis-read-receipts/{expected.compute_sha}/{expected.receipt_commitment}')
        B._hex(value['receipt_commit']);B._hex(value['receipt_blob_sha']);B._selection(value['selector'])
        instant=B._stamp(now);verified=B._stamp(value['verified_at'])
        B._need(B._stamp(run['run_started_at'])<=verified<=B._stamp(job['completed_at'])<=instant
            and verified<=instant<B._stamp(value['valid_until']))
        return value
    except Exception:
        raise ValueError('private basis delivery rejected') from None


@dataclass(frozen=True,repr=False)
class DailyDeliveryExpectation:
    grant_sha256:str
    slot_id:str
    repository_id:int
    owner_id:int
    workflow_id:int
    workflow_source_sha:str
    compute_sha:str
    run_id:int
    candidate:object


@dataclass(frozen=True,repr=False)
class VerifiedDailyDelivery:
    grant_sha256:str
    slot_id:str
    candidate:object
    verified_at:str
    run_id:int


def verify_daily_delivery(expected,repository,run,jobs,log,*,now):
    from scripts.daily_basis_updates import DailyBasisError
    try:
        B._need(type(expected) is DailyDeliveryExpectation)
        c=expected.candidate
        B._need(B._stamp(now)<B._stamp(c.receipt_until))
        base=DeliveryExpectation(expected.repository_id,expected.owner_id,expected.workflow_id,
            expected.workflow_source_sha,expected.compute_sha,c.receipt_commitment,expected.run_id)
        value=_verify_private_delivery(base,repository,run,jobs,log,now=now,calendar_authority={'finite_grant':expected.grant_sha256})
        B._need(value['selector']==c.selector and value['receipt_commit']==c.receipt_commit and
                value['receipt_blob_sha']==c.receipt_blob_sha and value['receipt_ref']==c.receipt_ref and
                value['valid_until']==c.receipt_until)
        return VerifiedDailyDelivery(expected.grant_sha256,expected.slot_id,c,value['verified_at'],expected.run_id)
    except Exception:raise DailyBasisError('DAILY_BASIS_DELIVERY_REJECTED') from None
