"""Historical SOXX appendix only, with existing retained-reconciler hosted caller.

Owner dispatch identifies independently approved private receipt bytes; neither
the reader nor the Store credential establishes consent. No provider access.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from scripts.run_private_authority_route import _environment, _quiet, _source, STORE_URL
from publish_soxx_web_appendix import (
    COMPUTE, STORE, _require_local_and_remote, _require_origins, _quiet_store_checkout)
from push_proof_store_commit import verify_exact_output_closure
from soxx_appendix_exact_preflight import _git, _strict_json, load_profile, preflight

WORKFLOW_REF=COMPUTE+'/.github/workflows/reconcile-retained-daily.yml@refs/heads/main'
DOMAIN=b'RETAINED-SOXX-WEB-APPENDIX-DISPATCH-V1\0'
RECEIPT='reconciliation-authority/receipt.json'
OWNER_ID='10399225'
CLAIM_DOMAIN=b'RETAINED-SOXX-WEB-APPENDIX-SINGLE-USE-V1\0'
CLAIM_LANE=b'approved-frozen-historical-appendix'
SHA=re.compile(r'[0-9a-f]{40}\Z')
HASH=re.compile(r'[0-9a-f]{64}\Z')

def need(ok):
    if not ok:
        raise ValueError('RETAINED_SOXX_AUTHORITY_REJECTED')

def public_request(env):
    sha=env.get('GITHUB_SHA','')
    rid=env.get('GITHUB_RUN_ID','')
    need(SHA.fullmatch(sha) and re.fullmatch(r'[1-9][0-9]{0,19}',rid))
    expected=dict(GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY=COMPUTE,GITHUB_REF='refs/heads/main',
                  GITHUB_ACTOR='yuatom',GITHUB_ACTOR_ID=OWNER_ID,GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_RUN_ATTEMPT='1',
                  GITHUB_WORKFLOW_REF=WORKFLOW_REF,GITHUB_WORKFLOW_SHA=sha,EXPECTED_COMPUTE_SHA=sha)
    need(all(env.get(k)==v for k,v in expected.items()))
    raw=env.get('REQUEST_JSON','').encode('utf-8')
    need(0<len(raw)<=192)
    obj=_strict_json(raw)
    need(type(obj) is dict and set(obj)=={'kind','commitment'} and obj['kind']=='soxx_web_appendix_recovery_v1'
         and type(obj['commitment']) is str and HASH.fullmatch(obj['commitment']))
    return sha,rid,obj['commitment']

@dataclass(frozen=True)
class _ReceiptValidity:
    start:datetime
    end:datetime

def _receipt_window(obj):
    def parse(s):
        need(type(s) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z',s))
        return datetime.fromisoformat(s[:-1]+'+00:00')
    start,end=parse(obj['valid_from']),parse(obj['valid_until'])
    need(timedelta(0)<end-start<=timedelta(minutes=30))
    return _ReceiptValidity(start,end)

def require_current_receipt(receipt_store,ref,expected,approval):
    # This is the last remote read before the UTC window check and the only write.
    need(_resolve_ref(receipt_store,ref)==expected)
    need(approval.start<=datetime.now(timezone.utc)<approval.end)

def bound_receipt(raw,sha,commitment,now):
    need(type(raw) is bytes and 0<len(raw)<=24000 and type(commitment) is str and HASH.fullmatch(commitment)
         and hmac.compare_digest(hashlib.sha256(DOMAIN+raw).hexdigest(),commitment))
    obj=_strict_json(raw)
    keys={'schema_version','kind','compute_sha','workflow_ref','action','valid_from','valid_until','profile'}
    need(type(obj) is dict and set(obj)==keys and type(obj['schema_version']) is int and obj['schema_version']==1 and
         obj['kind']=='soxx_retained_reconciliation_receipt' and obj['compute_sha']==sha and obj['workflow_ref']==WORKFLOW_REF
         and obj['action']=='exact_price_appendix_only' and type(obj['profile']) is str)
    need(json.dumps(obj,separators=(',',':'),sort_keys=True).encode()==raw)
    approval=_receipt_window(obj)
    need(approval.start<=now<approval.end)
    # A protected receipt must commit to the current-head route. Runtime checks
    # salt syntax only; independent staging must use fresh CSPRNG bytes.
    profile=obj['profile'].encode()
    inner=_strict_json(profile)
    need(type(inner) is dict and type(inner.get('schema_version')) is int and inner['schema_version']==3
         and type(inner.get('salt')) is str and HASH.fullmatch(inner['salt']) and inner['salt']!='0'*64)
    return load_profile(profile)

def _private_metadata(token):
    path='/repos/'+STORE
    conn=HTTPSConnection('api.github.com',timeout=20)
    try:
        conn.request('GET',path,headers={'Authorization':'Bearer '+token,'User-Agent':'retained-soxx-reconciliation'})
        resp=conn.getresponse()
        raw=resp.read(65537)
        need(resp.status==200 and len(raw)<=65536)
        obj=_strict_json(raw)
        need(type(obj) is dict and obj.get('full_name')==STORE and obj.get('private') is True)
    finally:
        conn.close()

def _resolve_ref(root,ref):
    rows=_git(root,'ls-remote','--refs','--exit-code','origin',ref).decode('ascii').split()
    need(len(rows)==2 and rows[1]==ref and SHA.fullmatch(rows[0]))
    return rows[0]

def _materialize_receipt(root,sha,commitment):
    # Ref from independently approved public opaque commitment, never a sidecar-supplied path.
    ref=f'refs/heads/reconciliation-receipts/soxx-web-appendix/{sha}/{commitment}'
    _git(root,'init','--quiet')
    _git(root,'remote','add','origin',STORE_URL)
    oid=_resolve_ref(root,ref)
    _git(root,'fetch','--quiet','--no-tags','--no-recurse-submodules','origin',oid)
    need(_git(root,'rev-parse','FETCH_HEAD').decode().strip()==oid)
    def g(*a): return _git(root,*a).decode('ascii').strip()
    tree=g('ls-tree',oid,'--',RECEIPT)
    need(re.fullmatch(r'100644 blob [0-9a-f]{40}\t'+re.escape(RECEIPT),tree))
    raw=_git(root,'show',oid+':'+RECEIPT)
    plan=bound_receipt(raw,sha,commitment,datetime.now(timezone.utc))
    need(g('rev-list','--parents','-n','1',oid).split()==[oid,plan.base])
    need(not g('ls-tree',plan.base,'--',RECEIPT))
    delta=_git(root,'diff-tree','-z','-r','--no-renames','--name-status',plan.base,oid)
    need(delta==b'A\0'+RECEIPT.encode()+b'\0')
    return ref,oid,raw,plan

def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def _git_result(root,*args,raw=None,env=None):
    return subprocess.run(['git','--no-replace-objects','-C',str(root),*args],
                          input=raw,env=env,capture_output=True,timeout=30,check=False)

def _output(root,*args,raw=None,env=None):
    out=_git_result(root,*args,raw=raw,env=env)
    need(out.returncode==0)
    return out.stdout

def claim_ref():
    # One lane for the existing frozen appendix, regardless of dates/base/run.
    return 'refs/heads/reconciliation-claims/soxx-web-appendix/'+hashlib.sha256(CLAIM_DOMAIN+CLAIM_LANE).hexdigest()

def _require_absent(root,ref):
    existing=_git_result(root,'ls-remote','--refs','--exit-code','origin',ref)
    need(existing.returncode==2 and existing.stdout==b'')

def _local_oid(root,*args,**kwargs):
    val=_output(root,*args,**kwargs).decode('ascii').strip()
    need(SHA.fullmatch(val))
    return val

def _local_commit(root,tree,label,parent=None):
    local_author=dict(os.environ,GIT_AUTHOR_NAME='SOXX Retained Publication',GIT_COMMITTER_NAME='SOXX Retained Publication',
                      GIT_AUTHOR_EMAIL='retained-publication@users.noreply.github.com',GIT_COMMITTER_EMAIL='retained-publication@users.noreply.github.com')
    parents=('-p',parent) if parent else ()
    return _local_oid(root,'commit-tree',tree,*parents,'-m',label,env=local_author)

def _prove_publication_tree(root,commit,plan):
    def text(*a): return _git(root,*a).decode('ascii').strip()
    need(text('rev-list','--parents','-n','1',commit).split()==[commit,plan.base])
    need(text('rev-parse',commit+'^{tree}')==plan.tree)
    delta=_git(root,'diff-tree','-z','-r','--no-renames','--name-status',plan.base,commit).decode('ascii').split('\0')
    need(len(plan.blobs)==4 and delta[-1]=='' and len(delta)==9 and
         dict(zip(delta[1::2],delta[::2]))==dict.fromkeys(plan.blobs,'A'))
    for path,expected in plan.blobs.items():
        need(text('ls-tree',commit,'--',path)==f'100644 blob {expected}\t{path}')
    verify_exact_output_closure(root,plan.base,commit,plan.manifest)

def _check_push_ack(stdout,refs):
    changed={}
    for line in stdout.decode('ascii').splitlines():
        if '\t' not in line:
            continue
        parts=line.split('\t')
        need(len(parts)==3 and len(parts[0])==1)
        _,sep,ref=parts[1].partition(':')
        need(sep and ref in refs and ref not in changed)
        changed[ref]=parts[0]
    need(changed==refs)

def _verify_store_publisher(root,refs,main,claim,claim_blob,body,plan):
    """Read-only after the single atomic write: never recreate missing refs."""
    def read_refs():
        return all(_resolve_ref(root,ref)==oid for ref,oid in refs.items())
    need(read_refs())
    _git(root,'fetch','--quiet','--no-tags','--no-recurse-submodules','origin',main)
    need(_git(root,'rev-parse','FETCH_HEAD').decode().strip()==main)
    _prove_publication_tree(root,main,plan)
    _git(root,'fetch','--quiet','--no-tags','--no-recurse-submodules','origin',claim)
    need(_git(root,'rev-parse','FETCH_HEAD').decode().strip()==claim and
         _git(root,'ls-tree',claim,'--','claim.json').decode().strip()==f'100644 blob {claim_blob}\tclaim.json' and
         _git(root,'show',claim+':claim.json')==body and read_refs())
    return {'status':'durability_verified','store_commit_sha':main}

def publish_atomically(root,compute_sha,actual_run,approved_commitment,receipt_oid,plan,*,before_push):
    """One atomic expected-old main CAS, create-only durability and fixed claim.

    Ref retention is an operator contract, not protection from an administrator
    able to delete refs and rewrite the repository. A lost ack is inspection only.
    """
    main_ref='refs/heads/main'
    ref=claim_ref()
    _require_absent(root,ref)
    need(_resolve_ref(root,main_ref)==plan.base)
    main=_local_commit(root,plan.tree,'data: append retained SOXX public-web prior-close evidence',parent=plan.base)
    _prove_publication_tree(root,main,plan)
    tag='refs/tags/market-data-read/'+main
    _require_absent(root,tag)
    scope=dict(action='exact_price_appendix_only',symbol='SOXX',trade_dates=plan.trade_dates,actor_id=OWNER_ID)
    body=_canonical(dict(schema_version=1,kind='soxx_retained_publication_claim',
          actor='yuatom',actor_id=OWNER_ID,run_id=actual_run,run_attempt=1,
          receipt_commitment=approved_commitment,receipt_commit=receipt_oid,
          compute_sha=compute_sha,expected_store_base=plan.base,store_candidate=plan.head,
          candidate_tree=plan.tree,evidence_base=plan.evidence_base,published_store_commit=main,scope=scope))
    blob=_local_oid(root,'hash-object','-w','--stdin',raw=body)
    tree=_local_oid(root,'mktree',raw=f'100644 blob {blob}\tclaim.json\n'.encode())
    claim=_local_commit(root,tree,'Retained appendix claim')
    refs={main_ref:main,tag:main,ref:claim}
    zero='0'*40
    leases={main_ref:plan.base,tag:zero,ref:zero}
    # The main lease is safe only for the exact validated direct child above.
    # The two zero leases are create-only. Any failure stops; no fallback.
    args=('push','--porcelain','--atomic',
          *('--force-with-lease='+r+':'+p for r,p in leases.items()),'origin',
          *(o+':'+r for r,o in refs.items()))
    before_push()
    ack=_output(root,*args)
    _check_push_ack(ack,{main_ref:' ',tag:'*',ref:'*'})
    return _verify_store_publisher(root,refs,main,claim,blob,body,plan)


def run(compute,receipt_store,store,env):
    sha,rid,commitment=public_request(env)
    token=env.get('MARKET_DATA_STORE_TOKEN','')
    need(type(token) is str and re.fullmatch(r'[A-Za-z0-9_]{1,1024}',token))
    with _environment():
        _source(compute,sha)
    with _environment(token):
        _private_metadata(token)
        ref,receipt_oid,raw,plan=_materialize_receipt(receipt_store,sha,commitment)
        _quiet_store_checkout(store,STORE_URL,plan)
        _require_origins(compute,store)
        preflight(store,plan)
        _require_local_and_remote(compute,store,sha,plan)
        def final_authority_check():
            # Receipt bytes were already validated against the owner-selected
            # commitment; parse only the pinned window before the late ref read.
            approval=_receipt_window(_strict_json(raw))
            require_current_receipt(receipt_store,ref,receipt_oid,approval)
        result=publish_atomically(store,sha,rid,commitment,receipt_oid,plan,before_push=final_authority_check)
        need(result.get('status')=='durability_verified')

def main():
    try:
        env=dict(os.environ)
        with _quiet(),tempfile.TemporaryDirectory(prefix='retained-soxx-private-') as t:
            root=Path(t)
            r,s=root/'receipt',root/'store'
            r.mkdir(mode=0o700); s.mkdir(mode=0o700)
            run(Path(__file__).resolve().parents[1],r,s,env)
        print('RETAINED_SOXX_APPENDIX_DURABILITY_VERIFIED')
        return 0
    except Exception:
        print('RETAINED_SOXX_APPENDIX_STOP_INSPECT_NO_RETRY')
        return 1

if __name__=='__main__':
    raise SystemExit(main())
