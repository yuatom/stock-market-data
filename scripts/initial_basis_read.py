"""Pure finite initial-price read authority. No acquisition, dispatch or mutation.

Identical in Compute and the consumer. Independent approval and authenticated
platform responses are trusted invocation inputs, never values adopted from a
Frozen draft or an untrusted delivery. The original one-shot contract is intact.
"""
from dataclasses import dataclass, field
from datetime import timedelta, datetime, time
from zoneinfo import ZoneInfo
import base64
import re
_CONSUMER = __package__ in ('scripts.basis_reader','basis_reader')
if _CONSUMER:
    from . import provider_basis_snapshot as B
else:
    from scripts import provider_basis_snapshot as B

MARKER='BASIS_PRIVATE_DELIVERY_V1 '
WORKFLOW='.github/workflows/basis-consumer-delivery.yml'


@dataclass(frozen=True,repr=False)
class DeliveryExpectation:
    repository_id: int
    owner_id: int
    workflow_id: int
    workflow_source_sha: str
    compute_sha: str
    receipt_commitment: str
    run_id: int


def _verify_delivery(expected, repository, run, jobs, log, *, now, grant):
    try:
        B._need(type(expected) is DeliveryExpectation and type(grant) is dict)
        for k in ('repository_id','owner_id','workflow_id','run_id'):
            B._need(type(getattr(expected,k)) is int and getattr(expected,k)>0)
        B._hex(expected.workflow_source_sha);B._hex(expected.compute_sha);B._hex(expected.receipt_commitment,64)
        B._need(repository['id']==expected.repository_id and repository['private'] is True
            and repository['full_name']=='yuatom/stock-market-data-store' and repository['owner']['id']==expected.owner_id)
        required=dict(id=expected.run_id,workflow_id=expected.workflow_id,head_sha=expected.workflow_source_sha,
            path=WORKFLOW,event='workflow_dispatch',run_attempt=1,status='completed',conclusion='success',head_branch='basis-consumer-delivery/'+expected.workflow_source_sha)
        B._need(all(run.get(k)==v for k,v in required.items()))
        B._need(run['repository']['id']==expected.repository_id and run['head_repository']['id']==expected.repository_id)
        B._need(run['actor']['id']==run['triggering_actor']['id']==expected.owner_id)
        B._need(type(jobs) is dict and jobs.get('total_count')==1 and len(jobs.get('jobs',[]))==1)
        job=jobs['jobs'][0]
        B._need(job['run_id']==expected.run_id and job['name']=='verify-basis-consumer'
            and job['head_sha']==expected.workflow_source_sha and job['status']=='completed' and job['conclusion']=='success')
        B._need(type(job['id']) is int and job['id']>0)
        steps=[s for s in job['steps'] if s.get('name')=='Verify and emit private delivery']
        B._need(len(steps)==1 and steps[0]['status']=='completed' and steps[0]['conclusion']=='success')
        # A decoded job log response must be fetched using this exact job id.
        B._need(type(log) is dict and set(log)=={'job_id','text'} and log['job_id']==job['id'])
        raw=log['text'];B._need(type(raw) is str and 0<len(raw)<=1024*1024)
        records=[]
        for line in raw.splitlines():
            if MARKER in line:
                match=re.fullmatch(r'(?:\d{4}-\d\d-\d\dT[0-9:.]+Z )?'+re.escape(MARKER)+r'([A-Za-z0-9+/=]+)',line)
                B._need(match);records.append(match.group(1))
        B._need(len(records)==1)
        decoded=base64.b64decode(records[0],validate=True);B._need(len(decoded)<=16384)
        value=B._parse(decoded)
        B._closed(value,('schema_version','kind','compute_sha','receipt_commitment','context','receipt_ref','receipt_commit','receipt_blob_sha','selector','verified_at','valid_until'))
        B._need(value['schema_version']==1 and value['kind']=='private_basis_consumer_delivery'
            and value['compute_sha']==expected.compute_sha and value['receipt_commitment']==expected.receipt_commitment)
        c=value['context'];B._closed(c,('repository','repository_id','owner_id','private','workflow_source_sha','run_id','run_attempt','event','actor_id','actor','triggering_actor','workflow_path','job'))
        B._need(c==dict(repository='yuatom/stock-market-data-store',repository_id=expected.repository_id,
            owner_id=expected.owner_id,private=True,workflow_source_sha=expected.workflow_source_sha,
            run_id=expected.run_id,run_attempt=1,event='workflow_dispatch',actor_id=expected.owner_id,
            actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer'))
        B._need(value['receipt_ref']==f'refs/heads/basis-read-receipts/{expected.compute_sha}/{expected.receipt_commitment}')
        B._hex(value['receipt_commit']);B._hex(value['receipt_blob_sha']);B._selection(value['selector'])
        instant=B._stamp(now);verified=B._stamp(value['verified_at'])
        B._need(B._stamp(run['run_started_at'])<=verified<=B._stamp(job['completed_at'])<=instant
            and verified<=instant<B._stamp(value['valid_until'])
            and B._stamp(grant['valid_from'])<=verified<B._stamp(grant['valid_until'])
            and B._stamp(grant['valid_until'])<=B._stamp(value['valid_until']))
        return value
    except Exception:
        raise ValueError('private basis delivery rejected') from None


@dataclass(frozen=True,repr=False)
class InitialReadApproval:
    approval_sha256: str
    compute_sha: str
    consumer_sha: str
    workflow_source_sha: str
    calendar_sha256: str


@dataclass(frozen=True,repr=False)
class InitialRuntimeAuthority:
    """Authenticated invocation observations, not a self-authorizing document."""
    approval: InitialReadApproval
    grant_raw: bytes = field(repr=False)
    calendar_raw: bytes = field(repr=False)
    repository: dict = field(repr=False)
    run: dict = field(repr=False)
    jobs: dict = field(repr=False)
    log: dict = field(repr=False)
    current_store_sha: str
    consumer_sha: str
    now: str
    calendar_by_trade_date: dict = field(repr=False)

    @property
    def compute_sha(self):return self.approval.compute_sha


GRANT_KEYS=('schema_version','kind','repository','repository_id','owner_id',
    'compute_sha','consumer_sha','workflow_source_sha','workflow_id','selector',
    'proof_ref','receipt_commitment','receipt_commit','receipt_blob_sha',
    'verification_run_id','calendar_sha256','trade_date','required_last_trade_date',
    'valid_from','valid_until','allowed_actions','allowed_metric_scopes',
    'provider_request_limit','verification_limit','volume_policy')


def initial_declaration(value):
    B._closed(value,('kind','approval_sha256','admitted_read_sha'))
    B._need(value['kind']=='finite_initial_read')
    B._hex(value['approval_sha256'],64);B._hex(value['admitted_read_sha'])
    return value


def approved_scope(approval,grant_raw,calendar_raw,*,consumer_sha,now,trade_date,knowledge_cutoff):
    """Validate independent consent before any read transport is selected."""
    B._need(type(approval) is InitialReadApproval)
    a=approval;grant=B._parse(grant_raw);calendar=B._parse(calendar_raw)
    B._need(B.sha(grant_raw)==B._hex(a.approval_sha256,64))
    B._closed(grant,GRANT_KEYS)
    B._need(type(grant['schema_version']) is int and grant['schema_version']==1
        and grant['kind']=='finite_initial_price_read')
    for key in ('compute_sha','consumer_sha','workflow_source_sha'):
        B._need(grant[key]==B._hex(getattr(a,key)))
    B._need(consumer_sha==a.consumer_sha)
    for key in ('repository_id','owner_id','workflow_id','verification_run_id'):
        B._need(type(grant[key]) is int and grant[key]>0)
    B._need(grant['repository']=='yuatom/stock-market-data-store'
        and grant['allowed_actions']==['read_price_only']
        and grant['allowed_metric_scopes']==['prior_session_core_context']
        and grant['volume_policy']=='BLOCKED_UNRESOLVED')
    B._need(type(grant['provider_request_limit']) is int and grant['provider_request_limit']==0
        and type(grant['verification_limit']) is int and grant['verification_limit']==1)
    B._selection(grant['selector']);B._need(grant['selector']['symbol']=='SOXX')
    for key in ('receipt_commit','receipt_blob_sha'):B._hex(grant[key])
    B._hex(grant['receipt_commitment'],64)
    proof=grant['proof_ref'];B._closed(proof,('path','blob_sha'));B._hex(proof['blob_sha'])
    B._need(type(proof['path']) is str and re.fullmatch(r'data/market-data/proofs/price-basis/[a-zA-Z0-9_-]+\.json',proof['path']))
    start,end,instant,cutoff=(B._stamp(x) for x in (grant['valid_from'],grant['valid_until'],now,knowledge_cutoff))
    B._need(start<=instant<end and end-start<=timedelta(hours=24) and start<end and cutoff<=instant)
    B._need(B._day(trade_date)==grant['trade_date'] and B._day(grant['required_last_trade_date'])<trade_date)
    eastern=ZoneInfo('America/New_York')
    next_midnight=datetime.combine(datetime.fromisoformat(trade_date).date()+timedelta(days=1),time(),eastern)
    B._need(instant.astimezone(eastern).date().isoformat()==trade_date and end<=next_midnight)
    B._need(B.sha(calendar_raw)==B._hex(a.calendar_sha256,64)==grant['calendar_sha256'])
    B._closed(calendar,(trade_date,));row=calendar[trade_date]
    B._closed(row,('previous_trade_date','previous_sessions','session_type','scheduled_close','source_url','known_at'))
    B._need(row['previous_trade_date']==grant['required_last_trade_date']
        and row['source_url']=='https://www.nyse.com/trade/hours-calendars'
        and B._stamp(row['known_at'])<=cutoff)
    B._need((row['session_type'],row['scheduled_close']) in (('normal','16:00'),('early_close','13:00')))
    dates=row['previous_sessions'];B._need(type(dates) is list and 50<=len(dates)<=10000)
    B._need(dates==sorted(set(B._day(d) for d in dates)) and dates[-1]==row['previous_trade_date'])
    return grant,calendar,dates


def _ref(g,name):
    raw=g.read('for-each-ref','--format=%(refname) %(objectname) %(objecttype) %(symref)',name)
    match=re.fullmatch(re.escape(name.encode())+rb' ([0-9a-f]{40}) commit \n',raw)
    B._need(match);oid=match.group(1).decode();g.commit(oid);return oid


def _proof(repo,selector,authority,view,*,dates,cutoff,compute_sha):
    # Both implementations use the already reviewed v2 arithmetic. No fetch,
    # publication, budget or provider module is imported on either branch.
    if not _CONSUMER:
        from scripts.build_price_basis_metric_proof import build_price_basis_proof
        return build_price_basis_proof(repo,selector,authority,required_sessions=dates,
            knowledge_cutoff=cutoff,compute_sha=compute_sha,scope='prior_session_core_context')
    from scripts.price_basis_consumer import proof_payload
    return dict(proof_payload(view,compute_sha=compute_sha,scope='prior_session_core_context'),
        selector={k:v for k,v in selector.items() if k!='read_sha'})


def resolve_initial_selector(repo,binding,runtime,*,trade_date,knowledge_cutoff):
    """Read only, recheck every invocation, and fail closed without fallback."""
    try:
        B._need(type(runtime) is InitialRuntimeAuthority)
        B._closed(binding,('selector','compute_sha','receipt_commitment','volume_policy','initial_read_authority'))
        d=initial_declaration(binding['initial_read_authority']);a=runtime.approval
        grant,calendar,dates=approved_scope(a,runtime.grant_raw,runtime.calendar_raw,
            consumer_sha=runtime.consumer_sha,now=runtime.now,trade_date=trade_date,knowledge_cutoff=knowledge_cutoff)
        B._need(runtime.calendar_by_trade_date==calendar and d['approval_sha256']==a.approval_sha256)
        selector=grant['selector'];admitted=selector['read_sha'];target=binding['selector']['read_sha']
        B._need(d['admitted_read_sha']==admitted and binding['compute_sha']==a.compute_sha
            and binding['receipt_commitment']==grant['receipt_commitment'] and binding['volume_policy']=='BLOCKED_UNRESOLVED')
        g=B._store(repo);current=B._hex(runtime.current_store_sha);B._hex(target)
        B._need(_ref(g,'refs/heads/main')==current and _ref(g,'refs/tags/market-data-read/'+target)==target)
        expected=DeliveryExpectation(grant['repository_id'],grant['owner_id'],grant['workflow_id'],
            a.workflow_source_sha,a.compute_sha,grant['receipt_commitment'],grant['verification_run_id'])
        delivery=_verify_delivery(expected,runtime.repository,runtime.run,runtime.jobs,runtime.log,now=runtime.now,grant=grant)
        B._need(delivery['selector']==selector and delivery['receipt_commit']==grant['receipt_commit']
            and delivery['receipt_blob_sha']==grant['receipt_blob_sha'])
        B._need(_ref(g,delivery['receipt_ref'])==grant['receipt_commit'])
        raw=B._blob(g,grant['receipt_commit'],'basis-authority/receipt.json',grant['receipt_blob_sha'])
        g.single_addition(grant['receipt_commit'],admitted,'basis-authority/receipt.json',grant['receipt_blob_sha'])
        selected,_=B.verify_consumer_receipt(raw,grant['receipt_commitment'],a.compute_sha,now=runtime.now)
        receipt=B._receipt(raw)
        B._need(selected==selector and receipt['valid_until']==delivery['valid_until']
            and B._stamp(receipt['valid_from'])<=B._stamp(grant['valid_from'])
            and B._stamp(grant['valid_until'])<=B._stamp(receipt['valid_until']))
        B._need(g.text('merge-base',admitted,target)==admitted and g.text('merge-base',target,current)==target)
        catalog={k:v for k,v in selector.items() if k!='read_sha'}
        paths=[B.catalog_path(B.PROVIDER,'SOXX'),B.snapshot_path(B.PROVIDER,'SOXX',selector['basis_id']),
            B.admission_path(B.PROVIDER,'SOXX',selector['admission_blob_sha']),grant['proof_ref']['path']]
        admission=B._parse(B._blob(g,admitted,paths[2],selector['admission_blob_sha']))
        paths += [admission['envelope_ref']['path'],admission['workflow_evidence']['terminal_ref']['path']]
        terminal=B._parse(B._blob(g,admitted,paths[-1],admission['workflow_evidence']['terminal_ref']['blob_sha']))
        retained=terminal.get('receipts');B._need(type(retained) is list and 1<=len(retained)<=16)
        raw_refs=[];retained_paths=set()
        for item in retained:
            B._closed(item,('path','blob_sha','sha256','store_commit_sha','durability_ref','market_qualified','production_activation'))
            B._need(type(item['path']) is str and item['path'].startswith('capture-evidence/')
                and item['market_qualified'] is False and item['production_activation'] is False)
            B._need(item['path'] not in retained_paths);retained_paths.add(item['path'])
            B._hex(item['store_commit_sha']);B._hex(item['blob_sha']);B._hex(item['sha256'],64)
            B._need(item['durability_ref']=='refs/tags/market-data-read/'+item['store_commit_sha'])
            raw=B._blob(g,admitted,item['path'],item['blob_sha'])
            B._need(B.sha(raw)==item['sha256'])
            if item['path'].endswith('/response.raw'):raw_refs.append(item)
            paths.append(item['path'])
        B._need(len(raw_refs)==1 and raw_refs[0]['sha256']==terminal['raw_response_sha256'])
        paths=list(dict.fromkeys(paths))

        # Initial adoption is not a daily acquisition admission. Its evidence
        # references must not invent a slot or borrow a daily claim.
        B._need(not any(p.startswith('capture-evidence/daily-basis/') for p in paths))
        commits=g.text('rev-list','--max-count=513',admitted+'..'+current).splitlines();B._need(len(commits)<=512)
        for commit in commits:
            for parent in g.commit(commit)[1]:
                B._need(not g.read('diff-tree','--no-commit-id','--no-ext-diff','--no-textconv','--no-renames','--name-only','-r',parent,commit,'--',*paths))
        for path in paths:
            oid=g.normal_blob_identity(admitted,path)
            B._need(g.normal_blob_identity(target,path)==oid==g.normal_blob_identity(current,path))
        B._need(B._parse(B._blob(g,current,paths[0]))==catalog)
        snapshot=B._parse(B._blob(g,target,paths[1],selector['snapshot_blob_sha']))
        B._need(snapshot['last_trade_date']==grant['required_last_trade_date'] and snapshot['raw_response_sha256']==raw_refs[0]['sha256'])
        lifted=dict(selector,read_sha=target);B._need(lifted==binding['selector'])
        authority=B.ReadAuthority(B.sha(B.encode(lifted)),lifted['admission_blob_sha'])
        view=B.read_price_view(repo,lifted,authority,fields=B.PRICE_FIELDS,required_sessions=dates,knowledge_cutoff=knowledge_cutoff)
        proof=B._parse(B._blob(g,target,grant['proof_ref']['path'],grant['proof_ref']['blob_sha']))
        B._need(proof==_proof(repo,lifted,authority,view,dates=dates,cutoff=knowledge_cutoff,compute_sha=a.compute_sha))
        B._need(_ref(g,'refs/heads/main')==current)
        return lifted,authority
    except Exception:
        raise ValueError('finite initial price read authority rejected') from None
