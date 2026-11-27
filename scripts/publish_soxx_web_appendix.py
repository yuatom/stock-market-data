#!/usr/bin/env python3
"""Exact, protected, single-run SOXX web appendix publisher; not a data acquirer."""
import argparse
import base64
from http.client import HTTPSConnection, HTTPException
from pathlib import Path
import json
import hashlib
import tempfile
from contextlib import contextmanager
import os
import re
import sys

from soxx_appendix_exact_preflight import _git, _strict_json, load_profile, preflight
from verify_store_publication import StorePublicationError, verify_publication

COMPUTE='yuatom/stock-market-data'
STORE='yuatom/stock-market-data-store'
WORKFLOW_REF=COMPUTE+'/.github/workflows/retained-soxx-web-price-appendix.yml@refs/heads/main'
AUTHORITY='SOXX_WEB_APPENDIX_EXACT_RUN_AUTHORITY'
TOKEN='SOXX_WEB_APPENDIX_STORE_WRITER_TOKEN'
STATUS='soxx_public_web_price_appendix'
SHA=re.compile(r'[0-9a-f]{40}\Z')

PROFILE='SOXX_WEB_APPENDIX_PRIVATE_PROFILE'

def profile_commitment(raw):
    return hashlib.sha256(b'SOXX-WEB-APPENDIX-PROFILE-V1\0'+raw).hexdigest()

def _scope(compute_sha, run_id, commitment):
    return {'schema_version':1,'kind':'approved_exact_soxx_web_appendix_store_publication',
            'compute_repo':COMPUTE,'compute_sha':compute_sha,'workflow_ref':WORKFLOW_REF,
            'run_id':run_id,'run_attempt':1,'store_repo':STORE,
            'profile_sha256':commitment,'purpose':'prior_close_only'}

def authorize(env):
    sha, run_id=env.get('GITHUB_SHA',''),env.get('GITHUB_RUN_ID','')
    if (env.get('GITHUB_REPOSITORY')!=COMPUTE or env.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or env.get('GITHUB_REF')!='refs/heads/main' or env.get('GITHUB_RUN_ATTEMPT')!='1'
            or env.get('GITHUB_ACTOR')!='yuatom' or env.get('GITHUB_TRIGGERING_ACTOR')!='yuatom'
            or env.get('GITHUB_WORKFLOW_REF')!=WORKFLOW_REF or env.get('GITHUB_WORKFLOW_SHA')!=sha
            or not SHA.fullmatch(sha) or not re.fullmatch(r'[1-9][0-9]{0,19}',run_id)):
        raise ValueError('UNAUTHORIZED_ACTIONS_CONTEXT')
    raw=env.get(AUTHORITY,'').encode('utf-8')
    profile=env.get(PROFILE,'').encode('utf-8')
    if not 1<=len(profile)<=16000:
        raise ValueError('PRIVATE_PROFILE_UNAVAILABLE')
    if not 1<=len(raw)<=4096:
        raise ValueError('EXACT_RUN_AUTHORITY_UNAVAILABLE')
    try:
        authority=_strict_json(raw)
    except ValueError:
        raise ValueError('EXACT_RUN_AUTHORITY_INVALID') from None
    if (type(authority) is not dict or type(authority.get('schema_version')) is not int
            or type(authority.get('run_attempt')) is not int or authority!=_scope(sha,run_id,profile_commitment(profile))):
        raise ValueError('EXACT_RUN_AUTHORITY_MISMATCH')
    return sha,run_id,load_profile(profile)

def _require_origins(compute, store):
    # This check intentionally supports only GitHub HTTPS checkout of both fixed repos.
    for root, slug in ((compute,COMPUTE),(store,STORE)):
        expect='https://github.com/'+slug
        allowed={expect,expect+'.git'}
        for direction in ((),('--push',)):
            observed=_git(root,'remote','get-url',*direction,'--all','origin').decode('ascii').splitlines()
            if len(observed)!=1 or observed[0] not in allowed:
                raise ValueError('EXACT_REPOSITORY_ORIGIN_REQUIRED')

def _durable_inputs(exact):
    refs={exact.evidence_base,exact.base}
    if isinstance(exact.advance,tuple):
        refs.add(exact.advance[0].publication_base)
    return refs

def _require_local_and_remote(compute,store,sha,exact):
    checks=((compute,('rev-parse','HEAD'),sha),
            (compute,('status','--porcelain','--untracked-files=all'),''),
            (compute,('ls-remote','--exit-code','origin','refs/heads/main'),sha+'\trefs/heads/main'),
            (store,('ls-remote','--exit-code','origin','refs/heads/main'),exact.base+'\trefs/heads/main'))
    for commit in sorted(_durable_inputs(exact)):
        ref='refs/tags/market-data-read/'+commit
        checks+=((store,('ls-remote','--exit-code','origin',ref),commit+'\t'+ref),)
    for root,args,expected in checks:
        if _git(root,*args).decode('ascii').strip()!=expected:
            raise ValueError('EXACT_SOURCE_OR_STORE_BASE_DRIFT')

def _graphql_one_post(token, document):
    # Direct TLS connection: no redirect, proxy credential forwarding, or write retry.
    connection=HTTPSConnection('api.github.com',timeout=20)
    try:
        connection.request('POST','/graphql',json.dumps(document).encode('utf-8'),{
            'Authorization':'Bearer '+token,'Content-Type':'application/json',
            'User-Agent':'SOXX-Exact-Appendix-Writer',
        })
        response=connection.getresponse()
        data=response.read((1<<16)+1)
        if response.status!=200 or len(data)>1<<16:
            raise ValueError('STORE_CAS_WRITE_UNCONFIRMED')
        return _strict_json(data)
    finally:
        connection.close()

