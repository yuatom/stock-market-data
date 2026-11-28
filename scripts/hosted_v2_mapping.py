"""Versioned hosted mapping consumer; legacy Mac mappings remain unchanged."""
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_delivery import DeliveryExpectation, WORKFLOW, _verify_private_delivery
from scripts.hosted_v2_durable_candidate import read_preproof, _check_source, RUNWAY

SIDE_PATH='basis-authority/hosted-delivery-proof.json'
V1_KEYS=('schema_version','kind','candidate','run_id','dispatched_at')
SIDE_KEYS=('schema_version','kind','mapping_commit','debit_head','preproof','final_job_id')
KEYS=('schema_version','kind','candidate','run_id','dispatched_at','preproof')
PRE_KEYS=('pin','commitment','run_id','request_id')
HOSTED='private_github_hosted_v1'


def inspect_hosted_mapping(updater, mapping, candidate, slot, readiness, scope_commitment):
    """Require the actual updater's independently validated durable debit."""
    B._closed(mapping,KEYS); B._closed(mapping['preproof'],PRE_KEYS)
    B._need(mapping['schema_version']==2 and mapping['kind']=='finite_hosted_daily_delivery_mapping')
    B._closed(readiness,('kind','workflow_blob','commitment','expires'))
    r=mapping['preproof'];g=updater.grant
    B._need(readiness['kind']==HOSTED and r['commitment']==readiness['commitment']
        and type(r['run_id']) is int and type(mapping['run_id']) is int
        and 0<r['run_id']!=mapping['run_id']>0 and r['commitment']!=candidate.receipt_commitment)
    expected=DeliveryExpectation(g['repository_id'],g['owner_id'],g['workflow_id'],g['workflow_source_sha'],
        g['compute_sha'],B._hex(r['commitment'],64),r['run_id'])
    B._need(expected.workflow_id==415302427)
    store=updater.store
    _check_source(store,expected,readiness['workflow_blob'])
    proof=read_preproof(store=store,expected=expected,expected_pin=r['pin'],request_id=r['request_id'])
    B._need(proof['workflow_blob']==readiness['workflow_blob'] and proof['expires']==readiness['expires'])
    _,anchor=store.g.commit(proof['claim_commit'])
    B._need(len(anchor)==1 and verify_independent_hosted_source(store,anchor[0])==proof['workflow_blob'])

    # This validates actual captured bytes, exact main debit CAS parent/budget,
    # original request/terminal and chronology, not just a claimed_at field.
    updater.validate_candidate(candidate)
    claim=B._parse(store.read(candidate.expected_head,updater._prefix(slot)+'/claim.json'))
    B._need(claim.get('hosted_binding')==dict(kind=HOSTED,scope_commitment=B._hex(scope_commitment,64),pin=r['pin']))
    debit=B._stamp(claim['claimed_at']);expires=B._stamp(proof['expires'])
    B._need(claim['request_id']==r['request_id'] and candidate.receipt_commit!=proof['receipt_commit']
        and candidate.receipt_blob_sha!=proof['receipt_blob']
        and B._stamp(proof['dispatch_at'])<=debit<=B._stamp(mapping['dispatched_at'])<expires
        and debit+RUNWAY<expires)
    return expected,proof,debit,expires,claim["previous_selector"]


def verify_historical_preproof(api,updater,repository,expected,proof,debit,prior_selector):
    """Historical qualification only; never grants a new live provider request."""
    base='/repos/yuatom/stock-market-data-store/actions'
    run=api.request('GET',f'{base}/runs/{expected.run_id}')
    jobs=api.request('GET',f'{base}/runs/{expected.run_id}/jobs?per_page=2')
    B._need(jobs.get('total_count')==1 and len(jobs.get('jobs',()))==1)
    job=jobs['jobs'][0]
    B._need(job['id']==proof['job_id'] and job['labels']==['ubuntu-latest']
        and B._stamp(proof['dispatch_at']).replace(microsecond=0)<=B._stamp(run['created_at'])
        <=B._stamp(run['run_started_at'])<=B._stamp(job['completed_at'])<=debit)
    value=_verify_private_delivery(expected,repository,run,jobs,api.job_log(job['id']),
        now=debit.isoformat(),calendar_authority={'verified_historical_debit':True})
    store=updater.store
    receipt=store.materialize_ref(value['receipt_ref'])
    B._need(receipt==value['receipt_commit']==proof['receipt_commit']
            and value['receipt_blob_sha']==proof['receipt_blob'])
    raw=B._blob(store.g,receipt,'basis-authority/receipt.json',proof['receipt_blob'])
    selector,_=B.verify_consumer_receipt(raw,expected.receipt_commitment,expected.compute_sha,now=debit.isoformat())
    B._need(selector==value['selector']
            and {k:v for k,v in selector.items() if k!='read_sha'}=={k:v for k,v in prior_selector.items() if k!='read_sha'}
            and B._receipt(raw)['valid_until']==value['valid_until']
            and B._stamp(proof['expires'])<=B._stamp(value['valid_until']))

