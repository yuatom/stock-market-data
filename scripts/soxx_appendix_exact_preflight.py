#!/usr/bin/env python3
"""Read-only preflight for one exact SOXX prior-close appendix. No publication."""
from __future__ import annotations
from dataclasses import dataclass, replace
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
    native_selector_blob:str|None=None
    native_admission_blob:str|None=None
    frozen_appendix_commit:str|None=None


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
    if type(doc) is dict and type(doc.get('schema_version')) is int and doc['schema_version']==2:
        return _load_two_hop(doc)
    if type(doc) is dict and type(doc.get('schema_version')) is int and doc['schema_version']==3:
        return _load_isolated_head(doc)
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
    _require(manifest.get('stage')=='premarket' and manifest.get('trade_date')==advance.trade_date,'STORE_PREMARKET_CLOSURE_SCOPE')
    try:
        verify_exact_output_closure(root,evidence_base,publication_base,advance.manifest)
    except StorePushError as exc:
        raise ValueError(str(exc)) from exc


def _verify_frozen_outputs(root,plan):
    if plan.frozen_appendix_commit is None:
        return
    _git(root,'merge-base','--is-ancestor',plan.frozen_appendix_commit,plan.head)
    for path,blob in plan.blobs.items():
        _require(_git(root,'ls-tree',plan.frozen_appendix_commit,'--',path).decode('ascii').strip()
                 ==f'100644 blob {blob}\t{path}','FROZEN_APPENDIX_BYTES_CHANGE')

def preflight(root, plan):
    def g(*args):
        return _git(root,*args).decode('ascii').strip()
    _verify_plan_advance(root,plan)
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
    _verify_frozen_outputs(root,plan)
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

@dataclass(frozen=True)
class _CloseAdvance:
    publication_base:str
    tree:str
    trade_date:str
    manifest:str
    paths:object
    compute_sha:str
    snapshot_id:str
    symbols:tuple

def _load_two_hop(doc):
    _keys(doc,'schema_version','kind','salt','evidence_base','base','head','tree','candidate_parents',
          'native_path','native_blob','manifest','outputs','trade_dates','advance',
          'native_selector_blob','native_admission_blob','frozen_appendix_commit')
    sequence=doc['advance']
    _require(type(sequence) is list and len(sequence)==2,'PRIVATE_PROFILE_FINITE_SEQUENCE')
    pre,close=sequence
    _keys(pre,'stage','commit','tree','trade_date','manifest','paths')
    _keys(close,'stage','commit','tree','trade_date','manifest','paths','compute_sha','window','snapshot_id','symbols')
    _require(pre['stage']=='premarket' and close['stage']=='close' and
             pre['trade_date']==close['trade_date'] and close['window']=={'start':'12:45','end':'13:00'},
             'PRIVATE_PROFILE_FINITE_STAGE_WINDOW')
    old={k:v for k,v in doc.items() if k not in ('native_selector_blob','native_admission_blob','frozen_appendix_commit')}
    # Reuse v1's field and first-hop validators without making the v1 type aware of Close.
    old.update(schema_version=1,base=pre['commit'],candidate_parents=[pre['commit']],
               advance={k:v for k,v in pre.items() if k not in ('stage','commit')})
    plan=load_profile(json.dumps(old).encode())
    _require(plan.advance is not None,'PRIVATE_PROFILE_FINITE_PRE_REQUIRED')
    base=_match(OID,doc['base'])
    _require(base==_match(OID,close['commit']) and len({base,plan.evidence_base,plan.base,plan.head})==4,
             'PRIVATE_PROFILE_FINITE_OBJECT_SCOPE')
    parents=doc['candidate_parents']
    _require(type(parents) is list and 1<=len(parents)<=2,'PRIVATE_PROFILE_PARENT_SHAPE')
    parents=tuple(_match(OID,p) for p in parents)
    _require(len(set(parents))==len(parents) and base in parents and plan.head not in parents,'PRIVATE_PROFILE_PARENT_SCOPE')
    frozen=_match(OID,doc['frozen_appendix_commit'])
    _require(frozen not in {base,plan.evidence_base,plan.base,plan.head},'PRIVATE_PROFILE_FROZEN_SCOPE')
    day=_day(close['trade_date']); month=day[:7]
    m=_match(re.compile(fr'data/market-data/collector-state/{month}/{day}-close-[0-9a-f]{{16}}-publication[.]json'),close['manifest'])
    sid=_match(re.compile(r'close-[0-9]{6}-et'),close['snapshot_id'])
    sha=_match(OID,close['compute_sha'])
    symbols=close['symbols']
    _require(type(symbols) is list and len(symbols)==33,'PRIVATE_PROFILE_CLOSE_SYMBOL_COUNT')
    symbols=tuple(_match(re.compile(r'[A-Z0-9]{1,10}'),r) for r in symbols)
    _require('SOXX' in symbols and len(set(symbols))==33,'PRIVATE_PROFILE_CLOSE_SYMBOLS')
    rows=close['paths']
    _require(type(rows) is dict and len(rows)==41,'PRIVATE_PROFILE_CLOSE_PATH_COUNT')
    paths={}
    for path,row in rows.items():
        _keys(row,'status','blob')
        _require(type(path) is str and len(path)<300 and row['status'] in ('A','M'),'PRIVATE_PROFILE_CLOSE_PATH_SCOPE')
        paths[path]=(row['status'],_match(OID,row['blob']))
    advance=_CloseAdvance(base,_match(OID,close['tree']),day,m,MappingProxyType(paths),sha,sid,symbols)
    _close_paths(advance)
    return replace(plan,base=base,candidate_parents=parents,advance=(plan.advance,advance),
                   native_selector_blob=_match(OID,doc['native_selector_blob']),
                   native_admission_blob=_match(OID,doc['native_admission_blob']),
                   frozen_appendix_commit=frozen)

