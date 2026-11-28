"""Private Store read-only relay for an independently approved PRE backup.

There is no GitHub API, supplier call, credential, Store update, or public
producer log access. Any consumer must separately authenticate originating
public producer metadata and private verifier run/job/log visibility.
"""
import argparse
import base64
from datetime import datetime,timedelta,timezone
import hashlib,hmac,os,re
from pathlib import Path
from scripts import provider_basis_snapshot as basis
from scripts import retained_price_only_slots as slots
from scripts import retained_price_only_source_policy as eligibility
from scripts.push_proof_store_commit import verify_exact_output_closure
from scripts.reviewed_equity_calendar import scheduled_close

STORE='yuatom/stock-market-data-store'
COMPUTE='yuatom/stock-market-data'
WORKFLOW='.github/workflows/basis-consumer-delivery.yml'
PRODUCER_WORKFLOW='.github/workflows/reconcile-retained-daily.yml'
JOB='verify-basis-consumer'
MARKER='RETAINED_PRIVATE_PRE_BACKUP_DELIVERY_V1 '
DOMAIN=b'RETAINED-PRIVATE-PRE-BACKUP-READ-RECEIPT-V1\0'
RECEIPT='basis-authority/retained-pre-backup-read-receipt.json'

class DeliveryHold(ValueError):pass

def need(x):
    if not x:raise DeliveryHold('RETAINED_PRIVATE_PRE_BACKUP_DELIVERY_HOLD')

def ident(x):return type(x) is int and x>0

def commitment(raw):return hashlib.sha256(DOMAIN+raw).hexdigest()

def refs(sha,approval):
    need(slots.oid(sha) and type(approval) is str and re.fullmatch('[0-9a-f]{64}',approval))
    suffix=f'/{sha}/{approval}'
    prefix='refs/remotes/origin/'
    return prefix+'basis-read-receipts'+suffix,prefix+'retained-pre-backup-read-receipts'+suffix

def read_ref(g,ref):
    raw=g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',ref)
    if not raw:return None
    match=re.fullmatch(re.escape(ref.encode())+rb' ([0-9a-f]{40}) commit \n',raw)
    need(match);return match[1].decode()

def resolve_purpose(store,sha,approval):
    native,alternate=refs(sha,approval)
    g=basis._store(store) if store is not None else None
    n=read_ref(g,native);b=read_ref(g,alternate)
    need(bool(n)^bool(b))
    return 'basis_native' if n else 'retained_pre_backup'

def bound_receipt(raw,approval,compute_sha,delivery,now):
    try:
        need(slots.oid(compute_sha) and type(raw) is bytes and 0<len(raw)<=16384 and
             hmac.compare_digest(commitment(raw),approval))
        need(type(delivery) is dict and set(delivery)=={'repository','repository_id','owner_id','actor_id',
             'run_id','run_attempt','workflow_path','logical_job','workflow_source_sha'})
        need(delivery['repository']==STORE and delivery['owner_id']==delivery['actor_id'] and
             all(ident(delivery[k]) for k in ('repository_id','owner_id','actor_id','run_id','run_attempt')) and
             delivery['run_attempt']==1 and delivery['workflow_path']==WORKFLOW and
             delivery['logical_job']==JOB and slots.oid(delivery['workflow_source_sha']))
        value=slots.parse(raw)
        need(type(value) is dict and set(value)=={'schema_version','kind','purpose','compute_sha','workflow_source_sha',
             'nonce','valid_from','valid_until','producer','artifact'})
        need(type(value['schema_version']) is int and value['schema_version']==1 and
             value['kind']=='private_retained_pre_backup_read_receipt' and value['purpose']=='prior_session_close_only' and
             value['compute_sha']==compute_sha and value['workflow_source_sha']==delivery['workflow_source_sha'] and
             type(value['nonce']) is str and re.fullmatch('[0-9a-f]{64}',value['nonce']) and value['nonce']!='0'*64)
        start,end=map(slots.stamp,(value['valid_from'],value['valid_until']))
        need(start<=slots.stamp(now)<end<=start+timedelta(hours=24))
        producer=value['producer'];a=value['artifact']
        need(type(producer) is dict and set(producer)=={'repository','repository_id','owner_id','workflow_id',
            'workflow_path','job_id','actor_id','run_id','run_attempt','source_sha'})
        need(producer['repository']==COMPUTE and producer['workflow_path']==PRODUCER_WORKFLOW and
             producer['source_sha']==compute_sha and producer['owner_id']==producer['actor_id']==delivery['owner_id'] and
             all(ident(producer[k]) for k in ('repository_id','workflow_id','job_id','owner_id','actor_id','run_id','run_attempt'))
             and producer['run_attempt']==1)
        need(type(a) is dict and set(a)=={'compute_sha','policy_blob','policy_sha256','published_store_sha','previous_session',
             'claim_commit','claim_blob','run_id','receipt_commit','source_commit','receipt_commitment','sources'})
        need(a['compute_sha']==compute_sha and a['run_id']==str(producer['run_id']) and slots.day(a['previous_session']) and
             all(slots.oid(a[k]) for k in ('policy_blob','published_store_sha','claim_commit','claim_blob','receipt_commit','source_commit')) and
             all(type(a[k]) is str and re.fullmatch('[0-9a-f]{64}',a[k]) for k in ('policy_sha256','receipt_commitment')) and
             type(a['sources']) is list and len(a['sources'])==len(set(a['sources']))==2 and
             all(type(s) is str and re.fullmatch('[a-z][a-z0-9_]{0,63}',s) for s in a['sources']))
        return value
    except Exception:
        raise DeliveryHold('RETAINED_PRIVATE_PRE_BACKUP_DELIVERY_HOLD') from None

