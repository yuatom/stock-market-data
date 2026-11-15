"""Read-only GitHub authority gate. No saved payload or environment-only fallback."""
import json,os,re,urllib.request
COMPUTE='yuatom/stock-market-data'
STORE='yuatom/stock-market-data-store'
OWNER_ID=10399225
STORE_ID=1335101677
CALLER='.github/workflows/market-data-collector.yml'
CALLEE='.github/workflows/dynamic-candidate-runtime.yml'
STANDARD='.github/workflows/market-data-collector-runtime.yml'
class AuthorityError(RuntimeError):pass

def require(value):
    if not value:raise AuthorityError('dynamic runtime authority unavailable or mismatched')

def validate(env,repo,store,run,workflow,*,control_sha,compute_sha):
    """All API objects must be fetched directly by the trusted entrypoint, not supplied by caller."""
    require(repo.get('full_name')==COMPUTE and repo.get('owner',{}).get('id')==OWNER_ID)
    rid=repo.get('id');require(type(rid) is int and rid>0)
    require(store.get('full_name')==STORE and store.get('id')==STORE_ID and store.get('private') is True and store.get('owner',{}).get('id')==OWNER_ID)
    require(env.get('GITHUB_REPOSITORY')==COMPUTE and env.get('GITHUB_REPOSITORY_ID')==str(rid) and env.get('GITHUB_REPOSITORY_OWNER_ID')==str(OWNER_ID))
    require(env.get('GITHUB_EVENT_NAME')=='push' and env.get('GITHUB_REF')=='refs/heads/discovery-requests' and env.get('GITHUB_SHA')==control_sha)
    require(env.get('GITHUB_WORKFLOW_REF')==COMPUTE+'/'+CALLER+'@refs/heads/discovery-requests')
    require(env.get('GITHUB_WORKFLOW_SHA')==control_sha)
    require(re.fullmatch('[0-9a-f]{40}',control_sha) and re.fullmatch('[0-9a-f]{40}',compute_sha))
    require(str(run.get('id'))==env.get('GITHUB_RUN_ID') and run.get('run_attempt')==1 and env.get('GITHUB_RUN_ATTEMPT')=='1')
    require(run.get('actor',{}).get('id')==OWNER_ID and run.get('triggering_actor',{}).get('id')==OWNER_ID)
    require(env.get('GITHUB_ACTOR_ID')==str(OWNER_ID) and run['actor'].get('login')==env.get('GITHUB_ACTOR') and bool(env.get('GITHUB_ACTOR')))
    require(run['triggering_actor'].get('login')==env.get('GITHUB_TRIGGERING_ACTOR') and bool(env.get('GITHUB_TRIGGERING_ACTOR')))
    require(run.get('event')=='push' and run.get('head_sha')==control_sha and run.get('head_branch')=='discovery-requests' and run.get('status')=='in_progress')
    for key in ('repository','head_repository'):
        require(run.get(key,{}).get('id')==rid and run[key].get('full_name')==COMPUTE)
    require(workflow.get('id')==run.get('workflow_id') and workflow.get('path')==CALLER)
    # GitHub also lists the known standard reusable job when that job is skipped.
    # Require exactly one dynamic target; at most one same-pin standard reference.
    refs=run.get('referenced_workflows');require(isinstance(refs,list) and 1<=len(refs)<=2)
    seen=set()
    for ref in refs:
        require(isinstance(ref,dict))
        matches=[name for name in (CALLEE,STANDARD) if ref.get('path') in
                 (COMPUTE+'/'+name+'@main',COMPUTE+'/'+name+'@refs/heads/main')]
        require(len(matches)==1 and matches[0] not in seen)
        require(ref.get('ref')=='refs/heads/main' and ref.get('sha')==compute_sha)
        seen.add(matches[0])
    require(CALLEE in seen)
    return dict(compute_repository_id=rid,store_repository_id=STORE_ID,run_id=run['id'],run_attempt=1,control_sha=control_sha,compute_sha=compute_sha)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise AuthorityError('authority redirect forbidden')

def api(path,token):
    require(isinstance(token,str) and bool(token) and path.startswith('/repos/') and '..' not in path)
    request=urllib.request.Request('https://api.github.com'+path,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
            raw=response.read(2_000_001);require(len(raw)<=2_000_000)
            value=json.loads(raw);require(isinstance(value,dict));return value
    except Exception:raise AuthorityError('authority read failed; no write or provider request') from None

def attest(env,*,control_sha,compute_sha,read=api):
    run_id=env.get('GITHUB_RUN_ID','');require(re.fullmatch('[1-9][0-9]*',run_id))
    token=env.get('GH_TOKEN','');store_token=env.get('MARKET_DATA_STORE_TOKEN','')
    repo=read('/repos/'+COMPUTE,token);store=read('/repos/'+STORE,store_token)
    run=read('/repos/'+COMPUTE+'/actions/runs/'+run_id,token)
    wid=run.get('workflow_id');require(type(wid) is int and wid>0)
    workflow=read('/repos/'+COMPUTE+'/actions/workflows/'+str(wid),token)
    return validate(env,repo,store,run,workflow,control_sha=control_sha,compute_sha=compute_sha)