def _close_paths(hop):
    root='data/market-data/'
    month=hop.trade_date[:7]
    d=f'{month}/{hop.trade_date}'
    snaps=f'{root}snapshots/{d}/close/'
    proofs=f'{root}proofs/deterministic-metrics/{d}/close/'
    collectors=f'{root}collector-state/{month}/{hop.trade_date}-'
    session=f'{root}sessions/{d}/regular/1245-1300/twelve_data_basic/'
    captures={s:f'{session}close-{s.lower()}-{hop.snapshot_id[6:]}.json' for s in hop.symbols}
    paths=dict(snapshot=snaps+hop.snapshot_id+'.json',snapshot_latest=snaps+'latest.json',
               proof=proofs+hop.compute_sha+'/'+hop.snapshot_id+'.json',proof_latest=proofs+'latest.json',
               missing=collectors+'close-missing.json',budget=collectors+'twelve-data-budget.json')
    inventory=root+'collector-state/market-data-inventory.json'
    expected=dict.fromkeys([*paths.values(),*captures.values(),hop.manifest],'A')
    expected[inventory]='M'
    _require({p:s for p,(s,_) in hop.paths.items()}==expected,'PRIVATE_PROFILE_CLOSE_OBJECT_SCOPE')
    return paths,captures

def _verify_native_selection(root,commit,plan):
    r='data/market-data/'
    active=r+'series-active/daily/twelve_data_basic/SOXX.json'
    basis=plan.native_path.split('/')[-2]
    admission=r+'series-admissions/daily/twelve_data_basic/SOXX/'+plan.native_admission_blob+'.json'
    for path,pin in ((active,plan.native_selector_blob),(admission,plan.native_admission_blob),(plan.native_path,plan.native_blob)):
        _require(_git(root,'ls-tree',commit,'--',path).decode('ascii').strip()==f'100644 blob {pin}\t{path}',
                 'FINITE_NATIVE_SELECTOR_OR_ADMISSION_DRIFT')
    data=_strict_json(_git(root,'show',commit+':'+active))
    _require(type(data) is dict and all(data.get(k)==v for k,v in dict(symbol='SOXX',provider='twelve_data_basic',
             basis_id=basis,snapshot_blob_sha=plan.native_blob,admission_blob_sha=plan.native_admission_blob).items()),
             'FINITE_NATIVE_SELECTOR_INCONSISTENT')

def _verify_plan_advance(root,plan):
    if isinstance(plan.advance,_IsolatedHead):
        _verify_isolated_head(root,plan)
        return
    if not isinstance(plan.advance,tuple):
        _verify_base_advance(root,plan.evidence_base,plan.native_blob,plan.base,plan.advance,plan.native_path)
        return
    pre,close=plan.advance
    for c in (plan.evidence_base,pre.publication_base,close.publication_base):
        _verify_native_selection(root,c,plan)
    _verify_base_advance(root,plan.evidence_base,plan.native_blob,pre.publication_base,pre,plan.native_path)
    _verify_early_close(root,pre.publication_base,close)