def read_receipt(store,compute_sha,approval,delivery,now):
    g=basis._store(store)
    old,ref=refs(compute_sha,approval)
    commit=read_ref(g,ref)
    need(commit is not None and read_ref(g,old) is None)
    raw=basis._blob(g,commit,RECEIPT)
    receipt=bound_receipt(raw,approval,compute_sha,delivery,now)
    parent=receipt['artifact']['published_store_sha']
    need(g.commit(commit)[1]==(parent,))
    delta=g.read('diff-tree','-z','-r','--no-renames','--name-status',parent,commit)
    need(delta==b'A\0'+RECEIPT.encode()+b'\0' and read_ref(g,ref)==commit)
    return receipt

def verify_artifact(compute,store,receipt,now):
    a=receipt['artifact'];c=a['published_store_sha']
    need(slots.val(compute,'rev-parse','HEAD')==a['compute_sha'])
    actual=slots.read(compute,a['compute_sha'],eligibility.POLICY_PATH)
    need(actual is not None and slots.sha(actual)==a['policy_sha256'] and slots.blob(actual)==a['policy_blob'])
    profiles=eligibility.load_policy(actual)
    g=basis._store(store)
    need(read_ref(g,slots.TAG+c)==c)
    identity=dict(mic='XNMS',symbol='SOXX',asset_class='etf',currency='USD',session='regular',
                  basis='split_adjusted_price_only',trade_date=a['previous_session'])
    claim_ref=slots.slot_key(identity).replace('refs/heads/','refs/remotes/origin/',1)
    need(read_ref(g,claim_ref)==a['claim_commit'])
    claim=slots.parse(basis._blob(g,a['claim_commit'],'claim.json',a['claim_blob']))
    proof=claim['publication_proof']
    need(claim['kind']=='immutable_reusable_price_target' and claim['identity']==identity and claim['market_commit']==c and
        proof=={k:a[k] for k in ('compute_sha','run_id','receipt_commit','receipt_commitment','source_commit')})
    row_path=slots.ROOT+slots.record_path(identity)
    row_raw=basis._blob(g,c,row_path)
    row=slots.parse(row_raw)
    need(slots.key(row)==identity and row['kind']=='admitted_regular_daily_price_backup' and
         row['native_provider_receipt'] is False and row['purpose']=='prior_session_close_only' and
         slots.stamp(row['admitted_at'])<=slots.stamp(now) and type(row['evidence']) is list and len(row['evidence'])==2)
    evidence={};source_urls={}
    for ref in row['evidence']:
        need(ref['path']==slots.evidence_path(row,ref['source_id']) and ref['source_id'] not in source_urls)
        path=slots.ROOT+ref['path'];raw=basis._blob(g,c,path,ref['blob_sha'])
        need(slots.sha(raw)==ref['sha256'])
        obs=slots.parse(raw)
        need(obs['kind']=='licensed_regular_daily_price_observation')
        evidence[ref['path']]=raw;source_urls[ref['source_id']]=obs['url']
    need(set(source_urls)==set(a['sources']))
    eligibility.select(profiles,source_urls,now=slots.stamp(now))
    exact=slots._verify_package(row,evidence,source_urls,{row['trade_date']:scheduled_close(row['trade_date'])},{},
                               'licensed_regular_daily_price_observation')
    need(claim['fact_blobs']=={p:slots.blob(raw) for p,raw in exact.items()})
    _,parents=g.commit(c);need(len(parents)==1)
    verify_exact_output_closure(store,parents[0],c,claim['manifest'])
    need(read_ref(g,slots.TAG+c)==c and read_ref(g,claim_ref)==a['claim_commit'])

