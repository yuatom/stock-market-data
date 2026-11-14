#!/usr/bin/env python3
"""Build a local-only XLC price candidate. No publication or provider transport."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path

import quarantine_retained_xlc_volume as source
import market_data_store as store
import rebase_market_data_baseline as rebase

BASE = 'b9afb1ef06abfce2577f213a148def687229a47a'
INDEX_BLOB = '304ecee15d4c6efce41866fbd31f1c534da9c3c5'
DATE = '2026-11-14'
NEW_ROW = dict(trade_date='2026-11-12',open=108.58,high=108.865,low=107.41,close=107.56,volume=4328600.0)
SERIES = 'series/daily/twelve_data_basic/XLC/'
AUDIT = 'collector-state/2026-11/2026-11-14-XLC-retained-price-candidate.json'
need = source.need


def candidate_rows(old, raw):
    source.validated_evidence(raw,old)
    retained=json.loads(raw)['retained_normalized_response']['records']
    incoming=[x for x in retained if x['trade_date']=='2026-11-12']
    need(incoming==[NEW_ROW])
    rows=copy.deepcopy(old)
    need(rows[-1]['trade_date']=='2026-11-11' and rows[-1]['high']==108.9681 and rows[-1]['volume']==5557979.)
    rows[-1]['high']=108.97
    rows.append(copy.deepcopy(incoming[0]))
    need(rows[:-2]==old[:-1] and rows[-2]['volume']==old[-1]['volume'])
    diffs=rebase.validate_replacement(old,rows,maximum_revised_overlap_records=1,revised_overlap_must_be_within_last_existing_sessions=1)
    need(len(diffs)==1 and diffs[0]['trade_date']=='2026-11-11' and diffs[0]['changed_fields']=={'high':{'old':108.9681,'new':108.97}})
    return rows,diffs


def build(repo,destination):
    repo=Path(repo).resolve();destination=Path(destination)
    need(not destination.exists())
    index_raw=source.git_bytes(repo,'show',f'{BASE}:{source.INDEX}')
    need(store.git_blob_sha_bytes(index_raw)==INDEX_BLOB)
    need(source.git(repo,'merge-base','--is-ancestor',source.SOURCE,BASE)=='')
    old_index=json.loads(index_raw)
    need(old_index['record_count']==318 and old_index['last_trade_date']=='2026-11-11')
    need(any(x.get('volume_policy')=='BLOCKED_UNRESOLVED' and x.get('policy_scope')=='entire_series' for x in old_index['lineage_segments']))
    old=[];old_files={'_index.json':index_raw}
    for sh in old_index['shards']:
        name=sh['path'];need('/' not in name and name.endswith('.json'))
        raw=source.git_bytes(repo,'show',f'{BASE}:{source.PREFIX}{SERIES}{name}')
        need(store.git_blob_sha_bytes(raw)==sh['blob_sha'])
        old_files[name]=raw;old.extend(json.loads(raw)['records'])
    raw=source.git_bytes(repo,'show',f'{source.SOURCE}:{source.SOURCE_PATH}')
    rows,diffs=candidate_rows(old,raw)
    destination.mkdir(parents=True)
    archive=rebase._archive_old_series(store_root=destination,trade_date=DATE,provider='twelve_data_basic',symbol='XLC',index=old_index,records=old,revisions=diffs,replacement_record_count=len(rows))
    archive_doc=json.loads(archive.read_bytes())
    archive_doc.update(reason='explicit_retained_evidence_high_only_correction',old_store_commit=BASE,
        source_commit=source.SOURCE,source_blob_sha=source.SOURCE_BLOB,source_sha256=source.SOURCE_HASH,
        old_files=[dict(path=SERIES+n,blob_sha=store.git_blob_sha_bytes(b),sha256=hashlib.sha256(b).hexdigest(),content_base64=base64.b64encode(b).decode()) for n,b in sorted(old_files.items())],
        retained_old_volume_policy='BLOCKED_UNRESOLVED_NOT_NEW_PROVIDER_VOLUME',provider_settlement_cause_established=False)
    store._write_json(archive,archive_doc)
    archive_rel=str(archive.relative_to(destination))
    index=copy.deepcopy(old_index)
    shards=rebase._build_shards(index['series_id'],rows,record_limit=int(index['shard_record_limit']),byte_limit=int(index['shard_byte_limit']))
    for name,doc,_meta in shards:store._write_json(destination/SERIES/name,doc)
    index.update(record_count=len(rows),last_trade_date='2026-11-12',shards=[meta for _,_,meta in shards])
    index['lineage_segments'] += [dict(ingest_kind='explicit_retained_price_correction_v1',start=317,end=317,
        corrected_trade_date='2026-11-11',changed_fields=['high'],recovery_archive_path=archive_rel,
        source_commit=source.SOURCE,source_blob_sha=source.SOURCE_BLOB,volume_policy='BLOCKED_UNRESOLVED',
        retained_old_volume_not_provider_revision=True),
        dict(ingest_kind='retained_provider_tail_append_v1',start=318,end=318,trade_date='2026-11-12',
             source_commit=source.SOURCE,source_blob_sha=source.SOURCE_BLOB,volume_policy='BLOCKED_UNRESOLVED')]
    store._write_json(destination/SERIES/'_index.json',index)
    audit=dict(schema_version=1,kind='local_retained_xlc_price_candidate',production_activation=False,
        symbol='XLC',base_store_commit=BASE,base_index_blob_sha=INDEX_BLOB,
        source_commit=source.SOURCE,source_path=source.SOURCE_PATH,source_blob_sha=source.SOURCE_BLOB,source_sha256=source.SOURCE_HASH,
        changed_old_fields=diffs,appended_dates=['2026-11-12'],old_volume_values_preserved=True,
        volume_policy='BLOCKED_UNRESOLVED',provider_settlement_cause_established=False,
        requests_attempted=0,additional_credits=0,archive_path=archive_rel)
    store._write_json(destination/AUDIT,audit)
    need(store.read_daily_series(destination,provider='twelve_data_basic',symbol='XLC')==rows)
    need(index['lineage_segments'][:len(old_index['lineage_segments'])]==old_index['lineage_segments'])
    for entry in archive_doc['old_files']:
        b=base64.b64decode(entry['content_base64'],validate=True)
        need(store.git_blob_sha_bytes(b)==entry['blob_sha'] and hashlib.sha256(b).hexdigest()==entry['sha256'])
    need(archive_doc['old_index']==old_index and archive_doc['old_records']==old)
    changed=[SERIES+n for n,_,_ in shards if (destination/SERIES/n).read_bytes()!=old_files.get(n)]
    changed += [SERIES+'_index.json',archive_rel,AUDIT]
    manifest=dict(schema_version=1,local_candidate_only=True,base_store_commit=BASE,
        changed_paths=sorted(changed),files=[dict(path=str(p.relative_to(destination)),blob_sha=store.git_blob_sha_bytes(p.read_bytes())) for p in sorted(destination.rglob('*.json'))])
    store._write_json(destination/'candidate-manifest.json',manifest)
    return audit


def main():
    p=argparse.ArgumentParser();p.add_argument('--store-repo',required=True);p.add_argument('--destination',required=True);a=p.parse_args()
    print(json.dumps(build(a.store_repo,a.destination)))

if __name__=='__main__':main()
