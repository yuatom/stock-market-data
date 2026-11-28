"""Dormant offline candidate for immutable retained regular-day price backups.

No hosted entrypoint, credential access, quote request, or admission issuer.
Scope inputs must come from the independent exact private approval, not user
parameters copied into a public job. Git effects are under the existing shared
retained reconciliation owner in any future hosted integration.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from scripts.push_proof_store_commit import verify_exact_output_closure

ROOT='data/market-data/'
MAIN='refs/heads/main'
TAG='refs/tags/market-data-read/'
CLAIMS='refs/heads/reconciliation-claims/daily-price-only-v1/'
ZERO='0'*40
IDENT=('mic','symbol','asset_class','currency','session','basis')
FIELDS=('open','high','low','close')
ET=ZoneInfo('America/New_York')

class Hold(RuntimeError): pass
class SamePricesAuthorityChanged(Hold):pass
SAME_PRICES_AUTHORITY_CHANGED='RETAINED_PRICE_ONLY_SAME_PRICES_AUTHORITY_CHANGED'

def need(ok):
    if not ok:raise Hold('RETAINED_PRICE_ONLY_HOLD_INSPECT')

def encode(x):return (json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode()

def parse(raw):
    def unique(items):
        out={}
        for k,v in items:
            need(k not in out);out[k]=v
        return out
    val=json.loads(raw,object_pairs_hook=unique)
    need(encode(val)==raw)
    return val

def sha(raw):return hashlib.sha256(raw).hexdigest()

def blob(raw):return hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()

def oid(x):return type(x) is str and re.fullmatch('[0-9a-f]{40}',x)

def day(d):
    need(type(d) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}',d))
    need(date.fromisoformat(d).isoformat()==d)
    return d

def stamp(s):
    need(type(s) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ',s))
    return datetime.fromisoformat(s[:-1]+'+00:00')

def key(row):
    need(type(row) is dict and re.fullmatch(r'[A-Z0-9]{4}',row['mic']) and
         re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}',row['symbol']) and
         row['asset_class'] in ('stocks','etf') and row['currency']=='USD' and
         row['session']=='regular' and row['basis']=='split_adjusted_price_only')
    d=day(row['trade_date'])
    return {x:row[x] for x in IDENT}|{'trade_date':d}

def slot_key(row):return CLAIMS+sha(b'retained-regular-daily-price-target-v1\0'+encode(key(row)))

def record_path(row):
    k=key(row)
    return 'recovery/daily-price-only/v1/'+'/'.join(k[x] for x in ('mic','symbol','session','basis','trade_date'))+'.json'

def evidence_path(row,source):
    need(type(source) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',source))
    return record_path(row).removesuffix('.json')+'/observations/'+source+'.json'

def _stage_prices(prices):
    need(type(prices) is dict and set(prices)==set(FIELDS))
    need(all(type(v) is str and re.fullmatch(r'(?:0|[1-9]\d{0,7})\.\d{2}',v) for v in prices.values()))
    values={k:Decimal(v) for k,v in prices.items()}
    need(0<values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high'])

def _verify_package(row,refs,source_urls,session_close,normalized_sources,observation_kind):
    k=key(row)
    need(set(row)==set(k)|{'schema_version','kind','scheduled_close_et','ohlc','evidence',
         'purpose','native_provider_receipt','admitted_at'} and type(row['schema_version']) is int
         and row['schema_version']==1 and row['kind']=='admitted_regular_daily_price_backup'
         and row['purpose']=='prior_session_close_only' and row['native_provider_receipt'] is False)
    d=row['trade_date'];close=row['scheduled_close_et']
    need(close in ('13:00','16:00') and session_close.get(d)==close)
    session_end=datetime.fromisoformat(d+'T'+close).replace(tzinfo=ET)
    admitted=stamp(row['admitted_at'])
    _stage_prices(row['ohlc'])
    need(type(row['evidence']) is list and len(row['evidence'])==2 and type(refs) is dict and len(refs)==2)
    seen=set()
    for ref in row['evidence']:
        need(type(ref) is dict and set(ref)=={'source_id','path','blob_sha','sha256'})
        source=ref['source_id'];path=evidence_path(row,source)
        need(source in source_urls and source not in seen and ref['path']==path)
        seen.add(source)
        observed=refs[path]
        need(type(observed) is bytes and 0<len(observed)<24000 and blob(observed)==ref['blob_sha'] and sha(observed)==ref['sha256'])
        obs=parse(observed)
        need(set(obs)==set(k)|{'schema_version','kind','scheduled_close_et','source_id','url','ohlc','observed_at','source_basis'}
             and type(obs['schema_version']) is int and obs['schema_version']==1)
        need(all(obs[x]==k[x] for x in k) and obs['kind']==observation_kind
             and obs['source_id']==source and obs['url']==source_urls[source]
             and obs['scheduled_close_et']==close and obs['ohlc']==row['ohlc']
             and session_end<=stamp(obs['observed_at'])<=admitted)
        need(urlsplit(obs['url']).scheme=='https')
        if obs['source_basis']!='split_adjusted_explicit':
            # Input must be separately verified against native source basis +
            # corporate-action evidence; a web observation cannot self-qualify.
            need(obs['source_basis']=='regular_prices_post_split_normalized' and
                 normalized_sources.get((d,source))==ref['sha256'])
    need(seen==set(source_urls) and len({urlsplit(url).hostname for url in source_urls.values()})==2)
    return {ROOT+record_path(row):encode(row)}|{ROOT+p:raw for p,raw in refs.items()}

def git(root,*args,raw=None,env=None,allow_fail=False):
    result=subprocess.run(['git','--no-replace-objects','-C',str(root),
                           '-c','core.hooksPath=/dev/null','-c','user.name=Retained test',
                           '-c','user.email=retained@example.invalid',*args],
                          input=raw,capture_output=True,env=env,timeout=25)
    if not allow_fail:need(result.returncode==0)
    return result

def val(root,*args,**kwargs):return git(root,*args,**kwargs).stdout.decode('ascii').strip()

def remote(root,ref):
    result=val(root,'ls-remote','--refs','origin',ref)
    if not result:return None
    raw, sep, actual=result.partition('\t')
    need(sep and actual==ref and oid(raw))
    return raw

def read(root,object_id,path):
    entry=val(root,'ls-tree',object_id,'--',path)
    if not entry:return None
    need(re.fullmatch(r'100644 blob [0-9a-f]{40}\t'+re.escape(path),entry))
    return git(root,'show',object_id+':'+path).stdout

@dataclass(frozen=True)
class Plan:
    base:str
    records:tuple
    additions:dict
    manifest:str
    noop:bool

def plan(root,base,packages,*,source_urls,session_close,legacy_occupied,request_id,normalized_sources=None,
         observation_kind='public_regular_daily_price_observation'):
    """Validate exact input shape/closure; no write unless explicitly published."""
    try:
        need(oid(base) and remote(root,MAIN)==base and remote(root,TAG+base)==base and 1<=len(packages)<=8 and
             type(request_id) is str and re.fullmatch(r'(?:[1-9][0-9]{0,19}-1|retained-price-only-[0-9a-f]{32})',request_id))
        add={};records=[];existing=[]
        for row,raw in packages:
            scope=slot_key(row);need(scope not in legacy_occupied and scope not in {slot_key(r) for r in records})
            data=_verify_package(row,raw,source_urls,session_close,normalized_sources or {},observation_kind)
            need(set(data).isdisjoint(add))
            visible={p:read(root,base,p) for p in data}
            if any(x is not None for x in visible.values()):
                if visible!=data:
                    old,old_data=_read_existing_slot(root,base,row)
                    _verify_existing(root,base,old,old_data)
                    if old['ohlc']==row['ohlc']:
                        # The independently retained prices agree. New proof
                        # bytes do not justify any write or a price-conflict label.
                        raise SamePricesAuthorityChanged(SAME_PRICES_AUTHORITY_CHANGED)
                    raise Hold('RETAINED_PRICE_ONLY_APPROVED_EXISTING_PRICES_DIFFER')
                _verify_existing(root,base,row,data)
                existing.append(scope)
            else:need(remote(root,scope) is None)
            records.append(row);add.update(data)
        need(not existing or len(existing)==len(records))
        manifest=ROOT+'collector-state/reusable-price-only-'+sha(encode(sorted(slot_key(r) for r in records)))+'.json'
        if existing:return Plan(base,tuple(records),{},manifest,True)
        need(read(root,base,manifest) is None)
        add[manifest]=encode(dict(schema_version=1,request_id=request_id,trade_date=min(r['trade_date'] for r in records),
            stage='retained_price_only_reconciliation',required_outputs=[
            {'path':p.removeprefix(ROOT),'blob_sha':blob(x)} for p,x in sorted(add.items())]))
        return Plan(base,tuple(records),add,manifest,False)
    except (TypeError,ValueError,KeyError,OSError,subprocess.SubprocessError):
        raise Hold('RETAINED_PRICE_ONLY_HOLD_INSPECT') from None

def _read_existing_slot(root,base,requested):
    path=ROOT+record_path(requested)
    raw=read(root,base,path)
    need(raw is not None)
    old=parse(raw)
    need(key(old)==key(requested) and set(old)==set(requested) and
         type(old['schema_version']) is int and old['schema_version']==1 and
         old['kind']=='admitted_regular_daily_price_backup' and
         old['native_provider_receipt'] is False and old['purpose']=='prior_session_close_only')
    _stage_prices(old['ohlc'])
    retained={path:raw};seen=set()
    need(type(old['evidence']) is list and len(old['evidence'])==2)
    for ref in old['evidence']:
        need(set(ref)=={'source_id','path','blob_sha','sha256'} and ref['source_id'] not in seen
             and ref['path']==evidence_path(old,ref['source_id']))
        seen.add(ref['source_id'])
        path=ROOT+ref['path'];data=read(root,base,path)
        need(data is not None and blob(data)==ref['blob_sha'] and sha(data)==ref['sha256'])
        retained[path]=data
    return old,retained

def _verify_existing(root,base,row,data):
    ref=slot_key(row);claimed=remote(root,ref);need(oid(claimed))
    git(root,'fetch','-q','--no-tags','origin',claimed)
    claim=parse(read(root,claimed,'claim.json'))
    fields={'identity','kind','schema_version','market_commit','manifest','fact_blobs'}
    need(set(claim) in (fields, fields|{'publication_proof'}) and
         type(claim['schema_version']) is int and claim['schema_version']==1 and
         claim['identity']==key(row) and claim['kind']=='immutable_reusable_price_target'
         and claim['fact_blobs']=={p:blob(raw) for p,raw in data.items()})
    if 'publication_proof' in claim:
        _prove_publication_context(claim['publication_proof'])
    market=claim['market_commit']
    need(oid(market) and remote(root,TAG+market)==market and
         val(root,'merge-base',market,base)==market and remote(root,ref)==claimed)
    parent=val(root,'rev-parse',market+'^')
    verify_exact_output_closure(root,parent,market,claim['manifest'])
    need(all(read(root,market,p)==raw for p,raw in data.items()))

def _commit(root,base,files):
    with TemporaryDirectory() as tmp:
        env=os.environ|{'GIT_INDEX_FILE':str(Path(tmp)/'index')}
        git(root,'read-tree',base,env=env) if base else git(root,'read-tree','--empty',env=env)
        for path,raw in files.items():
            item=val(root,'hash-object','-w','--stdin',raw=raw)
            need(item==blob(raw));git(root,'update-index','--add','--cacheinfo',f'100644,{item},{path}',env=env)
        tree=val(root,'write-tree',env=env)
        return val(root,'commit-tree',tree,*(('-p',base) if base else ()),'-m','Retained price-only candidate')

def _candidate(root,base,market,files,manifest):
    need(val(root,'rev-list','--parents','-n1',market).split()==[market,base])
    result=git(root,'diff-tree','-r','--no-commit-id','--no-renames','--name-status','-z',base,market).stdout.decode().split('\0')
    need(result[-1]=='' and len(result)==2*len(files)+1 and dict(zip(result[1::2],result[::2]))==dict.fromkeys(files,'A'))
    need(all(read(root,market,p)==raw for p,raw in files.items()))
    verify_exact_output_closure(root,base,market,manifest)

def _prove_publication_context(proof):
    need(type(proof) is dict and
         set(proof)=={'compute_sha','run_id','receipt_commit','receipt_commitment','source_commit'} and
         all(oid(proof[k]) for k in ('compute_sha','receipt_commit','source_commit')) and
         type(proof['run_id']) is str and re.fullmatch(r'[1-9][0-9]{0,19}',proof['run_id']) and
         type(proof['receipt_commitment']) is str and re.fullmatch('[0-9a-f]{64}',proof['receipt_commitment']))


def commit_atomically(root,p,*,before_push,publication_proof=None):
    """Offline-testable main + tag + all stable claims. No retry or recovery write."""
    try:
        need(remote(root,MAIN)==p.base and remote(root,TAG+p.base)==p.base)
        if publication_proof is not None:_prove_publication_context(publication_proof)
        if p.noop:
            for row in p.records:
                record=ROOT+record_path(row); data={record:read(root,p.base,record)}
                need(data[record]==encode(row))
                for ref in row['evidence']:
                    path=ROOT+ref['path'];raw=read(root,p.base,path)
                    need(raw is not None and blob(raw)==ref['blob_sha'] and sha(raw)==ref['sha256'])
                    data[path]=raw
                _verify_existing(root,p.base,row,data)
            need(before_push() is True and remote(root,MAIN)==p.base and remote(root,TAG+p.base)==p.base)
            return None
        market=_commit(root,p.base,p.additions);_candidate(root,p.base,market,p.additions,p.manifest)
        refs={MAIN:market,TAG+market:market};claims={}
        for row in p.records:
            ref=slot_key(row);paths=[ROOT+record_path(row),*(ROOT+x['path'] for x in row['evidence'])]
            data=encode(dict(schema_version=1,kind='immutable_reusable_price_target',
                       identity=key(row),market_commit=market,manifest=p.manifest,
                       fact_blobs={path:blob(p.additions[path]) for path in paths})|
                       ({} if publication_proof is None else {'publication_proof':publication_proof}))
            claim=_commit(root,None,{'claim.json':data})
            refs[ref]=claim;claims[ref]=data
        need(all(remote(root,r) is None for r in refs if r!=MAIN))
        need(before_push() is True)
        result=git(root,'push','--porcelain','--atomic',
            *(f'--force-with-lease={r}:{p.base if r==MAIN else ZERO}' for r in refs),
            'origin',*(f'{o}:{r}' for r,o in refs.items())).stdout.decode('ascii')
        ack={}
        for line in result.split('\n'):
            if '\t' not in line:continue
            flag,srcdest,_=line.split('\t');r=srcdest.split(':',1)[1];need(r in refs and r not in ack);ack[r]=flag
        need(ack=={r:' ' if r==MAIN else '*' for r in refs})
        need(all(remote(root,r)==o for r,o in refs.items()))
        for ref,data in claims.items():
            git(root,'fetch','-q','--no-tags','origin',refs[ref]);need(read(root,refs[ref],'claim.json')==data)
        git(root,'fetch','-q','--no-tags','origin',market);_candidate(root,p.base,market,p.additions,p.manifest)
        need(all(remote(root,r)==o for r,o in refs.items()))
        return market
    except Exception:
        raise Hold('RETAINED_PRICE_ONLY_HOLD_INSPECT_NO_RETRY') from None