def load_sidecar(store,updater,slot,mapping_commit,mapping,candidate):
    """Same V1 consumer artifact; hosted Compute requires the exact sidecar."""
    B._closed(mapping,V1_KEYS)
    B._need(mapping['schema_version']==1 and mapping['kind']=='finite_daily_delivery_mapping')
    ref=sidecar_ref(updater.digest,slot['slot_id'])
    oid=store.materialize_ref(ref)
    B._need(store.materialize_ref('refs/tags/market-data-read/'+oid)==oid)
    blob=store.g.normal_blob_identity(oid,SIDE_PATH)
    store.g.single_addition(oid,mapping_commit,SIDE_PATH,blob)
    value=B._parse(B._blob(store.g,oid,SIDE_PATH,blob))
    B._closed(value,SIDE_KEYS)
    B._need(value['schema_version']==1 and value['kind']=='finite_hosted_daily_delivery_sidecar'
        and value['mapping_commit']==mapping_commit and value['debit_head']==candidate.expected_head
        and type(value['final_job_id']) is int and value['final_job_id']>0)
    # Virtual normalized mapping separate from the legacy artifact, not
    # serialized to Store or passed to the report reader.
    return dict(mapping,schema_version=2,kind='finite_hosted_daily_delivery_mapping',
                preproof=value['preproof']),value['final_job_id']

def sidecar_ref(digest,slot_id):
    import re
    B._need(type(slot_id) is str and re.fullmatch('[A-Za-z0-9_-]{1,64}',slot_id))
    return f'refs/heads/basis-daily-hosted-deliveries/{B._hex(digest,64)}/{slot_id}'


def seal_verified_sidecar(store,updater,candidate,mapping_commit,mapping,preproof,final_job_id):
    """No second write on ambiguity; this proof is independent of legacy mapping."""
    B._need(type(final_job_id) is int and final_job_id>0)
    raw=B.encode(dict(schema_version=1,kind='finite_hosted_daily_delivery_sidecar',mapping_commit=mapping_commit,
        debit_head=candidate.expected_head,preproof=preproof,final_job_id=final_job_id))
    ref=sidecar_ref(updater.digest,candidate.slot_id)
    B._need(store.ref(ref) is None)
    oid=store.commit(mapping_commit,{SIDE_PATH:raw},replace=set())
    store.cas(ref,oid,'0'*40)
    normalized,pinned_job=load_sidecar(store,updater,{'slot_id':candidate.slot_id},mapping_commit,mapping,candidate)
    B._need(normalized['preproof']==preproof and pinned_job==final_job_id)

def verify_independent_hosted_source(store,head):
    """Exact authenticated default workflow bytes, with fixed runner/job rights."""
    import yaml
    path=WORKFLOW
    blob=store.g.normal_blob_identity(head,path)
    value=yaml.safe_load(B._blob(store.g,head,path,blob))
    B._need(type(value) is dict and value.get('permissions')=={'contents':'read'})
    jobs=value.get('jobs')
    B._need(type(jobs) is dict and set(jobs)=={'verify-basis-consumer'})
    job=jobs['verify-basis-consumer']
    B._need(type(job) is dict and job.get('runs-on')=='ubuntu-latest'
        and job.get('permissions',{'contents':'read'})=={'contents':'read'}
        and 'uses' not in job and 'strategy' not in job)
    return blob
