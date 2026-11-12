"""Subprocess-only synthetic Compute producer. Never network or genuine data."""
import json,sys,subprocess
from pathlib import Path
from datetime import date,datetime,timedelta,timezone
from dataclasses import asdict
from scripts import provider_basis_snapshot as B
from scripts.raw_response_evidence import build_envelope
from scripts.daily_basis_updates import DailyUpdater,GrantAuthority
from scripts.daily_basis_store import LocalGitStore
from scripts.run_daily_basis_route import dispatch_verify_once
from scripts.emit_basis_consumer_delivery import build_delivery,encode_delivery,WORKFLOW


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid',*args],stderr=subprocess.PIPE).decode().strip()


def seed(destination):
    repo=Path(destination);repo.mkdir();git(repo,'init','-qb','main');git(repo,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
    days=[];d=date(2026,9,10)
    while len(days)<62:
        if d.weekday()<5:days.append(d.isoformat())
        d-=timedelta(days=1)
    days=list(reversed(days));values=[dict(datetime=d,open=str(100+i),high=str(100+i),low=str(100+i),close=str(100+i),volume='300') for i,d in enumerate(days)]
    values[1]['volume']='303'
    request=dict(provider='twelve_data',symbol='SOXX',interval='1day',exchange='NASDAQ',mic_code='XNMS',currency='USD',type='ETF',adjust='splits',dp=-1,start_date=days[0],end_date=days[-3],outputsize=5000,order='asc')
    def response(req,rows):return B.encode(dict(status='ok',meta={k:req[k] for k in ('symbol','interval','currency','exchange','mic_code','type')}|{'exchange_timezone':'America/New_York'},values=rows))
    raw=response(request,values[:-2]);env=build_envelope(raw,request,'2026-09-09T12:00:00Z')
    baseline=B.encode([dict(trade_date=r['datetime'],open=int(r['open'])*3,high=int(r['high'])*3,low=int(r['low'])*3,close=int(r['close'])*3,volume=100) for r in values[:55]])
    split=dict(kind='forward_split',numerator=3,denominator=1,effective_date=days[55],source_url='https://example.invalid/synthetic-split')
    snapshot=B.prepare_snapshot(env,B.sha(env),baseline,split)
    terminal=B.encode(dict(status='CAPTURE_RETAINED_UNQUALIFIED',request_id='100-1',compute_sha='6'*40,envelope_sha256=B.sha(env),raw_response_sha256=B.sha(raw)))
    ep='capture-evidence/genesis/envelope.json';tp='capture-evidence/genesis/terminal.json';bp='capture-evidence/genesis/baseline.json'
    admission=dict(schema_version=1,kind='provider_snapshot_price_admission',status='ADMITTED_PRICE_ONLY',basis_id=snapshot['basis_id'],allowed_fields=list(B.PRICE_FIELDS),volume_policy='BLOCKED_UNRESOLVED',admitted_at='2026-09-09T12:00:01Z',proof_model='reviewed_workflow_receipt_not_provider_signature',envelope_ref=dict(path=ep,blob_sha=B.blob_sha(env)),workflow_evidence=dict(compute_sha='6'*40,request_id='100-1',terminal_ref=dict(path=tp,blob_sha=B.blob_sha(terminal))),unresolved_volume_dates=snapshot['unresolved_volume_dates'])
    ar=B.encode(admission);sr=B.encode(snapshot)
    catalog=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id=snapshot['basis_id'],snapshot_blob_sha=B.blob_sha(sr),admission_blob_sha=B.blob_sha(ar))
    files={B.catalog_path(B.PROVIDER,'SOXX'):B.encode(catalog),B.snapshot_path(B.PROVIDER,'SOXX',snapshot['basis_id']):sr,B.admission_path(B.PROVIDER,'SOXX',B.blob_sha(ar)):ar,ep:env,tp:terminal,bp:baseline}
    for path,data in files.items():
        p=repo/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    git(repo,'add','.');git(repo,'commit','-qm','synthetic genesis');head=git(repo,'rev-parse','HEAD')
    cal=dict(schema_version=1,kind='reviewed_finite_daily_calendar',source_url='https://www.nyse.com/trade/hours-calendars',known_at='2026-09-09T13:00:00Z',sessions=[dict(trade_date=d,scheduled_close='16:00') for d in days])
    slot=dict(slot_id='sep10',execution_day='2026-09-10',cutoff='2026-09-09',valid_from='2026-09-10T12:00:00Z',valid_until='2026-09-10T20:00:00Z',cleanup_until='2026-09-10T20:30:00Z',receipt_until='2026-09-11T00:00:00Z')
    grant=dict(schema_version=1,kind='finite_daily_basis_grant',repository='yuatom/stock-market-data-store',repository_id=11,owner_id=12,compute_sha='6'*40,consumer_sha='b'*40,workflow_source_sha='c'*40,workflow_id=14,calendar_sha256=B.sha(B.encode(cal)),initial_selector=catalog|{'read_sha':head},baseline_ref=dict(commit=head,path=bp,blob_sha=B.blob_sha(baseline),sha256=B.sha(baseline)),request_template=request,slots=[slot,dict(slot,slot_id='sep11',execution_day='2026-09-11',cutoff='2026-09-10',valid_from='2026-09-11T12:00:00Z',valid_until='2026-09-11T20:00:00Z',cleanup_until='2026-09-11T20:30:00Z',receipt_until='2026-09-12T00:00:00Z')],request_limit=2,credit_limit=2,verification_limit=2,hard_credits_per_day=200,hard_credits_per_minute=8,maturity_next_day_et='07:00',private_destination='capture-evidence/daily-basis',actions_free_only=True,allowed_actions=['capture','private_verify','immutable_catalog_update'],volume_policy='BLOCKED_UNRESOLVED')
    return repo,grant,cal,days,values,response

