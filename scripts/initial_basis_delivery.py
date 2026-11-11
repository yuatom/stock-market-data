"""Authenticated GET-only initial read delivery. No provider or dispatch owner.

Approval/source observations must come from the approved invocation, never a
Frozen draft or this function's output. A missing run is a blocker, not a reason
to create one. Private values are returned in memory, never printed or uploaded.
"""
import base64
import re
import urllib.request
import urllib.error
import urllib.parse
from scripts import initial_basis_read as I
from scripts import provider_basis_snapshot as B

STORE='yuatom/stock-market-data-store'
CONSUMER='yuatom/stock-dairy'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class ReadOnlyGitHubAPI:
    """Fixed host GET transport. No POST method, retries or credential redirects."""
    def __init__(self,token):
        B._need(type(token) is str and re.fullmatch('[A-Za-z0-9_.-]{8,4096}',token))
        self._token=token
        self._opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())

    def get(self,path):
        try:
            B._need(type(path) is str and (path.startswith('/repos/'+STORE+'/') or path=='/repos/'+STORE or path=='/repos/'+CONSUMER+'/contents/state/runtime-release.json?ref=main'))
            B._need('..' not in path and '\\' not in path and '#' not in path)
            url='https://api.github.com'+path
            request=urllib.request.Request(url,method='GET',headers={'Authorization':'Bearer '+self._token,
                'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10','User-Agent':'finite-initial-price-read'})
            with self._opener.open(request,timeout=30) as response:
                B._need(response.status==200 and response.geturl()==url);raw=response.read(1024*1024+1)
            B._need(len(raw)<=1024*1024)
            return B._parse(raw,canonical=False)
        except Exception:raise ValueError('initial read metadata unavailable') from None

    def job_log(self,job_id):
        try:
            B._need(type(job_id) is int and job_id>0)
            target='https://api.github.com/repos/'+STORE+'/actions/jobs/'+str(job_id)+'/logs'
            request=urllib.request.Request(target,method='GET',headers={'Authorization':'Bearer '+self._token,
                'Accept':'application/vnd.github+json','User-Agent':'finite-initial-price-read'})
            try:
                with self._opener.open(request,timeout=30) as response:
                    B._need(response.status==200 and response.geturl()==target);raw=response.read(1024*1024+1)
            except urllib.error.HTTPError as error:
                B._need(error.code==302 and error.geturl()==target)
                location=error.headers.get('Location','');u=urllib.parse.urlsplit(location)
                B._need(u.scheme=='https' and not u.username and not u.password and not u.fragment
                    and re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net',u.hostname or ''))
                # Signed log storage is read without forwarding the GitHub token.
                with self._opener.open(urllib.request.Request(location,method='GET',headers={'User-Agent':'finite-initial-price-read'}),timeout=30) as response:
                    B._need(response.status==200 and response.geturl()==location);raw=response.read(1024*1024+1)
            B._need(len(raw)<=1024*1024)
            return dict(job_id=job_id,text=raw.decode('utf-8'))
        except Exception:raise ValueError('initial read log unavailable') from None


def _release_bytes(content):
    """GitHub Contents wraps base64 lines; only bounded ASCII whitespace varies."""
    B._need(type(content) is str and 0<len(content)<=1024*1024)
    encoded=content.encode('ascii')
    compact=encoded.translate(None,b' \t\r\n')
    raw=base64.b64decode(compact,validate=True)
    B._need(0<len(raw)<=1024*1024)
    return raw


def current_initial_delivery(*,store,read_api,approval,grant_raw,calendar_raw,
        consumer_sha,trade_date,knowledge_cutoff,now,target_read_sha=None):
    """Fresh read-only authority plus validated view, no writes on any outcome.

    The Store adapter is used only for lock/head/materialize_ref/read operations.
    Materialization fetches exact private refs into a local clone, never remotely
    creates or changes them. Every metadata request is GET and named by approval.
    """
    try:
        grant,calendar,dates=I.approved_scope(approval,grant_raw,calendar_raw,consumer_sha=consumer_sha,
            now=now,trade_date=trade_date,knowledge_cutoff=knowledge_cutoff)
        with store.lock():
            current=store.head();target=B._hex(target_read_sha or current)
            repository=read_api.get('/repos/'+STORE)
            workflow=read_api.get('/repos/'+STORE+'/actions/workflows/'+str(grant['workflow_id']))
            B._need(workflow.get('id')==grant['workflow_id'] and workflow.get('path')==I.WORKFLOW and workflow.get('state')=='active')
            release=read_api.get('/repos/'+CONSUMER+'/contents/state/runtime-release.json?ref=main')
            B._need(release.get('type')=='file' and release.get('path')=='state/runtime-release.json' and release.get('encoding')=='base64')
            raw=_release_bytes(release['content'])
            B._need(len(raw)<=1024*1024 and B.blob_sha(raw)==release['sha']
                and B._parse(raw,canonical=False)['repository_commit_sha']==consumer_sha)
            run_id=grant['verification_run_id'];path='/repos/'+STORE+'/actions/runs/'+str(run_id)
            run=read_api.get(path);jobs=read_api.get(path+'/jobs?per_page=2')
            B._need(jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1)
            log=read_api.job_log(jobs['jobs'][0]['id'])
            receipt_ref=f"refs/heads/basis-read-receipts/{grant['compute_sha']}/{grant['receipt_commitment']}"
            B._need(store.materialize_ref(receipt_ref)==grant['receipt_commit'])
            B._need(store.materialize_ref('refs/tags/market-data-read/'+target)==target)
            runtime=I.InitialRuntimeAuthority(approval,grant_raw,calendar_raw,repository,run,jobs,log,
                current,consumer_sha,now,calendar)
            binding=dict(selector=dict(grant['selector'],read_sha=target),compute_sha=grant['compute_sha'],
                receipt_commitment=grant['receipt_commitment'],volume_policy='BLOCKED_UNRESOLVED',
                initial_read_authority=dict(kind='finite_initial_read',approval_sha256=approval.approval_sha256,admitted_read_sha=grant['selector']['read_sha']))
            selector,authority=I.resolve_initial_selector(store.repo,binding,runtime,trade_date=trade_date,knowledge_cutoff=knowledge_cutoff)
            view=B.read_price_view(store.repo,selector,authority,fields=B.PRICE_FIELDS,required_sessions=dates,knowledge_cutoff=knowledge_cutoff)
            B._need(store.head()==current)
            return dict(binding=binding,selector=selector,runtime=runtime,view=view,
                original_selector=grant['selector'],receipt_ref=receipt_ref,receipt_commit=grant['receipt_commit'],
                receipt_blob_sha=grant['receipt_blob_sha'],verified_run_id=run_id,
                valid_until=grant['valid_until'],current_store_sha=current,volume_policy='BLOCKED_UNRESOLVED')
    except Exception:raise ValueError('finite initial read delivery rejected') from None
