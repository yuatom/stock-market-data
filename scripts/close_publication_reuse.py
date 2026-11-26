"""Read-only deduplication proof for the original scheduled early Close."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from build_deterministic_metric_proof import INTRADAY_PRICE_SOURCES, verify_existing_proof
from close_session_window import candidate_close_window
from collection_universe import context_by_symbol, load_collection_universe, sector_symbols
from collect_market_data import _live_close_universe
from market_data_collection import _full_window_from_one_provider
from reviewed_equity_calendar import scheduled_close
from verify_store_publication import _git, _one_remote_ref, _verify_required_manifest

DATA='data/market-data/'
ET=ZoneInfo('America/New_York')
HEX40=re.compile('[0-9a-f]{40}')
MAX_MANIFESTS=48
FEEDS={'nasdaq_public_intraday':'nasdaq_public_chart_last_sale_volume_v1',
       'twelve_data_basic':'twelve_data_basic_us_equities_limited_realtime_coverage'}


class ReuseEvidenceError(RuntimeError):
    pass


def _require(ok):
    if not ok:raise ReuseEvidenceError('EARLY_CLOSE_REUSE_EVIDENCE_UNCONFIRMED')


def _sha(v):
    _require(type(v) is str and HEX40.fullmatch(v))
    return v


def _time(v):
    _require(type(v) is str)
    t=datetime.fromisoformat(v.replace('Z','+00:00'))
    _require(t.tzinfo is not None)
    return t


def _pairs(rows):
    keys={}
    for key,v in rows:
        _require(key not in keys); keys[key]=v
    return keys


def _file(repo, pin, rel, *, optional=False, expected=None):
    _require(rel.startswith(DATA) and re.fullmatch('[A-Za-z0-9._/-]+',rel) and
             all(p not in ('','.','..') for p in rel.split('/')))
    row=_git(repo,'ls-tree',pin,'--',rel).stdout.strip()
    if not row and optional:return None
    m=re.fullmatch(r'100644 blob ([0-9a-f]{40})\t'+re.escape(rel),row)
    _require(m and (expected is None or _sha(expected)==m[1]))
    _require(0<int(_git(repo,'cat-file','-s',m[1]).stdout)<8*1024*1024)
    d=json.loads(_git(repo,'cat-file','blob',m[1]).stdout,object_pairs_hook=_pairs)
    _require(type(d) is dict)
    return d,m[1]


def _covers(repo,pin,day,window,snapshot,universe,close,instant):
    refs=snapshot.get('data_refs')
    missing=snapshot.get('missing')
    _require(type(refs) is list and len(refs)<=256 and type(missing) is list)
    if missing:return None
    eligible=dict(_live_close_universe(universe)); contexts=context_by_symbol(universe)
    sectors=set(sector_symbols(universe))
    _require(eligible)
    target=f'{window["start"]}-{window["end"]}'
    stem=f'sessions/{day[:7]}/{day}/regular/{target.replace(":","")}/'
    sources={}
    for ref in refs:
        _require(type(ref) is dict and type(ref.get('window')) is str)
        if ref['window']!=target:continue
        symbol=ref.get('symbol'); rel=ref.get('path'); provider=ref.get('provider')
        _require(type(symbol) is str and symbol in eligible and type(provider) is str and provider in FEEDS
            and type(rel) is str and rel.startswith(stem+provider+'/'))
        kind='cross_asset_proxy_capture' if symbol in contexts else 'sector_context_capture' if symbol in sectors else 'regular_intraday_capture'
        _require(ref.get('kind')==kind and ref.get('reason_class')=='success')
        item,blob=_file(repo,pin,DATA+rel,expected=_sha(ref.get('blob_sha')))
        _require(item.get('trade_date')==day and item.get('session')=='regular'
                 and item.get('provider')==provider and item.get('window')==window
                 and item.get('feed_scope')==FEEDS[provider] and close<=_time(item.get('generated_at'))<=instant)
        facts=item.get('qualified_facts')
        _require(type(facts) is list and all(type(f) is dict and f.get('symbol')==symbol
            and f.get('asset_class')==eligible[symbol] and f.get('session')=='regular'
            and (provider,f.get('source_contract')) in INTRADAY_PRICE_SOURCES
            and f.get('market_context')==contexts.get(symbol) for f in facts))
        if not _full_window_from_one_provider(facts,day,window['start'],window['end']):continue
        # Keep all complete capture identities; the prior publication must
        # independently cover at least one coherent item for each symbol.
        sources.setdefault(symbol,set()).add((DATA+rel,blob))
    return sources if set(sources)==set(eligible) else None


def _record(repo,pin,day,required,sources):
    parent=f'{DATA}collector-state/{day[:7]}'
    pat=re.compile(re.escape(parent+'/'+day)+r'-close-[0-9a-f]{16}[-]publication\.json')
    out=_git(repo,'ls-tree','-r','--name-only',pin,'--',parent).stdout.splitlines()
    possible=[p for p in out if pat.fullmatch(p)]
    _require(len(possible)<=MAX_MANIFESTS)
    accepted=[]
    for p in possible:
        d,blob=_file(repo,pin,p)
        request=d.get('request_id')
        _require(set(d)=={'schema_version','request_id','trade_date','stage','required_outputs'} and
                 type(d['schema_version']) is int and d['schema_version']==1 and d['trade_date']==day and d['stage']=='close' and
                 type(request) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',request))
        stamp=hashlib.sha256(request.encode()).hexdigest()[:16]
        _require(p.endswith(f'-{stamp}-publication.json'))
        refs=d.get('required_outputs');_require(type(refs) is list and 0<len(refs)<=320)
        pairs=set()
        for entry in refs:
            _require(type(entry) is dict and set(entry)=={'path','blob_sha'} and type(entry['path']) is str)
            pairs.add((DATA+entry['path'],_sha(entry['blob_sha'])))
        if not required <= pairs or any(not choices & pairs for choices in sources.values()):continue
        history=_git(repo,'log','--first-parent','-n','2','--diff-filter=A','--format=%H',pin,'--',p).stdout.splitlines()
        _require(len(history)==1)
        producer=_sha(history[0])
        _file(repo,producer,p,expected=blob)
        verified=_verify_required_manifest(repo,producer,p)
        _require({(x['path'],x['blob_sha']) for x in verified}==pairs)
        accepted.append(producer)
    _require(len(accepted)==1)
    producer=accepted[0]
    for oid in {pin,producer}:
        tag=f'refs/tags/market-data-read/{oid}'
        _require(_one_remote_ref(repo,'origin',tag)==oid)
        _require(_git(repo,'cat-file','-t',oid).stdout.strip()=='commit')
    _require(_one_remote_ref(repo,'origin','refs/heads/main')==pin)


def decide(repo:Path,day:str,compute:str,pin:str,universe_path:Path,*,now=None)->bool:
    if scheduled_close(day)!='13:00':return False
    _sha(compute);_sha(pin)
    _require(_git(repo,'rev-parse','HEAD').stdout.strip()==pin)
    _require(not _git(repo,'status','--porcelain','--untracked-files=all').stdout.strip())
    window=candidate_close_window(day)
    _require(window=={'start':'12:45','end':'13:00'})
    base=f'snapshots/{day[:7]}/{day}/close'
    pointer_path=DATA+base+'/latest.json'
    latest=_file(repo,pin,pointer_path,optional=True)
    if latest is None:return False
    pointer,pointer_blob=latest
    _require(type(pointer.get('schema_version')) is int and pointer['schema_version']==1)
    rel=pointer.get('snapshot_path')
    _require(type(rel) is str and re.fullmatch(re.escape(base)+r'/[A-Za-z0-9_-]+\.json',rel) and rel!=base+'/latest.json')
    snapshot,snap_blob=_file(repo,pin,DATA+rel,expected=_sha(pointer.get('snapshot_blob_sha')))
    _require(snapshot.get('trade_date')==day and snapshot.get('stage')=='close' and
             snapshot.get('snapshot_id')==pointer.get('snapshot_id') and snapshot.get('target_window')==window)
    instant=now or datetime.now(timezone.utc)
    close=datetime.fromisoformat(day+'T13:00:00').replace(tzinfo=ET)
    _require(close<=_time(snapshot.get('generated_at'))<=instant)
    sources=_covers(repo,pin,day,window,snapshot,load_collection_universe(universe_path),close,instant)
    if sources is None:return False
    pr=f'proofs/deterministic-metrics/{day[:7]}/{day}/close'
    proof_latest=_file(repo,pin,DATA+pr+'/latest.json',optional=True)
    if proof_latest is None:return False
    p,pointer_proof_blob=proof_latest
    _require(type(p.get('schema_version')) is int and p['schema_version']==1)
    _require(p.get('snapshot_path')==rel and p.get('snapshot_blob_sha')==snap_blob and
             p.get('snapshot_id')==pointer.get('snapshot_id'))
    path=p.get('proof_path'); _require(type(path) is str and
        re.fullmatch(re.escape(pr)+r'/[0-9a-f]{40}/[A-Za-z0-9_-]+\.json',path))
    doc,blob=_file(repo,pin,DATA+path,expected=_sha(p.get('proof_blob_sha')))
    _require(doc.get('trade_date')==day and doc.get('stage')=='close'
             and _sha(doc.get('data_plane_commit_sha'))==path.split('/')[-2])
    if doc['data_plane_commit_sha']!=compute:return False
    monitor=verify_existing_proof(repo/DATA,trade_date=day,stage='close',data_plane_commit_sha=compute)
    _require(monitor==(path,blob))
    required={(pointer_path,pointer_blob),(DATA+rel,snap_blob),(DATA+path,blob),
              (DATA+pr+'/latest.json',pointer_proof_blob)}
    _record(repo,pin,day,required,sources)
    return True


def main(argv=None):
    p=argparse.ArgumentParser()
    for k in ('repo','trade-date','compute','pin','universe','result'):p.add_argument('--'+k,required=True)
    a=p.parse_args(argv)
    try:
        chosen=decide(Path(a.repo),a.trade_date,a.compute,a.pin,Path(a.universe))
        with Path(a.result).open('x',encoding='ascii') as f:
            f.write('true\n' if chosen else 'false\n')
        return 0
    except Exception:
        print('EARLY_CLOSE_REUSE_EVIDENCE_UNCONFIRMED')
        return 2

if __name__=='__main__':raise SystemExit(main())
