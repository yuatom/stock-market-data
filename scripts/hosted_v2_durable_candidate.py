"""Candidate pre-debit seam, intentionally not wired to a live collector.
A private, independently approved workflow blob must be passed by the trusted
authority caller. No provider or workflow transport occurs in this module.
"""
from datetime import timedelta

from scripts import provider_basis_snapshot as B
from scripts.daily_basis_delivery import DeliveryExpectation, WORKFLOW, _verify_private_delivery

PATH='basis-authority/readiness-proof.json'
CLAIM='basis-authority/readiness-attempt.json'
ZERO='0'*40
RUNWAY=timedelta(seconds=120)


def _ref(expected, purpose):
    return (f'refs/heads/basis-daily-readiness-{purpose}/'
            f'{B._hex(expected.compute_sha)}/{B._hex(expected.receipt_commitment,64)}')


def _check_source(store, expected, approved_blob):
    source=B._hex(expected.workflow_source_sha)
    branch='refs/heads/basis-consumer-delivery/'+source
    B._need(store.materialize_ref(branch)==source)
    B._blob(store.g,source,WORKFLOW,B._hex(approved_blob))


def _validate_ready(store, profile, repository, ready, approved_blob, dispatched_at, now):
    B._closed(profile, ('kind','workflow_blob','expires'))
    B._need(profile['kind']=='private_github_hosted_v1' and profile['workflow_blob']==approved_blob)
    expected,run,jobs,log,identity=ready
    B._need(type(expected) is DeliveryExpectation and expected.workflow_id==415302427)
    _check_source(store,expected,approved_blob)
    proof=_verify_private_delivery(expected,repository,run,jobs,log,
        now=now.isoformat(),calendar_authority={'finite_hosted_v2_readiness':True})
    job=jobs['jobs'][0]
    start, expires=B._stamp(dispatched_at), B._stamp(profile['expires'])
    B._need(job['labels']==['ubuntu-latest'] and
        start.replace(microsecond=0)<=B._stamp(run['created_at'])<=B._stamp(run['run_started_at'])
        <=B._stamp(job['completed_at'])<=now and
        now+RUNWAY<expires<=min(start+timedelta(minutes=10),B._stamp(proof['valid_until'])))
    B._need((proof['receipt_commit'],proof['receipt_blob_sha'],proof['selector'])==identity)
    receipt=store.materialize_ref(proof['receipt_ref'])
    B._need(receipt==proof['receipt_commit'])
    raw=B._blob(store.g,receipt,'basis-authority/receipt.json',proof['receipt_blob_sha'])
    selector,_=B.verify_consumer_receipt(raw,expected.receipt_commitment,expected.compute_sha,now=now.isoformat())
    B._need(selector==proof['selector'] and B._receipt(raw)['valid_until']==proof['valid_until'])
    return proof,job['id'],expires


def _read_claim(store,expected,request_id,receipt_commit):
    claim=store.materialize_ref(_ref(expected,'attempt'))
    _,parents=store.g.commit(claim)
    B._need(len(parents)==1)
    blob=store.g.normal_blob_identity(claim,CLAIM)
    store.g.single_addition(claim,parents[0],CLAIM,blob)
    a=B._parse(B._blob(store.g,claim,CLAIM,blob))
    B._closed(a, ('schema_version','kind','request_id','receipt_commit','claimed_at'))
    B._need(a['schema_version']==1 and a['kind']=='scheduled_basis_readiness_attempt'
            and a['request_id']==request_id and a['receipt_commit']==receipt_commit)
    return claim,B._stamp(a['claimed_at'])


def record_preproof(*,store,profile,repository,ready,anchored_workflow_blob,
                    ready_dispatched_at,request_id,clock):
    """Single create-only operation. A caller may act only on fresh success.

    On restart it must not convert read_preproof() into a capture permit. The
    existing slot/claim/debit CAS and inline request_guard remain necessary.
    """
    store.assert_locked()
    expected=ready[0]
    proof,job,expires=_validate_ready(store,profile,repository,ready,
        anchored_workflow_blob,ready_dispatched_at,clock())
    claim,claimed_at=_read_claim(store,expected,request_id,proof['receipt_commit'])
    B._need(claimed_at<=B._stamp(ready_dispatched_at))
    ref=_ref(expected,'proof')
    B._need(store.ref(ref) is None)
    record=dict(schema_version=2,kind='finite_hosted_v2_readiness_proof',
        request_id=request_id,compute_sha=expected.compute_sha,workflow_source_sha=expected.workflow_source_sha,
        workflow_blob=anchored_workflow_blob,readiness_commitment=expected.receipt_commitment,
        receipt_commit=proof['receipt_commit'],receipt_blob=proof['receipt_blob_sha'],
        run_id=expected.run_id,job_id=job,dispatch_at=ready_dispatched_at,expires=profile['expires'],
        claim_commit=claim)
    oid=store.commit(claim,{PATH:B.encode(record)},replace=set())
    B._need(clock()+RUNWAY<expires)
    store.cas(ref,oid,ZERO)
    B._need(read_preproof(store=store,expected=expected,expected_pin=oid,request_id=request_id)==record
            and clock()+RUNWAY<expires)
    return oid


def read_preproof(*,store,expected,expected_pin,request_id):
    """Immutable lineage readback; caller must independently authenticate Actions
    preproof again at reconciliation and attest final source/receipt separately.
    This intentionally returns no request permission.
    """
    store.assert_locked()
    oid=store.materialize_ref(_ref(expected,'proof'))
    B._need(oid==B._hex(expected_pin) and store.materialize_ref('refs/tags/market-data-read/'+oid)==oid)
    blob=store.g.normal_blob_identity(oid,PATH)
    value=B._parse(B._blob(store.g,oid,PATH,blob))
    B._closed(value, ('schema_version','kind','request_id','compute_sha','workflow_source_sha',
        'workflow_blob','readiness_commitment','receipt_commit','receipt_blob','run_id','job_id',
        'dispatch_at','expires','claim_commit'))
    B._need(value['schema_version']==2 and value['kind']=='finite_hosted_v2_readiness_proof' and
        (value['request_id'],value['run_id'],value['compute_sha'],value['workflow_source_sha'],
         value['readiness_commitment'])==(request_id,expected.run_id,expected.compute_sha,
             expected.workflow_source_sha,expected.receipt_commitment))
    claim,when=_read_claim(store,expected,request_id,value['receipt_commit'])
    B._need(value['claim_commit']==claim and when<=B._stamp(value['dispatch_at'])<B._stamp(value['expires']))
    store.g.single_addition(oid,claim,PATH,blob)
    return value
