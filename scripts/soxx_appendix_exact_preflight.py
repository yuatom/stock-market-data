#!/usr/bin/env python3
"""Read-only preflight for one exact SOXX prior-close appendix. No publication."""
from dataclasses import dataclass
from datetime import date as calendar_date
from pathlib import Path
from types import MappingProxyType
import hashlib
import json
import re
import subprocess

from push_proof_store_commit import StorePushError, verify_exact_output_closure

OID=re.compile(r'[0-9a-f]{40}\Z')
DIGEST=re.compile(r'[0-9a-f]{64}\Z')
DAY=re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
NATIVE_PATTERN=re.compile(r'data/market-data/series-versions/daily/twelve_data_basic/SOXX/[0-9a-f]{64}/snapshot[.]json\Z')
@dataclass(frozen=True)
class _Plan:
    evidence_base:str
    base:str
    head:str
    tree:str
    manifest:str
    blobs:object
    output_hashes:object
    native_path:str
    native_blob:str
    trade_dates:tuple
    advance:object=None
    candidate_parents:tuple=()


def _git(root,*args):
    try:
        return subprocess.check_output(['git','--no-replace-objects','-C',str(root),*args],
                                       stderr=subprocess.DEVNULL,timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('STORE_LOCAL_OBJECT_UNREADABLE') from exc

def _require(ok, reason):
    if not ok:
        raise ValueError(reason)

def _strict_json(raw):
    def unique(pairs):
        obj={}
        for key,value in pairs:
            _require(key not in obj,'STORE_DUPLICATE_OBJECT_KEY')
            obj[key]=value
        return obj
    return json.loads(raw,object_pairs_hook=unique)

@dataclass(frozen=True)
class _BaseAdvance:
    publication_base:str
    trade_date:str
    tree:str
    manifest:str
    paths:object


def _keys(obj,*required):
    _require(type(obj) is dict and obj.keys()==set(required),'PRIVATE_PROFILE_SCHEMA_INVALID')

def _match(pattern,value):
    _require(type(value) is str and pattern.fullmatch(value),'PRIVATE_PROFILE_VALUE_INVALID')
    return value

def _day(value):
    return calendar_date.fromisoformat(_match(DAY,value)).isoformat()

def load_profile(raw):
    _require(type(raw) is bytes and 0<len(raw)<=16000,'PRIVATE_PROFILE_SIZE')
    doc=_strict_json(raw)
    _keys(doc,'schema_version','kind','salt','evidence_base','base','head','tree','candidate_parents',
          'native_path','native_blob','manifest','outputs','trade_dates','advance')
    _require(type(doc['schema_version']) is int and doc['schema_version']==1
             and doc['kind']=='soxx_web_appendix_private_profile','PRIVATE_PROFILE_VERSION')
    _match(DIGEST,doc['salt'])
    evidence,base,head,tree,native_blob=(_match(OID,doc[k]) for k in ('evidence_base','base','head','tree','native_blob'))
    native=_match(NATIVE_PATTERN,doc['native_path'])
    parents=doc['candidate_parents']
    _require(type(parents) is list and 1<=len(parents)<=2,'PRIVATE_PROFILE_PARENT_SHAPE')
    parents=tuple(_match(OID,x) for x in parents)
    _require(len(set(parents))==len(parents) and base in parents and head not in {*parents,evidence},
             'PRIVATE_PROFILE_PARENT_SCOPE')
    dates=doc['trade_dates']
    _require(type(dates) is list and len(dates)==10,'PRIVATE_PROFILE_DATE_SCOPE')
    dates=tuple(_day(x) for x in dates)
    _require(dates==tuple(sorted(set(dates))),'PRIVATE_PROFILE_DATE_ORDER')
    end=dates[-1]
    _require(dates[0][:7]==end[:7],'PRIVATE_PROFILE_DATE_SCOPE')
    manifest=_match(re.compile(fr'data/market-data/collector-state/{end[:7]}/{end}-soxx_public_web_price_appendix-[0-9a-f]{{16}}-publication[.]json'),doc['manifest'])
    prefix=f'data/market-data/recovery/{end}/public-web/soxx-prior-close-v1/'
    files={prefix+n for n in ('manifest.json','source_sp.json','source_twelve.json')}
    _keys(doc['outputs'],manifest,*files)
    blobs={}; hashes={}
    for path,row in doc['outputs'].items():
        _keys(row,'blob','sha256')
        blobs[path]=_match(OID,row['blob'])
        if path!=manifest:
            hashes[path]=_match(DIGEST,row['sha256'])
        else:
            _require(row['sha256'] is None,'PRIVATE_PROFILE_OUTPUT_SCOPE')
    adv=doc['advance']; advance=None
    if base==evidence:
        _require(adv is None,'PRIVATE_PROFILE_UNEXPECTED_ADVANCE')
    else:
        _keys(adv,'tree','trade_date','manifest','paths')
        day=_day(adv['trade_date'])
        _require(day>end,'PRIVATE_PROFILE_PRE_DATE_SCOPE')
        tree_advance=_match(OID,adv['tree'])
        m_advance=_match(re.compile(fr'data/market-data/collector-state/{day[:7]}/{day}-premarket-[0-9a-f]{{16}}-publication[.]json'),adv['manifest'])
        rows=adv['paths']
        _require(type(rows) is dict and 4<=len(rows)<=32,'PRIVATE_PROFILE_PRE_CHANGE_COUNT')
        paths={}
        for path,row in rows.items():
            _keys(row,'status','blob')
            _require(type(path) is str and len(path)<300 and row['status'] in ('A','M'),
                     'PRIVATE_PROFILE_PRE_STATUS_SCOPE')
            paths[path]=(row['status'],_match(OID,row['blob']))
        advance=_BaseAdvance(base,day,tree_advance,m_advance,MappingProxyType(paths))
        _validate_advance_paths(advance)
    return _Plan(evidence,base,head,tree,manifest,MappingProxyType(blobs),MappingProxyType(hashes),
                 native,native_blob,dates,advance,parents)

def _validate_advance_paths(advance):
    date=advance.trade_date
    pre='data/market-data/'
    inv=pre+'collector-state/market-data-inventory.json'
    month=date[:7]
    manifest_pattern=pre+fr'collector-state/{month}/{date}-premarket-[0-9a-f]{{16}}-publication[.]json'
    snapshot=pre+f'snapshots/{month}/{date}/premarket/'
    session=pre+f'sessions/{month}/{date}/premarket/0400-0930/nasdaq_public_extended/'
    def allowed(path,status):
        if path==inv:
            return status=='M'
        if status!='A':
            return False
        return ((path==advance.manifest and re.fullmatch(manifest_pattern,path))
                or (path.startswith(snapshot) and re.fullmatch(r'(latest|premarket-[0-9]{6}-et)[.]json',path[len(snapshot):]))
                or (path.startswith(session) and re.fullmatch(r'premarket-[a-z0-9]+-[0-9]{6}-et[.]json',path[len(session):])))
    _require(advance.manifest in advance.paths and inv in advance.paths,'STORE_PREMARKET_CLOSURE_MISSING')
    _require(all(allowed(p,s) for p,(s,_) in advance.paths.items()),'STORE_NON_PREMARKET_BASE_ADVANCE')

def _verify_base_advance(root,evidence_base,native_blob,publication_base,advance,native_path):
    def g(*args):
        return _git(root,*args).decode('ascii').strip()
    for commit in (evidence_base,publication_base):
        _require(g('ls-tree',commit,'--',native_path)==f'100644 blob {native_blob}\t{native_path}',
                 'STORE_SOXX_NATIVE_HISTORY_CHANGE')
    if evidence_base==publication_base:
        _require(advance is None,'STORE_UNNECESSARY_BASE_ADVANCE')
        return
    _require(advance is not None and publication_base==advance.publication_base,'STORE_UNAPPROVED_BASE_ADVANCE')
    _require(g('rev-list','--parents','-n','1',publication_base).split()==[publication_base,evidence_base],
             'STORE_BASE_ADVANCE_NOT_DIRECT_CHILD')
    _require(g('rev-parse',publication_base+'^{tree}')==advance.tree,'STORE_BASE_ADVANCE_TREE_DRIFT')
    _validate_advance_paths(advance)
    observed=_git(root,'diff-tree','-z','-r','--no-renames','--name-status',evidence_base,publication_base)
    fields=observed.decode('ascii').split('\0')
    _require(fields[-1]=='' and len(fields)==2*len(advance.paths)+1,'STORE_BASE_ADVANCE_PATH_DRIFT')
    _require(dict(zip(fields[1::2],fields[::2]))=={p:s for p,(s,_) in advance.paths.items()},'STORE_BASE_ADVANCE_PATH_DRIFT')
    for path,(status,blob) in advance.paths.items():
        _require(g('ls-tree',publication_base,'--',path)==f'100644 blob {blob}\t{path}',
                 'STORE_BASE_ADVANCE_BLOB_DRIFT')
    manifest=_strict_json(_git(root,'show',publication_base+':'+advance.manifest))
    _require(manifest.get('stage')=='premarket' and manifest.get('trade_date')==date,'STORE_PREMARKET_CLOSURE_SCOPE')
    try:
        verify_exact_output_closure(root,evidence_base,publication_base,advance.manifest)
    except StorePushError as exc:
        raise ValueError(str(exc)) from exc


def preflight(root, plan):
    def g(*args):
        return _git(root,*args).decode('ascii').strip()
    _verify_base_advance(root,plan.evidence_base,plan.native_blob,plan.base,plan.advance,plan.native_path)
    _require(g('rev-parse','HEAD')==plan.head,'STORE_CANDIDATE_DRIFT')
    _require(not g('status','--porcelain','--untracked-files=all'),'STORE_DIRTY_WORKTREE')
    parents=plan.candidate_parents or (plan.base,)
    _require(plan.base in parents and g('rev-list','--parents','-n','1',plan.head).split()==[plan.head,*parents],
             'STORE_CANDIDATE_PARENT_DRIFT')
    _require(g('rev-parse',plan.head+'^{tree}')==plan.tree,'STORE_TREE_DRIFT')
    _require(set(plan.blobs)=={plan.manifest,*plan.output_hashes},'STORE_APPENDIX_UNEXPECTED_PATH_PLAN')
    raw=_git(root,'diff-tree','-z','-r','--no-renames','--name-status',plan.base,plan.head)
    fields=raw.decode('ascii').split('\0')
    _require(fields[-1]=='' and len(fields)==9,'STORE_NEEDS_EXACT_FOUR_ADDS')
    _require(dict(zip(fields[1::2],fields[::2]))==dict.fromkeys(plan.blobs,'A'),'STORE_NEEDS_EXACT_FOUR_ADDS')
    for path,blob in plan.blobs.items():
        _require(g('ls-tree',plan.head,'--',path)==f'100644 blob {blob}\t{path}',
                 'STORE_APPENDIX_BLOB_OR_FILE_MODE_MISMATCH')
        if path in plan.output_hashes:
            _require(hashlib.sha256(_git(root,'show',plan.head+':'+path)).hexdigest()==plan.output_hashes[path],
                     'STORE_APPENDIX_BYTES_MISMATCH')
    for commit in (plan.evidence_base,plan.base,plan.head):
        _require(g('rev-parse',commit +':'+plan.native_path)==plan.native_blob,'STORE_SOXX_NATIVE_HISTORY_CHANGE')
    publication=_strict_json(_git(root,'show',plan.head+':'+plan.manifest))
    _require(type(publication) is dict and type(publication.get('schema_version')) is int and publication['schema_version']==1
             and publication.get('trade_date')==plan.trade_dates[-1]
             and publication.get('stage')=='soxx_public_web_price_appendix','STORE_PUBLICATION_SCOPE_MISMATCH')
    try:
        verify_exact_output_closure(root,plan.base,plan.head,plan.manifest)
    except StorePushError as exc:
        raise ValueError(str(exc)) from exc
    appendix_path=f'data/market-data/recovery/{plan.trade_dates[-1]}/public-web/soxx-prior-close-v1/manifest.json'
    data=_strict_json(_git(root,'show',plan.head+':'+appendix_path))
    _require(data.get('kind')=='soxx_public_web_prior_close_appendix'
             and data.get('purpose')=='prior_close_only'
             and data.get('base_store_commit')==plan.evidence_base
             and data.get('base_snapshot_blob')==plan.native_blob
             and data.get('provider_native_receipt') is False
             and data.get('market_qualified') is False and data.get('production_activation') is False
             and data.get('volume_policy')=='BLOCKED_UNRESOLVED'
             and all(set(row)=={'trade_date','open','high','low','close'} for row in data.get('bars',()))
             and tuple(r['trade_date'] for r in data['bars'])==plan.trade_dates,
             'STORE_APPENDIX_PRICE_ONLY_SCOPE_MISMATCH')
    return {'status':'local_preflight_only','evidence_base':plan.evidence_base,'base':plan.base,'candidate':plan.head,'tree':plan.tree,
            'publication_manifest':plan.manifest,'provider_requests':0,'consumer_authority':False}