def build_delivery(*,compute,store,compute_sha,approval,delivery,now):
    receipt=read_receipt(store,compute_sha,approval,delivery,now)
    verify_artifact(compute,store,receipt,now)
    payload=slots.encode(dict(schema_version=1,kind='private_retained_prior_close_backup_delivery',
        purpose='prior_session_close_only',stage='premarket',symbol='SOXX',verified_at=now,
        valid_until=receipt['valid_until'],producer=receipt['producer'],delivery=delivery,artifact=receipt['artifact']))
    need(len(payload)<=16384)
    return MARKER+base64.b64encode(payload).decode('ascii')

def _utc():return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

def main():
    try:
        parser=argparse.ArgumentParser();parser.add_argument('--store',type=Path,required=True)
        parser.add_argument('--compute',type=Path,required=True);args=parser.parse_args()
        e=os.environ;head=e['GITHUB_SHA'];sha=e['COMPUTE_SHA'];approval=e['RECEIPT_COMMITMENT']
        ref='refs/heads/basis-consumer-delivery/'+head
        need(e['GITHUB_REPOSITORY']==STORE and e['STORE_PRIVATE']=='true' and e['GITHUB_ACTIONS']=='true'
             and e['GITHUB_EVENT_NAME']=='workflow_dispatch' and e['GITHUB_RUN_ATTEMPT']=='1' and
             e['GITHUB_JOB']==JOB and e['GITHUB_REF']==ref and e['GITHUB_WORKFLOW_REF']==STORE+'/'+WORKFLOW+'@'+ref
             and e['GITHUB_ACTOR']==e['TRIGGERING_ACTOR']==e['EXPECTED_ACTOR']=='yuatom'
             and e['GITHUB_ACTOR_ID']==e['STORE_OWNER_ID'])
        delivery=dict(repository=STORE,repository_id=int(e['STORE_REPOSITORY_ID']),owner_id=int(e['STORE_OWNER_ID']),
                      actor_id=int(e['GITHUB_ACTOR_ID']),run_id=int(e['GITHUB_RUN_ID']),run_attempt=1,
                      workflow_path=WORKFLOW,logical_job=JOB,workflow_source_sha=head)
        line=build_delivery(compute=args.compute,store=args.store,compute_sha=sha,
                            approval=approval,delivery=delivery,now=_utc())
        # A slow cold private read must not emit outside the capsule's expiry.
        current=_utc()
        # Bind expiry to a fresh actual clock and independently verified payload
        # immediately before output; no result exposure on failed check.
        value=slots.parse(base64.b64decode(line.removeprefix(MARKER),validate=True))
        need(slots.stamp(value['verified_at'])<=slots.stamp(current)<slots.stamp(value['valid_until']))
        print(line,flush=True)
        return 0
    except Exception:
        print('RETAINED_PRIVATE_PRE_BACKUP_DELIVERY_HOLD')
        return 1

if __name__=='__main__':raise SystemExit(main())