def _verify_early_close(root,parent,hop):
    """Reconcile previously retained Store drift; never qualify a producer or minute coverage.

    The independently approved private profile pins reviewed retained objects;
    seeing a per-symbol capture says nothing about complete minute coverage.
    """
    commit=hop.publication_base
    paths,captures=_close_paths(hop)
    def g(*args): return _git(root,*args).decode('ascii').strip()
    def load(path): return _strict_json(_git(root,'show',commit+':'+path))
    def rel(path): return path.removeprefix('data/market-data/')
    def blob(path): return hop.paths[path][1]
    def agrees(obj,**expected):
        return type(obj) is dict and all(obj.get(k)==v for k,v in expected.items())
    _require(g('rev-list','--parents','-n','1',commit).split()==[commit,parent],'CLOSE_NOT_NEXT_DIRECT_CHILD')
    _require(g('rev-parse',commit+'^{tree}')==hop.tree,'CLOSE_TREE_DRIFT')
    observed=_git(root,'diff-tree','-z','-r','--no-renames','--name-status',parent,commit).decode('ascii').split('\0')
    _require(observed[-1]=='' and len(observed)==2*len(hop.paths)+1 and
             dict(zip(observed[1::2],observed[::2]))=={k:v[0] for k,v in hop.paths.items()},'CLOSE_DELTA_DRIFT')
    for path,(_,pin) in hop.paths.items():
        _require(g('ls-tree',commit,'--',path)==f'100644 blob {pin}\t{path}','CLOSE_PATH_TYPE_OR_BLOB_DRIFT')
    day=hop.trade_date
    manifest=load(hop.manifest)
    _require(agrees(manifest,schema_version=1,stage='close',trade_date=day) and type(manifest['schema_version']) is int
             and type(manifest.get('request_id')) is str and re.fullmatch(fr"close-{day.replace('-','')}-[0-9]{{6}}-et-01-collector",manifest['request_id']),
             'CLOSE_TERMINAL_UNRECOGNIZED')
    try:
        verify_exact_output_closure(root,parent,commit,hop.manifest)
    except StorePushError as exc:
        raise ValueError('CLOSE_TERMINAL_OUTPUT_MISMATCH') from exc
    win={'start':'12:45','end':'13:00'}
    snapshot=load(paths['snapshot'])
    _require(agrees(snapshot,schema_version=1,stage='close',trade_date=day,snapshot_id=hop.snapshot_id,target_window=win,missing=[]),
             'CLOSE_SNAPSHOT_SCOPE_MISMATCH')
    coverage=snapshot.get('coverage')
    _require(agrees(coverage,symbols_requested_in_increment=33,symbols_available_in_increment=33),
             'CLOSE_COVERAGE_SCOPE_MISMATCH')
    refs=snapshot.get('data_refs')
    _require(type(refs) is list and len(refs)==33 and all(type(x) is dict for x in refs) and
             {x.get('symbol') for x in refs}==set(captures),'CLOSE_SNAPSHOT_SYMBOL_SCOPE_MISMATCH')
    for ref in refs:
        symbol=ref['symbol']; path=captures[symbol]
        _require(agrees(ref,path=rel(path),blob_sha=blob(path),provider='twelve_data_basic',window='12:45-13:00',reason_class='success')
                 and ref.get('kind') in ('regular_intraday_capture','cross_asset_proxy_capture','sector_context_capture'),
                 'CLOSE_CAPTURE_REF_MISMATCH')
        capture=load(path)
        _require(agrees(capture,schema_version=1,provider='twelve_data_basic',trade_date=day,session='regular',window=win,
                 capture_id=path.rsplit('/',1)[1][:-5],missing_symbols=[]),'CLOSE_CAPTURE_SCOPE_MISMATCH')
        facts=capture.get('qualified_facts')
        _require(type(facts) is list and bool(facts) and all(agrees(f,symbol=symbol) for f in facts),'CLOSE_CAPTURE_SYMBOL_MISMATCH')
    snap=dict(snapshot_path=rel(paths['snapshot']),snapshot_blob_sha=blob(paths['snapshot']),snapshot_id=hop.snapshot_id,schema_version=1)
    _require(agrees(load(paths['snapshot_latest']),**snap) and agrees(load(paths['proof_latest']),**snap,
                     proof_blob_sha=blob(paths['proof']),proof_path=rel(paths['proof'])),'CLOSE_POINTER_MISMATCH')
    _require(agrees(load(paths['proof']),schema_version=1,proof_kind='stock_dairy_deterministic_metrics',stage='close',trade_date=day,
                 data_plane_commit_sha=hop.compute_sha,snapshot={'path':snap['snapshot_path'],'blob_sha':snap['snapshot_blob_sha'],
                                                               'snapshot_id':hop.snapshot_id}),'CLOSE_PROOF_SOURCE_MISMATCH')
    _require(agrees(load(paths['missing']),schema_version=1,trade_date=day,last_mode='close',missing=[]) and
             agrees(load(paths['budget']),schema_version=1,trade_date=day),'CLOSE_STATE_SCOPE_MISMATCH')


