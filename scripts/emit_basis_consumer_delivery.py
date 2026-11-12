"""Private Store Actions read-only receipt verification and private-log delivery.

No provider I/O, Store write, artifact upload, new secret or activation. Genuine
pinned private Actions job provenance, independently verified by the consumer,
attests this output. A marker copied into a Store file is not such provenance.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
import re
import subprocess
from scripts import provider_basis_snapshot as basis

MARKER='BASIS_PRIVATE_DELIVERY_V1 '
WORKFLOW='.github/workflows/basis-consumer-delivery.yml'


def materialize_local_receipt_ref(repo, commitment, compute_sha):
    """Actions fetch-depth:0 yields remote-tracking refs; create only a local ref.

    No fetch, remote update or durable Store write occurs. Conflicting local refs
    reject rather than being replaced. All receipt bytes still require the
    independently approved salted commitment in build_delivery.
    """
    basis._hex(commitment,64);basis._hex(compute_sha)
    g=basis._store(repo)
    suffix=f'basis-read-receipts/{compute_sha}/{commitment}'
    remote='refs/remotes/origin/'+suffix;local='refs/heads/'+suffix
    def identity(ref):
        raw=g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',ref)
        if not raw:return None
        match=re.fullmatch(re.escape(ref.encode())+rb' ([0-9a-f]{40}) commit \n',raw)
        basis._need(match);return match.group(1).decode()
    oid=identity(remote);basis._need(oid is not None)
    existing=identity(local)
    if existing is not None:
        basis._need(existing==oid);return
    g.commit(oid)
    result=subprocess.run(['git','-C',str(g.repo),'--no-replace-objects','-c','core.hooksPath=/dev/null',
        '-c','core.fsmonitor=false','update-ref',local,oid,'0'*40],env=g.env,capture_output=True,timeout=30)
    basis._need(result.returncode==0 and identity(local)==oid)


def build_delivery(repo, expected_commitment, expected_compute_sha, context, *, now):
    required={'repository','repository_id','owner_id','private','workflow_source_sha','run_id','run_attempt','event','actor_id','actor','triggering_actor','workflow_path','job'}
    basis._need(type(context) is dict and set(context)==required)
    basis._need(context['repository']=='yuatom/stock-market-data-store' and context['private'] is True
        and context['event']=='workflow_dispatch' and context['run_attempt']==1
        and context['actor_id']==context['owner_id'] and context['actor']==context['triggering_actor']=='yuatom'
        and context['workflow_path']==WORKFLOW and context['job']=='verify-basis-consumer')
    for key in ('repository_id','owner_id','run_id','actor_id'):
        basis._need(type(context[key]) is int and context[key]>0)
    basis._hex(context['workflow_source_sha'])
    materialize_local_receipt_ref(repo,expected_commitment,expected_compute_sha)
    selector,_=basis.load_consumer_receipt(repo,expected_commitment,expected_compute_sha,now=now)
    g=basis._store(repo)
    ref=f'refs/heads/basis-read-receipts/{expected_compute_sha}/{expected_commitment}'
    record=g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',ref)
    match=re.fullmatch(re.escape(ref.encode())+rb' ([0-9a-f]{40}) commit \n',record)
    basis._need(match);commit=match.group(1).decode()
    blob=g.normal_blob_identity(commit,'basis-authority/receipt.json')
    receipt=basis._receipt(g.object(blob,'blob',basis.MAX_BYTES))
    # Verify this exact second read too; a changed ref between reads is rejected.
    verified,_=basis.verify_consumer_receipt(g.object(blob,'blob',basis.MAX_BYTES),
        expected_commitment,expected_compute_sha,now=now)
    basis._need(verified==selector)
    return dict(schema_version=1,kind='private_basis_consumer_delivery',
        compute_sha=expected_compute_sha,receipt_commitment=expected_commitment,
        context=context,receipt_ref=ref,receipt_commit=commit,receipt_blob_sha=blob,
        selector=selector,verified_at=now,valid_until=receipt['valid_until'])


def encode_delivery(value):
    raw=basis.encode(value);basis._need(len(raw)<=16384)
    return MARKER+base64.b64encode(raw).decode('ascii')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--store',required=True);args=parser.parse_args()
    context=dict(repository=os.environ['GITHUB_REPOSITORY'],repository_id=int(os.environ['STORE_REPOSITORY_ID']),
        owner_id=int(os.environ['STORE_OWNER_ID']),private=os.environ['STORE_PRIVATE']=='true',
        workflow_source_sha=os.environ['GITHUB_SHA'],run_id=int(os.environ['GITHUB_RUN_ID']),
        run_attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),event=os.environ['GITHUB_EVENT_NAME'],
        actor_id=int(os.environ['GITHUB_ACTOR_ID']),actor=os.environ['GITHUB_ACTOR'],
        triggering_actor=os.environ['TRIGGERING_ACTOR'],workflow_path=WORKFLOW,job=os.environ['GITHUB_JOB'])
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    value=build_delivery(args.store,os.environ['RECEIPT_COMMITMENT'],os.environ['COMPUTE_SHA'],context,now=now)
    print(encode_delivery(value),flush=True)


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('PRIVATE_BASIS_DELIVERY_REJECTED') from None
