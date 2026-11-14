#!/usr/bin/env python3
"""One-time owner-authorized XLC metadata quarantine; no provider transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import backfill_market_data_tail as tail
import market_data_store as store
import rebase_market_data_baseline as rebaser
from build_market_data_inventory import build_inventory
from verify_store_publication import verify_publication

BASE = '71cb9ea541bae007c0e2b03d35906d376b2b3c0a'
SOURCE = '23f4392999b912c986424200d7bbbfa824a5885e'
SOURCE_PATH = 'data/market-data/collector-state/2026-11/2026-11-14-daily-history-tail-backfill-43594816095-1-XLC.json'
SOURCE_BLOB = 'aa00f0fe5501ab540648907b04021b34c69ff69b'
SOURCE_HASH = '730b7a767cef30652a8281056ef7b46a0540b6b2f076a4d946277a7ce7b06ae6'
INDEX_BLOB = 'b1473abec40b2e4f1c56b384a54862d70733a426'
PREFIX = 'data/market-data/'
INDEX = PREFIX + 'series/daily/twelve_data_basic/XLC/_index.json'
REPORT = PREFIX + 'collector-state/2026-11/2026-11-14-XLC-retained-volume-quarantine.json'
MANIFEST = PREFIX + 'collector-state/2026-11/2026-11-14-XLC-retained-volume-quarantine-manifest.json'
DAY = '2026-11-14'
MODE = 'retained_xlc_volume_quarantine'
H = SimpleNamespace(store=store, rebaser=rebaser, collectors=SimpleNamespace(TWELVE_PROVIDER='twelve_data_basic'))


def need(condition):
    if not condition:
        raise ValueError('XLC_QUARANTINE_REJECTED')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, timeout=30).decode().strip()


def git_bytes(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, timeout=30)


def validated_evidence(raw, old):
    need(store.git_blob_sha_bytes(raw) == SOURCE_BLOB and hashlib.sha256(raw).hexdigest() == SOURCE_HASH)
    report = json.loads(raw)
    need(report['status'] == 'RESPONSE_RETAINED_UNQUALIFIED' and report['request_id'] == '43594816095-1'
         and report['compute_sha'] == '65abc29a193f9944d032bb888d018547f84e9948'
         and report['requests_attempted'] == report['budget_debits'] == 1)
    retained = report['retained_normalized_response']
    need(retained['symbol'] == 'XLC' and retained['authority'] == 'normalized_not_raw_http')
    rows = retained['records']
    need(len(rows) == 463 and len(old) == 318 and old[-1]['trade_date'] == '2026-11-11')
    by_date = {r['trade_date']:r for r in rows}
    need(len(by_date) == len(rows) and '2026-11-12' in by_date)
    diffs = []
    for prior in old:
        need(prior['trade_date'] in by_date)
        new = by_date[prior['trade_date']]
        changed = {k: [prior[k], new[k]] for k in ('open', 'high', 'low', 'close', 'volume') if prior[k] != new[k]}
        if changed:
            diffs.append(dict(date=prior['trade_date'], changes=changed))
    need(diffs == [dict(date='2026-11-11', changes={'high':[108.9681,108.97], 'volume':[5557979.0,5558000.0]})])
    evidence = tail.volume_conflict_evidence(H, dict(symbol='XLC', existing=old), rows, '2026-11-12')
    need(evidence is not None)
    return evidence


def prepare(repo, request_id):
    """Local candidate only. Caller must supply an exact clean detached BASE."""
    repo = Path(repo)
    need(git(repo,'rev-parse','HEAD') == BASE and not git(repo,'status','--porcelain'))
    need(not (repo/REPORT).exists() and not (repo/MANIFEST).exists())
    need(git(repo,'rev-parse',f'{BASE}:{INDEX}') == INDEX_BLOB)
    need(git(repo,'merge-base','--is-ancestor',SOURCE,BASE) == '')
    raw = git_bytes(repo,'show',f'{SOURCE}:{SOURCE_PATH}')
    old = store.read_daily_series(repo/PREFIX, provider='twelve_data_basic', symbol='XLC')
    evidence = validated_evidence(raw, old)
    index = json.loads((repo/INDEX).read_bytes())
    need(not any(s.get('volume_policy') == 'BLOCKED_UNRESOLVED' or s.get('ingest_kind') == 'collector_price_only_revision_v1' for s in index.get('lineage_segments',[])))
    shard_paths = {PREFIX+'series/daily/twelve_data_basic/XLC/'+s['path'] for s in index['shards']}
    shard_bytes = {p:(repo/p).read_bytes() for p in shard_paths}
    outputs, marker = tail.stage_volume_quarantine(H, repo, dict(symbol='XLC',existing=old,index_relative=INDEX), evidence, DAY)
    need(marker['annotation_added'] is True)
    inventory = PREFIX+'collector-state/market-data-inventory.json'
    store._write_json(repo/inventory,build_inventory(repo/PREFIX))
    audit = dict(schema_version=1,kind=MODE,status='VOLUME_QUARANTINED_PRICE_REJECTED',request_id=request_id,
        trade_date=DAY,symbol='XLC',requests_attempted=0,additional_credits=0,price_changes=0,
        source_commit=SOURCE,source_path=SOURCE_PATH,source_blob_sha=SOURCE_BLOB,source_sha256=SOURCE_HASH,
        source_request_id='43594816095-1',base_store_sha=BASE,base_index_blob_sha=INDEX_BLOB,
        volume_quarantine=marker,price_last_trade_date='2026-11-11',production_rebase=False)
    store._write_json(repo/REPORT,audit)
    outputs.update((inventory,REPORT))
    required = [dict(path=p[len(PREFIX):],blob_sha=git(repo,'hash-object','--',str(repo/p))) for p in sorted(outputs)]
    store._write_json(repo/MANIFEST,dict(schema_version=1,request_id=request_id,trade_date=DAY,stage=MODE,required_outputs=required))
    allowed = outputs|{MANIFEST}
    need(store.read_daily_series(repo/PREFIX,provider='twelve_data_basic',symbol='XLC') == old)
    need(all((repo/p).read_bytes()==raw for p,raw in shard_bytes.items()))
    changed=set(git(repo,'diff','--name-only').splitlines())|set(git(repo,'ls-files','--others','--exclude-standard').splitlines())
    need(changed==allowed)
    return allowed


def execute(repo, compute, compute_sha, request_id):
    need(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REPOSITORY')=='yuatom/stock-market-data'
         and os.environ.get('GITHUB_REF')=='refs/heads/main' and os.environ.get('GITHUB_SHA')==compute_sha
         and os.environ.get('GITHUB_WORKFLOW')=='Retained XLC Volume Quarantine'
         and os.environ.get('GITHUB_RUN_ATTEMPT')=='1'
         and request_id == os.environ.get('GITHUB_RUN_ID','')+'-1'
         and re.fullmatch(r'[0-9a-f]{40}',compute_sha))
    repo=Path(repo).resolve();compute=Path(compute).resolve()
    need(git(compute,'rev-parse','HEAD')==compute_sha)
    for checkout,name in ((repo,'stock-market-data-store'),(compute,'stock-market-data')):
        origin=git(checkout,'remote','get-url','origin')
        need(origin in (f'https://github.com/yuatom/{name}',f'https://github.com/yuatom/{name}.git',f'git@github.com:yuatom/{name}.git'))
    need(git(repo,'ls-remote','origin','refs/heads/main').split()==[BASE,'refs/heads/main'])
    need(git(compute,'ls-remote','origin','refs/heads/main').split()==[compute_sha,'refs/heads/main'])
    allowed=prepare(repo,request_id)
    git(repo,'add','--',*sorted(allowed))
    git(repo,'-c','user.name=stock-market-data-bot','-c','user.email=stock-market-data-bot@users.noreply.github.com','commit','--no-gpg-sign','-m','data: quarantine retained XLC volume conflict without collection')
    candidate=git(repo,'rev-parse','HEAD')
    need(git(repo,'rev-parse','HEAD^')==BASE)
    need(git(repo,'ls-remote','origin','refs/heads/main').split()==[BASE,'refs/heads/main'])
    # Exactly one push. Any failure/unknown result escapes; never retry this producer.
    git(repo,'push','origin',f'{candidate}:refs/heads/main')
    receipt=verify_publication(repo,candidate,required_manifest=MANIFEST)
    return dict(status='VOLUME_QUARANTINE_DURABLE',requests_attempted=0,additional_credits=0,price_changes=0,receipt=receipt)


def main():
    p=argparse.ArgumentParser();p.add_argument('--store-repo',required=True);p.add_argument('--compute-sha',required=True);p.add_argument('--request-id',required=True);a=p.parse_args()
    try:
        result=execute(a.store_repo,Path(__file__).resolve().parents[1],a.compute_sha,a.request_id)
    except Exception:
        print(json.dumps(dict(status='STOPPED_UNCONFIRMED_OR_REJECTED',automatic_retry_allowed=False)))
        return 1
    print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