@dataclass(frozen=True)
class _IsolatedHead:
    tree:str

def _load_isolated_head(doc):
    _keys(doc,'schema_version','kind','salt','evidence_base','base','head','tree','candidate_parents',
          'native_path','native_blob','manifest','outputs','trade_dates','advance',
          'native_selector_blob','native_admission_blob','frozen_appendix_commit')
    advance=doc['advance']
    _keys(advance,'kind','tree')
    _require(advance['kind']=='exact_durable_current_head_isolation','ISOLATED_HEAD_TYPE')
    if not isinstance(doc['candidate_parents'],list) or len(doc['candidate_parents']) not in (1,2):
        raise ValueError('PRIVATE_PROFILE_PARENT_SHAPE')
    parents=tuple(_match(OID,i) for i in doc['candidate_parents'])
    base=_match(OID,doc['base'])
    frozen=_match(OID,doc['frozen_appendix_commit'])
    old={k:v for k,v in doc.items() if k not in ('native_selector_blob','native_admission_blob','frozen_appendix_commit')}
    old.update(schema_version=1,base=doc['evidence_base'],candidate_parents=[doc['evidence_base']],advance=None)
    plan=load_profile(json.dumps(old).encode())
    _require(len({base,plan.evidence_base,plan.head,frozen})==4 and len(set(parents))==len(parents)
             and base in parents and plan.head not in parents,'ISOLATED_EXACT_OBJECT_SCOPE')
    return replace(plan, base=base,candidate_parents=parents,advance=_IsolatedHead(_match(OID,advance['tree'])),
                   native_selector_blob=_match(OID,doc['native_selector_blob']),
                   native_admission_blob=_match(OID,doc['native_admission_blob']),
                   frozen_appendix_commit=frozen)

def _admission_evidence_refs(root,plan,admission):
    admitted=_strict_json(_git(root,'show',plan.evidence_base+':'+admission))
    _require(type(admitted) is dict and type(admitted.get('workflow_evidence')) is dict,
             'ISOLATED_ADMISSION_SHAPE')
    refs=(admitted.get('envelope_ref'),admitted['workflow_evidence'].get('terminal_ref'))
    evidence={}
    for ref in refs:
        _keys(ref,'path','blob_sha')
        path=_match(re.compile(r'capture-evidence/[a-zA-Z0-9_./-]+'),ref['path'])
        _require(not any(p in ('','.','..') for p in path.split('/')) and path not in evidence,
                 'ISOLATED_ADMISSION_PATH_SCOPE')
        evidence[path]=_match(OID,ref['blob_sha'])
    return evidence

_STORE_INVENTORY='data/market-data/'+'collector-state/market-data-inventory.json'

def _inventory_has_no_appendix_index(root,commit,plan):
    inventory=_strict_json(_git(root,'show',commit+':'+_STORE_INVENTORY))
    _keys(inventory,'schema_version','generated_at','market_fact_authority','purpose','daily_series_summary',
          'session_capture_summary','stage_snapshot_summary','extended_hours_probe_summary')
    _require(type(inventory['schema_version']) is int and inventory['schema_version']==1 and
             inventory['market_fact_authority'] is False and type(inventory['session_capture_summary']) is list and
             type(inventory['stage_snapshot_summary']) is list,'ISOLATED_INVENTORY_SCHEMA')
    daily=inventory['daily_series_summary']
    _keys(daily,'series_count','provider_series_counts','series')
    _require(type(daily['series']) is list and type(daily['series_count']) is int and
             daily['series_count']==len(daily['series']),'ISOLATED_DAILY_INVENTORY_SHAPE')
    def records(doc):
        if isinstance(doc,dict):
            for key,value in doc.items():
                yield key
                yield from records(value)
        elif isinstance(doc,list):
            for value in doc:
                yield from records(value)
        elif isinstance(doc,str):
            yield doc
    _require(not any(any(term in x.lower() for term in ('soxx-prior-close-v1','soxx_public_web_price_appendix',
                     'supplement','appendix')) for x in records(inventory)),
                     'ISOLATED_APPENDIX_ALREADY_INDEXED')
    for row in daily['series']:
        _require(type(row) is dict and type(row.get('symbol')) is str and type(row.get('index_path')) is str,
                 'ISOLATED_DAILY_INVENTORY_ROW')
        if row['symbol'].upper()!='SOXX':
            _require('/SOXX/' not in row['index_path'].upper(),'ISOLATED_DAILY_TARGET_OR_UNCERTAIN_INDEX')
            continue
        provider=row.get('provider')
        _require(provider in ('twelve_data_basic','stockanalysis_spglobal_historical_html') and
                 row['index_path']==f'series/daily/{provider}/SOXX/_index.json' and
                 _day(row.get('first_trade_date'))<=_day(row.get('last_trade_date'))<plan.trade_dates[0],
                 'ISOLATED_DAILY_TARGET_OR_UNCERTAIN_INDEX')

