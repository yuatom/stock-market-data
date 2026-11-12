"""Consume a private same-job owner result to suppress duplicate SOXX fetches.

This is acquisition routing only, not permission to use data in research. The
actual owner produced the file after finite authority and hosted provenance
checks; this consumer additionally verifies its immutable authority commitment,
source, day, run, Store head and catalog. No self-supplied skip list is accepted.
"""
import os
from pathlib import Path
import stat
from datetime import datetime,timezone
from scripts import provider_basis_snapshot as B
from scripts.run_daily_basis_route import validate_authority_receipt,AUTH_PATH


def load_daily_basis_owner(store_root,day,filename,env):
    p=Path(filename).absolute()
    B._need(not any(x.is_symlink() for x in (p,*p.parents)))
    info=p.stat();B._need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600)
    d=B._parse(p.read_bytes());B._closed(d,('schema_version','kind','status','symbol','compute_sha','store_sha','execution_day','request_id','authority_binding','grant_sha256','selector','volume_policy'))
    B._need(d['schema_version']==1 and d['kind']=='trusted_daily_basis_delegation' and d['symbol']=='SOXX' and d['volume_policy']=='BLOCKED_UNRESOLVED')
    B._need(d['execution_day']==day and d['compute_sha']==env.get('MARKET_DATA_COMPUTE_SHA') and d['request_id']==env.get('GITHUB_RUN_ID','')+'-1')
    B._need(env.get('GITHUB_ACTIONS')=='true' and env.get('GITHUB_EVENT_NAME')=='schedule' and env.get('GITHUB_RUN_ATTEMPT')=='1' and env.get('GITHUB_REPOSITORY')=='yuatom/stock-market-data')
    root=Path(store_root).resolve();B._need(root.parts[-2:]==('data','market-data'))
    g=B._store(root.parent.parent);B._need(g.text('rev-parse','HEAD')==d['store_sha'])
    binding=d['authority_binding']
    unavailable=d['status']=='ACQUISITION_HELD_AUTHORITY_UNAVAILABLE'
    if unavailable:
        B._need(binding is None and d['grant_sha256'] is None and bool(env.get('DAILY_BASIS_GRANT_COMMITMENT')))
    else:
        B._closed(binding,('ref','commit','blob_sha','commitment'))
        B._need(binding['commitment']==env.get('DAILY_BASIS_GRANT_COMMITMENT') and binding['ref']==f'refs/heads/basis-daily-authority/{d["compute_sha"]}/{binding["commitment"]}')
        authority=validate_authority_receipt(B._blob(g,binding['commit'],AUTH_PATH,binding['blob_sha']),binding['commitment'],d['compute_sha'],now=datetime.now(timezone.utc).isoformat(),allow_expired_hold=d['status']=='ACQUISITION_HELD_GRANT_EXHAUSTED')
        ref=authority['grant_ref'];grant_raw=B._blob(g,ref['commit'],ref['path'],ref['blob_sha'])
        B._need(B.sha(grant_raw)==ref['sha256']==d['grant_sha256'])
        grant=B._parse(grant_raw)
        if d['status'] not in ('ACQUISITION_HELD_GRANT_EXHAUSTED','ACQUISITION_HELD_REVIEW_REQUIRED'):
            B._need(any(s['execution_day']==day for s in grant['slots']))
    catalog=B.catalog_path(B.PROVIDER,'SOXX')
    exists=bool(g.read('ls-tree','-z',d['store_sha'],'--',catalog))
    if d['status']=='ACQUISITION_HELD_PENDING_INITIAL_BASIS':B._need(not exists and d['selector'] is None)
    elif d['status'] in ('ACQUISITION_HELD_AUTHORITY_UNAVAILABLE','ACQUISITION_HELD_GRANT_EXHAUSTED','ACQUISITION_HELD_REVIEW_REQUIRED'):
        if exists:
            B._selection(d['selector'])
            B._need(B._parse(B._blob(g,d['store_sha'],catalog))=={k:v for k,v in d['selector'].items() if k!='read_sha'})
        else:B._need(d['selector'] is None)
    else:
        B._need(d['status']=='OWNED_IMMUTABLE_DAILY' and exists)
        B._selection(d['selector'])
        B._need(B._parse(B._blob(g,d['store_sha'],catalog))=={k:v for k,v in d['selector'].items() if k!='read_sha'})
        B._need(g.text('merge-base',d['selector']['read_sha'],d['store_sha'])==d['selector']['read_sha'])
    # This is the actual post-owner base, not the old invocation observation.
    os.environ['MARKET_DATA_STORE_INPUT_SHA']=d['store_sha']
    return {'SOXX':d['status']}
