#!/usr/bin/env python3
"""Read-only local Store evidence audit; never performs admission or provider I/O."""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
from datetime import date
from pathlib import Path

class AuditError(ValueError):
    pass

HEX = re.compile(r'^[0-9a-f]{40}$')
HEX256 = re.compile(r'^[0-9a-f]{64}$')
SYMBOL = re.compile(r'^[A-Z][A-Z0-9.-]{0,9}$')
ENV = {'PATH':'/usr/bin:/bin', 'HOME':'/dev/null', 'GIT_CONFIG_NOSYSTEM':'1',
       'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_CONFIG_SYSTEM':'/dev/null',
       'GIT_TERMINAL_PROMPT':'0', 'GIT_NO_LAZY_FETCH':'1', 'GIT_ALLOW_PROTOCOL':'file',
       'GIT_NO_REPLACE_OBJECTS':'1', 'LC_ALL':'C'}

def _audit(repo, commit, trade_date, target_date, symbols, conflict_path, expected_conflict_blob=None):
    repo = Path(repo).resolve()
    if not isinstance(commit,str) or not HEX.fullmatch(commit) or not isinstance(symbols,list) or not symbols or not all(isinstance(s,str) and SYMBOL.fullmatch(s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise AuditError('invalid commit or symbol selection')
    try:
        if date.fromisoformat(trade_date).isoformat()!=trade_date or date.fromisoformat(target_date).isoformat()!=target_date:
            raise ValueError('date')
    except (ValueError, TypeError) as exc: raise AuditError('invalid date') from exc
    source_repo=Path(__file__).resolve().parents[1]
    def git(*args, location=repo):
        result=subprocess.run(['/usr/bin/git','-C',str(location),*args],env=ENV,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if result.returncode: raise AuditError('required local git object unavailable')
        return result.stdout
    if git('rev-parse','--verify',commit+'^{commit}').decode().strip()!=commit:
        raise AuditError('commit identity mismatch')
    def read(path, expected=None, snapshot=None, location=repo):
        record=git('ls-tree','-z',snapshot or commit,'--',path,location=location)
        parts=record.rstrip(b'\0').split(b'\t',1)
        if len(parts)!=2 or parts[1].decode()!=path: raise AuditError('required exact path unavailable')
        info=parts[0].decode().split()
        if len(info)!=3 or info[0]!='100644' or info[1]!='blob': raise AuditError('path is not regular blob')
        blob=info[2]
        if expected is not None and blob!=expected: raise AuditError('expected blob mismatch')
        raw=git('cat-file','blob',blob,location=location)
        if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=blob: raise AuditError('blob bytes mismatch')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('duplicate member')
                result[key]=value
            return result
        def invalid_constant(_value): raise ValueError('non-finite number')
        def finite_float(value):
            number=float(value)
            if not math.isfinite(number): raise ValueError('non-finite number')
            return number
        try: return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite_float), blob
        except (ValueError,UnicodeDecodeError) as exc: raise AuditError('invalid JSON') from exc
    def check_index_layout(index):
        metas=index.get('shards')
        if not isinstance(metas,list): raise AuditError('invalid shard index')
        cursor=0; names=set()
        for meta in metas:
            if not isinstance(meta,dict): raise AuditError('invalid shard index')
            name=meta.get('path'); sha=meta.get('blob_sha'); count=meta.get('count')
            if (not isinstance(name,str) or not re.fullmatch(r'[0-9]{6}\.json',name) or name in names
                    or not isinstance(sha,str) or not HEX.fullmatch(sha)
                    or type(count) is not int or count<=0 or type(meta.get('start')) is not int
                    or type(meta.get('end')) is not int or type(meta.get('byte_length')) is not int
                    or type(meta.get('sealed')) is not bool or not isinstance(meta.get('json_sha256'),str)
                    or not HEX256.fullmatch(meta['json_sha256']) or meta['start']!=cursor or meta['end']!=cursor+count-1):
                raise AuditError('invalid shard index')
            names.add(name); cursor+=count
        if type(index.get('record_count')) is not int or index['record_count']!=cursor:
            raise AuditError('invalid shard index')
    def check_row(row):
        if not isinstance(row,dict): raise AuditError('invalid series row')
        day=row.get('trade_date'); close=row.get('close')
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day): raise AuditError('invalid row date')
        if date.fromisoformat(day).isoformat()!=day: raise AuditError('invalid row date')
        if type(close) not in (int,float) or not math.isfinite(close): raise AuditError('invalid row close')
        return day
    def collection_identity(snapshot):
        if not isinstance(snapshot,str) or not HEX.fullmatch(snapshot) or git('rev-parse','--verify',snapshot+'^{commit}',location=source_repo).decode().strip()!=snapshot:
            raise AuditError('collection contract unavailable')
        universe,_=read('config/collection-universe.json',snapshot=snapshot,location=source_repo)
        entries=universe.get('daily_series') if isinstance(universe,dict) else None
        if not isinstance(entries,list) or not entries: raise AuditError('collection contract invalid')
        mapping={}
        for entry in entries:
            if not isinstance(entry,dict) or not isinstance(entry.get('symbol'),str) or not SYMBOL.fullmatch(entry['symbol']) or entry['symbol'] in mapping or entry.get('asset_class') not in {'stocks','etf'}:
                raise AuditError('collection contract invalid')
            mapping[entry['symbol']]=(entry['asset_class'],entry.get('ticker_effective_at'))
        return mapping
    source_head=git('rev-parse','HEAD',location=source_repo).decode().strip()
    current_identity=collection_identity(source_head)
    def check_index_identity(index,symbol,identity):
        if symbol not in identity: raise AuditError('series absent from collection contract')
        asset,effective=identity[symbol]
        expected={'provider':'twelve_data_basic','symbol':symbol,'asset_class':asset,'session':'regular','interval':'1d','series_semantics':'daily_regular_ohlcv','adjustment_semantics':'provider_reported','series_id':f'twelve_data_basic:{symbol}:regular:1d','identity_effective_from':effective}
        if not isinstance(index,dict) or type(index.get('schema_version')) is not int or index['schema_version']!=1 or any(index.get(k)!=v for k,v in expected.items()):
            raise AuditError('series identity invalid')
        if 'sort_order' in index and index['sort_order']!='trade_date_asc': raise AuditError('series sort order invalid')
        if 'lineage_segments' in index and not isinstance(index['lineage_segments'],list): raise AuditError('series lineage invalid')
        for field in ('identity_effective_from','first_trade_date','last_trade_date'):
            value=index.get(field)
            if value is not None and (not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) or date.fromisoformat(value).isoformat()!=value):
                raise AuditError('series date invalid')
    results=[]
    for symbol in symbols:
        base=f'data/market-data/series/daily/twelve_data_basic/{symbol}'
        idx, blob=read(base+'/_index.json')
        check_index_identity(idx,symbol,current_identity)
        check_index_layout(idx)
        rows=[]
        for meta in idx['shards']:
            name=meta.get('path') if isinstance(meta,dict) else None
            if not isinstance(name,str) or not re.fullmatch(r'[0-9]{6}\.json',name): raise AuditError('invalid shard path')
            doc,actual_blob=read(base+'/'+name,meta.get('blob_sha'))
            if not isinstance(doc,dict) or type(doc.get('schema_version')) is not int or doc['schema_version']!=1 or type(doc.get('start')) is not int or doc.get('series_id')!=idx['series_id'] or not isinstance(doc.get('records'),list): raise AuditError('shard identity invalid')
            canonical=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            records=doc['records']; start=len(rows)
            fields={'start':start,'end':start+len(records)-1,'count':len(records),'byte_length':len(canonical),'json_sha256':hashlib.sha256(canonical).hexdigest()}
            if doc.get('start')!=start or any(meta.get(k)!=v for k,v in fields.items()): raise AuditError('shard metadata mismatch')
            rows.extend(records)
        dates=[check_row(row) for row in rows]
        if dates!=sorted(set(dates)) or len(rows)!=idx.get('record_count') or (dates and (idx.get('first_trade_date')!=dates[0] or idx.get('last_trade_date')!=dates[-1])): raise AuditError('series dates or count mismatch')
        results.append({'symbol':symbol,'index_blob':blob,'record_count':len(rows),'last_trade_date':dates[-1] if dates else None,'target_date_present':target_date in dates})
    pattern=rf'data/market-data/collector-state/{trade_date[:7]}/{trade_date}-previous_session_eod-[0-9a-f]{{16}}-conflicts\.json'
    if not isinstance(conflict_path,str) or not re.fullmatch(pattern,conflict_path): raise AuditError('conflict path invalid')
    conflict,conflict_blob=read(conflict_path,expected_conflict_blob)
    try:
        inv=conflict['invocation']; records=conflict['conflicts']; count=conflict['conflict_record_count']; displayed=conflict['conflict_diagnostic_record_count']; failures=conflict['failure_target_count']
        valid=(type(conflict['schema_version']) is int and conflict['schema_version']==1 and conflict['diagnostic_authority']=='non_authoritative_conflict_evidence' and conflict['trade_date']==trade_date and inv['mode']=='previous_session_eod' and isinstance(inv['invocation_id'],str) and bool(inv['invocation_id']) and all(HEX.fullmatch(conflict[k]) for k in ('compute_sha','store_input_sha')) and isinstance(records,list) and all(type(n) is int and n>=0 for n in (count,displayed,failures)) and displayed==len(records) and count>=displayed and type(conflict['conflict_diagnostics_truncated']) is bool and conflict['conflict_diagnostics_truncated']==(count>displayed) and conflict_path.endswith(hashlib.sha256(inv['invocation_id'].encode()).hexdigest()[:16]+'-conflicts.json'))
    except (KeyError,TypeError): valid=False
    if not valid: raise AuditError('conflict diagnostic identity or counts invalid')
    old_snapshot=conflict['store_input_sha']
    if git('rev-parse','--verify',old_snapshot+'^{commit}').decode().strip()!=old_snapshot:
        raise AuditError('conflict old snapshot mismatch')
    old_identity=collection_identity(conflict['compute_sha'])
    def digest(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    for item in records:
        try:
            symbol=item['symbol']; day=item['trade_date']; ref=item['store_source']; request=item['request_parameters']
            if not isinstance(symbol,str) or not SYMBOL.fullmatch(symbol) or date.fromisoformat(day).isoformat()!=day or day>=trade_date: raise ValueError('identity')
            base=f'data/market-data/series/daily/twelve_data_basic/{symbol}'
            if ref['provider']!='twelve_data_basic' or ref['index_path']!='series/daily/twelve_data_basic/'+symbol+'/_index.json' or not isinstance(ref['index_blob_sha'],str) or not HEX.fullmatch(ref['index_blob_sha']): raise ValueError('index ref')
            if request['provider']!='twelve_data_basic' or request['symbol']!=symbol or request['trade_date_exclusive_upper_bound']!=trade_date: raise ValueError('request identity')
            index,_=read(base+'/_index.json',ref['index_blob_sha'],old_snapshot)
            check_index_identity(index,symbol,old_identity)
            shard=ref['shard']; name=shard['path'].rsplit('/',1)[-1]
            if not re.fullmatch(r'[0-9]{6}\.json',name) or shard['path']!='series/daily/twelve_data_basic/'+symbol+'/'+name or not isinstance(shard['blob_sha'],str) or not HEX.fullmatch(shard['blob_sha']): raise ValueError('shard path')
            check_index_layout(index)
            metas=[m for m in index['shards'] if m.get('path')==name]
            if len(metas)!=1 or metas[0]['blob_sha']!=shard['blob_sha']: raise ValueError('shard membership')
            cursor=0; rows=None; start=None
            for meta in index['shards']:
                doc,_=read(base+'/'+meta['path'],meta['blob_sha'],old_snapshot)
                if not isinstance(doc,dict) or not isinstance(doc.get('records'),list): raise ValueError('shard structure')
                candidate=doc['records']; canonical=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                if type(doc.get('schema_version')) is not int or doc['schema_version']!=1 or type(doc.get('start')) is not int or doc['series_id']!=index['series_id'] or doc['start']!=cursor or meta['start']!=cursor or meta['end']!=cursor+len(candidate)-1 or meta['count']!=len(candidate) or meta['byte_length']!=len(canonical) or meta['json_sha256']!=hashlib.sha256(canonical).hexdigest(): raise ValueError('shard metadata')
                for entry in candidate: check_row(entry)
                if meta['path']==name:
                    start=cursor; rows=candidate; break
                cursor+=len(candidate)
            row_number=ref['row_number']
            if rows is None or type(row_number) is not int or not start<=row_number<start+len(rows): raise ValueError('shard position')
            old=rows[row_number-start]
            check_row(old)
            if old['trade_date']!=day or digest(old)!=item['old_row_sha256']: raise ValueError('old row mismatch')
            fields=item['fields']; seen=set(); variants=[dict(old)]
            if not isinstance(fields,list) or not fields: raise ValueError('missing differences')
            for field in fields:
                name=field['field']; prior=field['old_value']; new=field['new_value']
                if name not in {'open','high','low','close','volume','source_timestamp'} or name in seen: raise ValueError('field identity')
                seen.add(name)
                if field['old_type']!=type(prior).__name__ or field['new_type']!=type(new).__name__ or type(old.get(name)) is not type(prior) or old.get(name)!=prior or (type(prior) is type(new) and prior==new): raise ValueError('field values')
                if field['old_sha256']!=digest({'type':type(prior).__name__,'value':prior}) or field['new_sha256']!=digest({'type':type(new).__name__,'value':new}): raise ValueError('field digest')
                for variant in variants: variant[name]=new
                if new is None:
                    without=[dict(variant) for variant in variants]
                    for variant in without: variant.pop(name,None)
                    variants+=without
            if item['new_row_sha256'] not in {digest(variant) for variant in variants}: raise ValueError('new row digest')
        except (KeyError,TypeError,ValueError,IndexError) as exc:
            raise AuditError('conflict entry or old snapshot binding invalid') from exc
    return {'scope':'local_store_evidence_only','store_commit':commit,'target_date':target_date,'series':results,'conflict_blob':conflict_blob,'conflict_old_snapshot':old_snapshot,'conflict_count':count,'verified_conflict_entries':len(records),'limitations':['provider new values not independently verified','no admission or production decision']}

def audit(repo, commit, trade_date, target_date, symbols, conflict_path, expected_conflict_blob=None):
    try:
        return _audit(repo,commit,trade_date,target_date,symbols,conflict_path,expected_conflict_blob)
    except AuditError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError('invalid or unavailable local audit input') from exc

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--store',required=True); p.add_argument('--commit',required=True); p.add_argument('--trade-date',required=True); p.add_argument('--target-date',required=True); p.add_argument('--symbols',nargs='+',required=True); p.add_argument('--conflict-path',required=True); p.add_argument('--conflict-blob',required=True)
    a=p.parse_args()
    if not HEX.fullmatch(a.conflict_blob): p.error('conflict blob must be a full SHA')
    try: print(json.dumps(audit(a.store,a.commit,a.trade_date,a.target_date,a.symbols,a.conflict_path,a.conflict_blob),sort_keys=True))
    except AuditError: p.exit(2,'local evidence audit failed: invalid or unavailable input\n')
if __name__=='__main__': main()