def _create_exact(store,token,run_id,exact):
    mid='soxx-web-appendix-'+run_id
    additions=[{'path':path,'contents':base64.b64encode(_git(store,'show',exact.head+':'+path)).decode('ascii')}
               for path in sorted(exact.blobs)]
    document={'query':'mutation ExactAppendix($input: CreateCommitOnBranchInput!) { '
                       'createCommitOnBranch(input: $input) { clientMutationId commit { oid } } }',
              'variables':{'input':{
                  'branch':{'repositoryNameWithOwner':STORE,'branchName':'main'},
                  'expectedHeadOid':exact.base,'clientMutationId':mid,
                  'message':{'headline':'data: append retained SOXX public-web prior-close evidence'},
                  'fileChanges':{'additions':additions},
              }}}
    try:
        response=_graphql_one_post(token,document)
        if type(response) is not dict or 'errors' in response:
            raise ValueError('STORE_CAS_WRITE_UNCONFIRMED')
        record=response['data']['createCommitOnBranch']
        oid=record['commit']['oid']
        if (record['clientMutationId']!=mid or type(oid) is not str or not SHA.fullmatch(oid)
                or oid in (exact.base,exact.head)):
            raise ValueError('STORE_CAS_IDENTITY_UNCONFIRMED')
    except (HTTPException,OSError,ValueError,KeyError,TypeError):
        raise ValueError('STORE_CAS_STOP_INSPECT_NO_RETRY') from None
    return oid

def _verify_created_local(store,oid,exact):
    def g(*args):
        return _git(store,*args).decode('ascii').strip()
    if g('ls-remote','--exit-code','origin','refs/heads/main')!=oid+'\trefs/heads/main':
        raise ValueError('STORE_CAS_REMOTE_HEAD_MISMATCH')
    g('fetch','--no-tags','--no-recurse-submodules','--write-fetch-head','origin',oid)
    if (g('rev-parse','FETCH_HEAD')!=oid or g('rev-list','--parents','-n','1',oid).split()!=[oid,exact.base]
            or g('rev-parse',oid+'^{tree}')!=exact.tree
            or g('rev-parse','HEAD')!=exact.head or g('status','--porcelain','--untracked-files=all')):
        raise ValueError('STORE_CAS_PARENT_OR_TREE_MISMATCH')
    g('checkout','--detach',oid)

def _credential(env):
    token=env.get(TOKEN,'')
    if not token or len(token)>1024 or not re.fullmatch(r'[A-Za-z0-9_]+',token):
        raise ValueError('STORE_CAS_CREDENTIAL_UNAVAILABLE')
    return token

@contextmanager
def _store_git_credentials(token):
    # Environment only: neither token nor transformed token is put in command args or Git config.
    endpoint='https://github.com/'+STORE
    env={'GIT_CONFIG_COUNT':'2','GIT_CONFIG_KEY_1':'http.followRedirects','GIT_CONFIG_VALUE_1':'false','GIT_CONFIG_KEY_0':'http.'+endpoint+'.extraheader',
         'GIT_CONFIG_VALUE_0':'AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode(),
         'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never','GIT_CURL_VERBOSE':'0'}
    env.update({name:'0' for name in os.environ if name.startswith('GIT_TRACE')})
    old={key:os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield endpoint
    finally:
        for key,val in old.items():
            if val is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=val

def _quiet_store_checkout(store,endpoint,exact):
    _git(store,'init','--quiet')
    _git(store,'remote','add','origin',endpoint)
    _git(store,'fetch','--quiet','--no-tags','--no-recurse-submodules','--write-fetch-head','origin',exact.head)
    if _git(store,'rev-parse','FETCH_HEAD').decode('ascii').strip()!=exact.head:
        raise ValueError('STORE_FETCHED_UNEXPECTED_OBJECT')
    _git(store,'checkout','--quiet','--detach',exact.head)

def publish(compute,store,env):
    sha,run_id,exact=authorize(env)
    token=_credential(env)
    with _store_git_credentials(token) as endpoint:
        _quiet_store_checkout(store,endpoint,exact)
        return _publish_checked(compute,store,sha,run_id,token,exact)

def _publish_checked(compute,store,sha,run_id,token,exact):
    _require_origins(compute,store)
    preflight(store,exact)
    _require_local_and_remote(compute,store,sha,exact)
    oid=_create_exact(store,token,run_id,exact)
    _verify_created_local(store,oid,exact)
    result=verify_publication(store,oid,status=STATUS,required_manifest=exact.manifest)
    if (result.get('status')!='durability_verified' or result.get('verified_changed_paths')!=4
            or len(result.get('verified_required_outputs',[]))!=3):
        raise ValueError('EXACT_APPENDIX_DURABILITY_UNVERIFIED')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-authority',action='store_true')
    mode.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    try:
        env=dict(os.environ)
        for key in (TOKEN,AUTHORITY,PROFILE):
            os.environ.pop(key,None)
        if args.check_authority:
            authorize(env)
            return 0
        with tempfile.TemporaryDirectory(prefix='soxx-private-publication-') as store:
            publish(Path(__file__).resolve().parents[1],Path(store),env)
        print('SOXX_APPENDIX_DURABILITY_VERIFIED')
        return 0
    except Exception:  # Never let runner traceback disclose private strings or credentials.
        print('SOXX_APPENDIX_STOP_INSPECT_NO_RETRY',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
