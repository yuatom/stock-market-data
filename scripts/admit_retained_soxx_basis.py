#!/usr/bin/env python3
"""One-time retained SOXX admission and private receipt; no provider transport.

Historical price-only basis through 2026-11-11, not permission to shorten a
current report's required calendar or to use volume. Any unknown write stops.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from scripts import provider_basis_snapshot as B
from scripts.daily_basis_remote import RemoteGitStore
from scripts.prepare_retained_soxx_admission import DIAGNOSTIC_PATH, MANIFEST_PATH

BASE = 'd574510647ad8c1cf2f1786e700a39b4e24c2a8d'
WORKFLOW = 'Retained SOXX Initial Price Admission'
ZERO = '0' * 40


def need(value):
    if not value:
        raise ValueError('SOXX_RETAINED_ADMISSION_REJECTED')


def publish_prepared(store, prepared, *, expected_head, compute_sha, now, progress):
    """Caller holds the shared writer lock; prepared objects grant no authority."""
    B._hex(compute_sha); B._hex(expected_head)
    need(now.tzinfo is not None)
    store.assert_locked()
    need(store.head() == expected_head)
    catalog = prepared['catalog']; snapshot = prepared['snapshot']
    paths = {B.catalog_path(B.PROVIDER, 'SOXX'),
             B.snapshot_path(B.PROVIDER, 'SOXX', prepared['basis_id']),
             B.admission_path(B.PROVIDER, 'SOXX', catalog['admission_blob_sha']),
             DIAGNOSTIC_PATH, MANIFEST_PATH}
    updates = prepared['updates']
    need(set(updates) == paths and catalog['basis_id'] == prepared['basis_id'])
    need(updates[B.catalog_path(B.PROVIDER,'SOXX')] == B.encode(catalog))
    snapshot_raw=updates[B.snapshot_path(B.PROVIDER,'SOXX',prepared['basis_id'])]
    admission_raw=updates[B.admission_path(B.PROVIDER,'SOXX',catalog['admission_blob_sha'])]
    need(snapshot_raw == B.encode(snapshot) and B.blob_sha(snapshot_raw) == catalog['snapshot_blob_sha'])
    need(B.blob_sha(admission_raw) == catalog['admission_blob_sha'])
    need(B.sha(updates[DIAGNOSTIC_PATH]) == snapshot['baseline_evidence_sha256'])
    manifest=B._parse(updates[MANIFEST_PATH])
    need(manifest == prepared['reconstruction_audit'] and manifest['new_basis_id'] == prepared['basis_id']
         and manifest['original_diagnostic_raw_recovered'] is False)
    need(manifest['objects'] == [dict(path=p,blob_sha=B.blob_sha(raw),sha256=B.sha(raw))
        for p,raw in sorted(updates.items()) if p != MANIFEST_PATH])
    need(all(not store.exists(expected_head,p) for p in paths))
    need(snapshot['records'] and snapshot['records'][-1]['datetime'] == '2026-11-11')
    dates = [r['datetime'] for r in snapshot['records']]
    need(len(dates) == 318)
    candidate = store.commit(expected_head, updates, replace=set())
    selector = dict(catalog, read_sha=candidate)
    authority = B.ReadAuthority(B.sha(B.encode(selector)), selector['admission_blob_sha'])
    view = B.read_price_view(store.repo, selector, authority, fields=B.PRICE_FIELDS,
                            required_sessions=dates, knowledge_cutoff=now.isoformat())
    need(view['record_count'] == 318 and view['volume_status'] == 'BLOCKED_UNRESOLVED')
    receipt_raw = B.encode(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=compute_sha,
        selector=selector,admission_blob_sha=selector['admission_blob_sha'],nonce=secrets.token_hex(32),
        valid_from=now.isoformat(),valid_until=(now+timedelta(hours=24)).isoformat()))
    commitment = B.receipt_commitment(receipt_raw)
    verified, _ = B.verify_consumer_receipt(receipt_raw,commitment,compute_sha,now=now.isoformat())
    need(verified == selector)
    receipt_ref = f'refs/heads/basis-read-receipts/{compute_sha}/{commitment}'
    receipt_path = 'basis-authority/receipt.json'
    receipt_commit = store.commit(candidate,{receipt_path:receipt_raw},
        replace={receipt_path} if store.exists(candidate,receipt_path) else set())
    need(store.ref(receipt_ref) is None and store.head() == expected_head)
    # Main is a direct-child CAS; receipt is create-only. Adapter couples each
    # push atomically to its exact durability tag and rejects uncertain writes.
    progress.update(stage='MAIN_PUBLICATION_ATTEMPTED',candidate_commit=candidate,
                    receipt_commit=receipt_commit,receipt_commitment=commitment)
    store.cas('refs/heads/main',candidate,expected_head)
    progress['stage']='MAIN_DURABLE_RECEIPT_NOT_YET_PUBLISHED'
    store.cas(receipt_ref,receipt_commit,ZERO)
    progress['stage']='MAIN_AND_RECEIPT_DURABLE'
    need(store.head() == candidate and store.ref(receipt_ref) == receipt_commit)
    stored_raw = store.read(receipt_commit,receipt_path)
    need(stored_raw == receipt_raw)
    verified, _ = B.verify_consumer_receipt(stored_raw,commitment,compute_sha,now=now.isoformat())
    need(verified == selector)
    return dict(status='HISTORICAL_PRICE_BASIS_AND_RECEIPT_DURABLE',selector=selector,
        receipt_commitment=commitment,receipt_ref=receipt_ref,receipt_commit=receipt_commit,
        receipt_blob_sha=B.blob_sha(receipt_raw),valid_until=(now+timedelta(hours=24)).isoformat(),
        requests_attempted=0,additional_credits=0,record_count=318,last_trade_date='2026-11-11',
        volume_policy='BLOCKED_UNRESOLVED',current_report_readiness=False,
        consumer_verification_pending=True,automatic_retry_allowed=False,
        reconstruction_audit=prepared.get('reconstruction_audit'))


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.PIPE,timeout=30).decode().strip()


def execute(repo,compute_sha,progress):
    from scripts.prepare_retained_soxx_admission import prepare_from_store
    compute=Path(__file__).resolve().parents[1]
    def platform():
        need(os.environ.get('GITHUB_ACTIONS')=='true'
             and os.environ.get('GITHUB_REPOSITORY')=='yuatom/stock-market-data'
             and os.environ.get('GITHUB_REF')=='refs/heads/main'
             and os.environ.get('GITHUB_SHA')==compute_sha
             and os.environ.get('GITHUB_WORKFLOW')==WORKFLOW
             and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
             and os.environ.get('GITHUB_RUN_ATTEMPT')=='1'
             and os.environ.get('GITHUB_ACTOR')=='yuatom'
             and os.environ.get('GITHUB_TRIGGERING_ACTOR')=='yuatom')
    def source():
        need(re.fullmatch('[0-9a-f]{40}',compute_sha))
        need(git(compute,'rev-parse','HEAD')==compute_sha)
        need(git(compute,'remote','get-url','origin') in ('https://github.com/yuatom/stock-market-data','https://github.com/yuatom/stock-market-data.git'))
        need(git(compute,'ls-remote','origin','refs/heads/main').split()==[compute_sha,'refs/heads/main'])
    platform();source()
    token=os.environ.get('MARKET_DATA_STORE_TOKEN','')
    store=RemoteGitStore(Path(repo).resolve(),token,source_recheck=source,platform_guard=platform)
    with store.lock():
        need(store.head()==BASE)
        now=datetime.now(timezone.utc)
        prepared=prepare_from_store(store,admitted_at=now.isoformat())
        return publish_prepared(store,prepared,expected_head=BASE,compute_sha=compute_sha,now=now,progress=progress)


def main():
    p=argparse.ArgumentParser();p.add_argument('--store-repo',required=True);p.add_argument('--compute-sha',required=True);a=p.parse_args()
    progress={'stage':'NOT_PUBLISHED'}
    try:
        result=execute(a.store_repo,a.compute_sha,progress)
    except Exception:
        print(json.dumps(dict(status='STOPPED_INSPECT_NO_RETRY',progress=progress,automatic_retry_allowed=False)))
        return 1
    print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
