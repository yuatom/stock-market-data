#!/usr/bin/env python3
"""Read-only local Store evidence audit; never performs admission or provider I/O."""
import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path

class AuditError(ValueError):
    pass

HEX = re.compile(r'^[0-9a-f]{40}$')
SYMBOL = re.compile(r'^[A-Z][A-Z0-9.-]{0,9}$')
ENV = {'PATH':'/usr/bin:/bin', 'HOME':'/dev/null', 'GIT_CONFIG_NOSYSTEM':'1',
       'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_CONFIG_SYSTEM':'/dev/null',
       'GIT_TERMINAL_PROMPT':'0', 'GIT_NO_LAZY_FETCH':'1', 'GIT_ALLOW_PROTOCOL':'file',
       'GIT_NO_REPLACE_OBJECTS':'1', 'LC_ALL':'C'}

def audit(repo, commit, trade_date, target_date, symbols, conflict_path, expected_conflict_blob=None):
    repo = Path(repo).resolve()
    if not HEX.fullmatch(commit) or not symbols or len(set(symbols)) != len(symbols) or not all(SYMBOL.fullmatch(s) for s in symbols):
        raise AuditError('invalid commit or symbol selection')
    try:
        if date.fromisoformat(trade_date).isoformat()!=trade_date or date.fromisoformat(target_date).isoformat()!=target_date:
            raise ValueError('date')
    except (ValueError, TypeError) as exc: raise AuditError('invalid date') from exc
    def git(*args):
        result=subprocess.run(['/usr/bin/git','-C',str(repo),*args],env=ENV,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if result.returncode: raise AuditError('required local git object unavailable')
        return result.stdout
    if git('rev-parse','--verify',commit+'^{commit}').decode().strip()!=commit:
        raise AuditError('commit identity mismatch')
    def read(path, expected=None):
        record=git('ls-tree','-z',commit,'--',path)
        parts=record.rstrip(b'\0').split(b'\t',1)
        if len(parts)!=2 or parts[1].decode()!=path: raise AuditError('required exact path unavailable')
        info=parts[0].decode().split()
        if len(info)!=3 or info[0]!='100644' or info[1]!='blob': raise AuditError('path is not regular blob')
        blob=info[2]
        if expected is not None and blob!=expected: raise AuditError('expected blob mismatch')
        raw=git('cat-file','blob',blob)
        if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=blob: raise AuditError('blob bytes mismatch')
        try: return json.loads(raw), blob
        except (ValueError,UnicodeDecodeError) as exc: raise AuditError('invalid JSON') from exc
    results=[]
    for symbol in symbols:
        base=f'data/market-data/series/daily/twelve_data_basic/{symbol}'
        idx, blob=read(base+'/_index.json')
        expected={'provider':'twelve_data_basic','symbol':symbol,'asset_class':'stocks','session':'regular','interval':'1d','series_semantics':'daily_regular_ohlcv','adjustment_semantics':'provider_reported','series_id':f'twelve_data_basic:{symbol}:regular:1d'}
        if not isinstance(idx,dict) or any(idx.get(k)!=v for k,v in expected.items()) or not isinstance(idx.get('shards'),list): raise AuditError('series identity invalid')
        rows=[]
        for meta in idx['shards']:
            name=meta.get('path') if isinstance(meta,dict) else None
            if not isinstance(name,str) or not re.fullmatch(r'[0-9]{6}\.json',name): raise AuditError('invalid shard path')
            doc,actual_blob=read(base+'/'+name,meta.get('blob_sha'))
            if not isinstance(doc,dict) or doc.get('series_id')!=idx['series_id'] or not isinstance(doc.get('records'),list): raise AuditError('shard identity invalid')
            canonical=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            records=doc['records']; start=len(rows)
            fields={'start':start,'end':start+len(records)-1,'count':len(records),'byte_length':len(canonical),'json_sha256':hashlib.sha256(canonical).hexdigest()}
            if doc.get('start')!=start or any(meta.get(k)!=v for k,v in fields.items()): raise AuditError('shard metadata mismatch')
            rows.extend(records)
        try: dates=[date.fromisoformat(row['trade_date']).isoformat() for row in rows]
        except (ValueError,KeyError,TypeError) as exc: raise AuditError('invalid row date') from exc
        if dates!=sorted(set(dates)) or len(rows)!=idx.get('record_count') or (dates and (idx.get('first_trade_date')!=dates[0] or idx.get('last_trade_date')!=dates[-1])): raise AuditError('series dates or count mismatch')
        results.append({'symbol':symbol,'index_blob':blob,'record_count':len(rows),'last_trade_date':dates[-1] if dates else None,'target_date_present':target_date in dates})
    pattern=rf'data/market-data/collector-state/{trade_date[:7]}/{trade_date}-previous_session_eod-[0-9a-f]{{16}}-conflicts\.json'
    if not re.fullmatch(pattern,conflict_path): raise AuditError('conflict path invalid')
    conflict,conflict_blob=read(conflict_path,expected_conflict_blob)
    try:
        inv=conflict['invocation']; records=conflict['conflicts']; count=conflict['conflict_record_count']; displayed=conflict['conflict_diagnostic_record_count']; failures=conflict['failure_target_count']
        valid=(conflict['schema_version']==1 and conflict['diagnostic_authority']=='non_authoritative_conflict_evidence' and conflict['trade_date']==trade_date and inv['mode']=='previous_session_eod' and isinstance(inv['invocation_id'],str) and bool(inv['invocation_id']) and all(HEX.fullmatch(conflict[k]) for k in ('compute_sha','store_input_sha')) and isinstance(records,list) and all(type(n) is int and n>=0 for n in (count,displayed,failures)) and displayed==len(records) and count>=displayed and conflict['conflict_diagnostics_truncated']==(count>displayed) and conflict_path.endswith(hashlib.sha256(inv['invocation_id'].encode()).hexdigest()[:16]+'-conflicts.json'))
    except (KeyError,TypeError): valid=False
    if not valid: raise AuditError('conflict diagnostic identity or counts invalid')
    return {'scope':'local_store_evidence_only','store_commit':commit,'target_date':target_date,'series':results,'conflict_blob':conflict_blob,'conflict_count':count,'limitations':['conflict entries and referenced prior snapshots not independently verified','no provider verification','no admission or production decision']}

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--store',required=True); p.add_argument('--commit',required=True); p.add_argument('--trade-date',required=True); p.add_argument('--target-date',required=True); p.add_argument('--symbols',nargs='+',required=True); p.add_argument('--conflict-path',required=True); p.add_argument('--conflict-blob',required=True)
    a=p.parse_args()
    if not HEX.fullmatch(a.conflict_blob): p.error('conflict blob must be a full SHA')
    try: print(json.dumps(audit(a.store,a.commit,a.trade_date,a.target_date,a.symbols,a.conflict_path,a.conflict_blob),sort_keys=True))
    except (AuditError, subprocess.TimeoutExpired) as exc: p.exit(2,'local evidence audit failed: '+str(exc)+'\n')
if __name__=='__main__': main()