# HTTP responses below are synthetic. All protocol writers, readers, emitters,
# CAS logic and ordinary-clone boundaries are production functions.
import base64,copy,re
sys.path.insert(0,str(Path(__file__).resolve().parent))
from scripts import run_daily_basis_route as H
from scripts.daily_basis_remote import RemoteGitStore,STORE_URL
from test_daily_basis_remote import BareTransport,TOKEN

class HostedChain:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        repo,g,cal,days,values,response=seed(self.root/'seed')
        self.grant=g;self.calendar=cal;self.days=days;self.values=values;self.response=response
        self.now=datetime(2026,9,10,12,0,5,tzinfo=timezone.utc)
        self.calls=[];self.posts=[];self.emissions={};self.runs={};self.jobs={};self.clones=[]
        self.repository=dict(id=11,private=True,full_name=H.STORE,owner={'id':12})
        # The private source-ref is a real exact commit, not a synthesized SHA
        # returned only by a fixture override of remote_ref.
        source=git(repo,'rev-parse','HEAD');g['workflow_source_sha']=source
        git(repo,'update-ref','refs/heads/basis-consumer-delivery/'+source,source)
        local=LocalGitStore(repo)
        gr=B.encode(g);cr=B.encode(cal)
        with local.lock():
            scope=local.publish(local.head(),{'basis-authority/grant.json':gr,'basis-authority/calendar.json':cr},replace=set())
            a=dict(schema_version=1,kind='finite_daily_basis_authority',compute_sha=g['compute_sha'],consumer_sha=g['consumer_sha'],workflow_source_sha=source,grant_ref=dict(commit=scope,path='basis-authority/grant.json',blob_sha=B.blob_sha(gr),sha256=B.sha(gr)),calendar_ref=dict(commit=scope,path='basis-authority/calendar.json',blob_sha=B.blob_sha(cr),sha256=B.sha(cr)),nonce='e'*64,valid_from='2026-09-10T12:00:00Z',valid_until='2026-09-12T00:00:00Z',actions_free_until='2026-09-12T00:00:00Z',readiness_run_id=444,repository_id=11,owner_id=12,runtime_workflow_id=33,trigger_kind='schedule',schedule='5 7 * * 1-5',old_writers_drained=True)
            ar=B.encode(a);self.commitment=H.authority_commitment(ar);ac=local.commit(scope,{H.AUTH_PATH:ar},replace=set())
            local.cas('refs/heads/basis-daily-authority/'+g['compute_sha']+'/'+self.commitment,ac,'0'*40)
        self.authority=a;self.remote=self.root/'remote.git'
        git(self.root,'clone','--bare','--no-hardlinks',str(repo),str(self.remote))
        self.public_run=self.public(101)

    def public(self,run_id):
        return dict(id=run_id,workflow_id=33,path=H.CALLER,head_sha=self.grant['compute_sha'],event='schedule',run_attempt=1,head_branch='main',status='in_progress',repository={'full_name':H.COMPUTE},head_repository={'full_name':H.COMPUTE},actor={'id':12},triggering_actor={'id':12})

    def fresh(self,name,*,adapter=True):
        repo=self.root/name;git(self.root,'clone','--no-hardlinks',str(self.remote),str(repo))
        git(repo,'remote','set-url','origin',STORE_URL);self.clones.append(name)
        if not adapter:return repo
        transport=BareTransport(repo,self.remote)
        store=RemoteGitStore(repo,TOKEN,source_recheck=lambda:None,platform_guard=lambda:None,expected_repository_id=11,expected_owner_id=12,transport=transport)
        return store,transport

    def dispatch_capability(self,workflow_id):return workflow_id==14

    def request(self,method,path,payload=None):
        if method=='POST':
            assert path=='/repos/'+H.STORE+'/actions/workflows/14/dispatches'
            assert payload['ref']=='basis-consumer-delivery/'+self.grant['workflow_source_sha']
            assert set(payload['inputs'])=={'compute_sha','receipt_commitment'}
            assert payload['inputs']['compute_sha']==self.grant['compute_sha']
            run_id=555+len(self.posts)*10;self.posts.append((path,copy.deepcopy(payload)))
            source=self.grant['workflow_source_sha'];stamp=self.now.isoformat()
            self.runs[run_id]=dict(id=run_id,workflow_id=14,path=H.VERIFIER,head_sha=source,event='workflow_dispatch',run_attempt=1,head_branch='basis-consumer-delivery/'+source,status='completed',conclusion='success',repository={'id':11},head_repository={'id':11},actor={'id':12},triggering_actor={'id':12},run_started_at=stamp)
            self.jobs[run_id]=dict(total_count=1,jobs=[dict(id=run_id+1,run_id=run_id,name='verify-basis-consumer',head_sha=source,status='completed',conclusion='success',completed_at=stamp,steps=[dict(name='Verify and emit private delivery',status='completed',conclusion='success')])])
            # Simulate the private Actions checkout boundary with an ordinary
            # fresh clone, then invoke the actual read-only private emitter.
            verifier=self.fresh('verifier-'+str(run_id),adapter=False)
            context=dict(repository=H.STORE,repository_id=11,owner_id=12,private=True,workflow_source_sha=source,run_id=run_id,run_attempt=1,event='workflow_dispatch',actor_id=12,actor='yuatom',triggering_actor='yuatom',workflow_path=WORKFLOW,job='verify-basis-consumer')
            emitted=build_delivery(verifier,payload['inputs']['receipt_commitment'],self.grant['compute_sha'],context,now=stamp)
            self.emissions[run_id]=dict(job_id=run_id+1,text=encode_delivery(emitted))
            return dict(workflow_run_id=run_id,run_url='https://api.github.com/repos/'+H.STORE+'/actions/runs/'+str(run_id),html_url='https://github.com/'+H.STORE+'/actions/runs/'+str(run_id))
        assert method=='GET'
        if path=='/user':return {'id':12}
        if path=='/repos/'+H.STORE:return copy.deepcopy(self.repository)
        if path=='/repos/'+H.COMPUTE+'/actions/runs/'+str(self.public_run['id']):return copy.deepcopy(self.public_run)
        if path=='/repos/'+H.STORE+'/actions/workflows/14':return dict(id=14,path=H.VERIFIER,state='active')
        if path=='/repos/'+H.STORE+'/actions/runs/444':
            return dict(workflow_id=14,head_sha=self.grant['workflow_source_sha'],path=H.VERIFIER,status='completed',conclusion='success',run_attempt=1,event='workflow_dispatch',actor={'id':12},triggering_actor={'id':12})
        if '/contents/state/runtime-release.json' in path:
            raw=B.encode({'repository_commit_sha':self.grant['consumer_sha']});return dict(type='file',path='state/runtime-release.json',encoding='base64',content=base64.b64encode(raw).decode(),sha=B.blob_sha(raw))
        m=re.fullmatch('/repos/'+re.escape(H.STORE)+r'/actions/runs/([0-9]+)(/jobs\?per_page=2)?',path)
        if m:return copy.deepcopy(self.jobs[int(m[1])] if m[2] else self.runs[int(m[1])])
        raise AssertionError(path)

    def job_log(self,job_id):return copy.deepcopy(self.emissions[job_id-1])

    def capture(self,*,request,before_request,retain_raw,retain_envelope,clock):
        before_request();self.calls.append(copy.deepcopy(request))
        raw=self.response(request,[v for v in self.values if v['datetime']<=request['end_date']]);retain_raw(raw)
        e=build_envelope(raw,request,clock().isoformat());retain_envelope(e)
        return dict(status='CAPTURE_RETAINED_UNQUALIFIED',envelope_sha256=B.sha(e),raw_response_sha256=B.sha(raw))

    def run(self,index,*,before=None):
        slot=self.grant['slots'][index];self.now=B._stamp(slot['valid_from'])+timedelta(seconds=5)
        self.public_run=self.public(101+index)
        store,tr=self.fresh('collector-'+slot['slot_id'])
        if before:before(store,tr)
        env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=H.COMPUTE,GITHUB_SHA=self.grant['compute_sha'],GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='schedule',GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='yuatom',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_JOB='collect',DAILY_BASIS_TRIGGER_KIND='schedule',DAILY_BASIS_SCHEDULE='5 7 * * 1-5',GITHUB_WORKFLOW_REF=H.COMPUTE+'/'+H.CALLER+'@refs/heads/main',GITHUB_RUN_ID=str(101+index))
        result=H.run_route(store=store,api=self,compute=self.root,compute_sha=self.grant['compute_sha'],commitment=self.commitment,trade_date=slot['execution_day'],env=env,key='synthetic-only',access={},delegation_output=self.root/('delegation-'+slot['slot_id']+'.json'),clock=lambda:self.now,capture=self.capture,source_check=lambda:None)
        assert result=='FINITE_DAILY_BASIS_COMPLETE'
        return store,tr

    def read(self,*,later=True):
        if later:
            writer,_=self.fresh('other-symbol-writer')
            self.other='data/market-data/intraday/OTHER/synthetic.json';self.other_raw=B.encode(dict(symbol='OTHER',trade_date='2026-09-11',close=99))
            with writer.lock():writer.publish(writer.head(),{self.other:self.other_raw},replace=set())
        self.now+=timedelta(hours=1)
        store,tr=self.fresh('standalone-reader')
        delivery=H.current_delivery(store=store,api=self,compute_sha=self.grant['compute_sha'],commitment=self.commitment,clock=lambda:self.now)
        slot=self.grant['slots'][-1];prefix='capture-evidence/daily-basis/'+B.sha(B.encode(self.grant))+'/'+slot['slot_id']
        ref='refs/heads/basis-daily-deliveries/'+B.sha(B.encode(self.grant))+'/'+slot['slot_id']
        mapping=B._parse(store.read(store.ref(ref),'basis-authority/daily-delivery.json'))
        proof_path=prefix+'/price-proof.json';proof=B._parse(store.read(delivery['current_store_sha'],proof_path));run_id=mapping['run_id']
        exported={k:v for k,v in delivery.items() if k not in ('grant_raw','calendar_raw')}
        return dict(grant=self.grant,calendar=self.calendar,candidate=mapping['candidate'],repository=self.repository,run=self.runs[run_id],jobs=self.jobs[run_id],log=self.emissions[run_id],mapping=mapping,dispatch_count=len(self.posts),capture_count=len(self.calls),days=self.days,current_store_sha=delivery['current_store_sha'],proof_path=proof_path,proof=proof,proof_blob=B.blob_sha(B.encode(proof)),other_ref=dict(path=self.other,blob_sha=B.blob_sha(self.other_raw)),view=delivery['view'],current_delivery=exported,clone_names=self.clones,store_path=str(store.repo))


def main(destination):
    chain=HostedChain(destination);chain.run(0);chain.run(1);result=chain.read();print(json.dumps(result))

if __name__=='__main__':main(sys.argv[1])