def _verify_isolated_head(root,plan):
    """Prove no scoped history drift, without qualifying any unrelated retained change."""
    base,evidence=plan.base,plan.evidence_base
    def g(*a): return _git(root,*a).decode('ascii').strip()
    _require(g('rev-parse',base+'^{tree}')==plan.advance.tree,'ISOLATED_BASE_TREE_DRIFT')
    _git(root,'merge-base','--is-ancestor',evidence,base)
    # Production Store's normal writes are single-parent. Reject any ambiguous
    # merge, missing ancestry or unexpectedly large history instead of trusting it.
    commits=g('rev-list','--parents','--max-count=257',evidence+'..'+base).splitlines()
    _require(1<=len(commits)<=256,'ISOLATED_HISTORY_BOUND')
    cursor=base
    for row in commits:
        nodes=row.split()
        _require(len(nodes)==2 and nodes[0]==cursor,'ISOLATED_NONLINEAR_HISTORY')
        cursor=nodes[1]
    _require(cursor==evidence,'ISOLATED_ANCESTRY_INCOMPLETE')
    for commit in (evidence,base):
        _verify_native_selection(root,commit,plan)
    inventory_edits=g('log','--full-history','--format=%H','--no-renames',evidence+'..'+base,'--',_STORE_INVENTORY).splitlines()
    for commit in sorted({evidence,base,*inventory_edits}):
        _inventory_has_no_appendix_index(root,commit,plan)
    prefix='data/market-data/'
    active=prefix+'series-active/daily/twelve_data_basic/SOXX.json'
    admissions=prefix+'series-admissions/daily/twelve_data_basic/SOXX'
    versions=prefix+'series-versions/daily/twelve_data_basic/SOXX'
    admission=admissions+'/'+plan.native_admission_blob+'.json'
    linked=_admission_evidence_refs(root,plan,admission)
    for commit in (evidence,base):
        for path,blob in linked.items():
            _require(g('ls-tree',commit,'--',path)==f'100644 blob {blob}\t{path}',
                     'ISOLATED_NATIVE_EVIDENCE_DRIFT')
        snapshot=_strict_json(_git(root,'show',commit+':'+plan.native_path))
        rows=snapshot.get('records') if type(snapshot) is dict else None
        _require(type(rows) is list and bool(rows) and all(type(x) is dict for x in rows),
                 'ISOLATED_NATIVE_DATE_SHAPE')
        dates=[_day(x.get('datetime')) for x in rows]
        _require(len(set(dates))==len(dates) and not set(plan.trade_dates).intersection(dates),
                 'ISOLATED_NATIVE_TARGET_OCCUPIED')
    for p in plan.blobs:
        _require(not g('ls-tree',evidence,'--',p) and not g('ls-tree',base,'--',p),'ISOLATED_APPENDIX_PATH_OCCUPIED')
    protected=(active,admissions,versions,*linked,*plan.blobs,
               ':(icase,glob)'+prefix+'series/daily/*/SOXX/**',
               ':(icase,glob)'+prefix+'series-active/daily/*/SOXX.json',
               ':(icase,glob)'+prefix+'series-admissions/daily/*/SOXX/**',
               ':(icase,glob)'+prefix+'series-versions/daily/*/SOXX/**',
               ':(icase,glob)'+prefix+'recovery/**/*soxx*',
               ':(icase,glob)'+prefix+'recovery/**/*soxx*/**')
    history=g('log','--full-history','--format=%H','--no-renames',evidence+'..'+base,'--',*protected)
    _require(not history,'ISOLATED_PROTECTED_HISTORY_CHANGED')
