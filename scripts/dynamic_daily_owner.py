"""Explicitly injected dynamic daily writer. No automatic workflow/CLI activation."""
import hashlib,json,re,subprocess,tempfile,time
from pathlib import Path
import admit_market_data_baseline as owner
import verify_store_publication as durable
import dynamic_baseline_targets as request_owner

MODE='dynamic_daily_attempt'
class DynamicPublicationUnknown(RuntimeError):
    """Must escape collection without any fallback publication or retry."""

def encode(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
def blob(raw):return hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()

def load_request_source(control,commit,expected_blob,compute_sha):
    """Existing request schema + exact blob/first-introduction, no prior result needed."""
    from collect_dynamic_candidates import _load_request
    if not re.fullmatch('[0-9a-f]{40}',commit) or not re.fullmatch('[0-9a-f]{40}',expected_blob):raise ValueError('invalid request identity')
    request_owner._git(control,'merge-base','--is-ancestor',commit,'refs/remotes/origin/discovery-requests')
    raw=subprocess.check_output(['git','-C',str(control),'cat-file','blob',commit+':'+request_owner.REQUEST_PATH],stderr=subprocess.DEVNULL)
    if blob(raw)!=expected_blob or request_owner._git(control,'rev-parse',commit+':'+request_owner.REQUEST_PATH)!=expected_blob:raise ValueError('request blob mismatch')
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'request.json';path.write_bytes(raw);request=_load_request(path,expected_contract_sha=compute_sha)
    identity=request['request_id']
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',identity):raise ValueError('request identity invalid')
    introductions=request_owner._git(control,'log','--first-parent','--format=%H','-S',identity,'refs/remotes/origin/discovery-requests','--',request_owner.REQUEST_PATH).splitlines()
    if not introductions or introductions[-1]!=commit:raise ValueError('request not first introduction')
    return request,raw

class DynamicDailyOwner(owner.StorePublisher):
    def __init__(self,repo,expected,day,run_id,*,compute,compute_sha,control,request_commit,request_blob,clock,deadline):
        self.request,self.request_raw=load_request_source(control,request_commit,request_blob,compute_sha)
        if self.request['trade_date']!=day:raise ValueError('request/accounting day mismatch')
        super().__init__(repo,expected,day,run_id,verify=self._verify,report_namespace='dynamic-daily-attempt')
        self.compute=compute;self.compute_sha=compute_sha;self.clock=clock;self.deadline=deadline
        self.request_commit=request_commit;self.request_blob=request_blob;self.request_sha=hashlib.sha256(self.request_raw).hexdigest()
        self.intraday_scope=None
        self.allowed={self.budget_relative};self.active=None;self.reservation='NOT_STARTED';self.failed=False;self.predebit_publish_started=False
    @property
    def mode(self):return 'dynamic_intraday_attempt' if self.intraday_scope else MODE
    def begin_intraday(self,trade_date,stage,start,end):
        windows={'open_15m':('09:30','09:45'),'open_30m':('09:30','10:00'),'open_60m':('10:00','10:30'),'close':('15:45','16:00')}
        if self.active or trade_date!=self.request['trade_date'] or stage!='discovery_'+self.request['stage'] or (start,end)!=windows[self.request['stage']]:self.abort()
        self.assert_current()
        self.intraday_outcomes=[]
        self.intraday_scope=dict(provider='twelve_data_basic',stage=stage,trade_date=trade_date,start=start,end=end,interval='1min')
    def raw_prefix(self,symbol):
        kind='dynamic-intraday' if self.intraday_scope else 'dynamic-daily'
        return f'collector-state/{self.day[:7]}/{kind}-{symbol}-'
    def close_intraday(self,selected,status):
        try:
            if not self.intraday_scope or not self.active or status not in ('SELECTED_TWELVE','UNSELECTED_VALID','REJECTED'):raise ValueError('missing or invalid intraday attempt')
            if status=='SELECTED_TWELVE' and not selected:raise ValueError('selected fallback missing capture')
            paths=[]
            if self.active.get('raw'):paths.append(self.active['raw'].removeprefix('data/market-data/'))
            if selected:
                self.active['selected_capture']=selected;paths.append(selected)
            symbol=self.active['symbol'];reservation=self.reservation
            result=self.finish(paths,status)
            self.intraday_outcomes.append(dict(symbol=symbol,status=status,reservation=reservation,provider_charge_status='NOT_VERIFIED',selected_capture=selected))
            return result
        except Exception:self.abort()
    def assert_current(self,expected=None):
        try:return super().assert_current(expected)
        except Exception:self.abort()
    def bind(self,request,root):
        try:return self._bind(request,root)
        except Exception:self.abort()
    def _bind(self,request,root):
        if request!=self.request or Path(root).resolve()!=self.repo/'data/market-data':raise ValueError('dynamic binding mismatch')
        self.assert_current()
    def report(self,*args,**kwargs):raise ValueError('baseline report interface forbidden')
    def _verify(self,repo,sha,**kwargs):
        if kwargs:raise ValueError('baseline verifier forbidden')
        result=durable.verify_publication(repo,sha,required_manifest=self.budget_relative)
        manifest=json.loads(owner.git(repo,'show',f'{sha}:{self.budget_relative}'))
        if manifest['stage']!=self.mode or manifest['request_id']!=self.request_id:raise ValueError('dynamic manifest identity mismatch')
        return result
    def _manifest(self,paths):
        ledger=json.loads(self.safe_path(self.budget_relative).read_bytes())
        ledger.update(request_id=self.request_id,trade_date=self.day,stage=self.mode,required_outputs=[
            dict(path=p.removeprefix('data/market-data/'),blob_sha=blob(self.safe_path(p).read_bytes())) for p in sorted(set(paths))])
        self.safe_path(self.budget_relative).write_bytes(encode(ledger))
    def abort(self):
        self.failed=True
        raise DynamicPublicationUnknown('uncertain local storage or publication; no further writes') from None
    def publish(self,paths,**kwargs):
        try:return self._publish(paths,**kwargs)
        except Exception:self.abort()
    def _publish(self,paths,**kwargs):
        if self.failed:raise DynamicPublicationUnknown('dynamic owner stopped')
        if paths==[self.budget_relative]:
            if not self.active or self.safe_path(self.active['claim']).exists():raise ValueError('duplicate debit')
            value=dict(schema_version=1,mode=self.mode,status='CONSERVATIVE_RESERVATION',symbol=self.active['symbol'],execution_day=self.day,
                run_id=self.request_id,business_request_id=self.request['request_id'],request_commit=self.request_commit,
                request_blob_sha=self.request_blob,request_sha256=self.request_sha,compute_sha=self.compute_sha,
                provider_charge_status='NOT_VERIFIED',automatic_retry_allowed=False,
                **({'intraday_scope':self.intraday_scope} if self.intraday_scope else {}))
            raw=encode(value);self.safe_path(self.active['claim']).write_bytes(raw);self.active['claim_blob']=blob(raw)
            self._manifest([self.active['claim']]);paths=[self.budget_relative,self.active['claim']];self.predebit_publish_started=True
        try:return super().publish(paths,**kwargs)
        except Exception:
            self.failed=True
            raise DynamicPublicationUnknown('publication outcome unknown; no further writes') from None
    def reserve(self,symbol,h,budget):
        try:return self._reserve(symbol,h,budget)
        except owner.AdmissionError as exc:
            if exc.code in ('budget_exhausted','budget_slot_expired','deadline_exceeded','invalid_target','input_not_mature'):raise
            self.abort()
        except Exception:self.abort()
    def _reserve(self,symbol,h,budget):
        if self.failed:raise DynamicPublicationUnknown('dynamic owner stopped')
        self.reservation='NOT_STARTED'
        self.assert_current()
        if self.active is not None or symbol not in self.request['candidate_symbols']:raise ValueError('unclosed or unselected symbol')
        identity=(self.request['request_id']+'\0'+symbol).encode()
        if self.intraday_scope:identity+=b'\0'+encode(self.intraday_scope)
        token=hashlib.sha256(identity).hexdigest()
        namespace='dynamic-intraday-attempt' if self.intraday_scope else 'dynamic-daily-attempt'
        claim=f'data/market-data/collector-state/{namespace}-{token}.json';self.allowed.add(claim)
        if self.safe_path(claim).exists():raise ValueError('attempt already exists')
        self.active=dict(symbol=symbol,claim=claim,called=False);self.predebit_publish_started=False
        before=owner.read_budget_state(self.safe_path(self.budget_relative),self.day)['credits']
        try:self.active['debit']=owner.debit_before_request(h,self,self.repo/'data/market-data',self.day,budget,
                clock=self.clock,deadline=self.deadline,compute=self.compute,compute_sha=self.compute_sha)
        except BaseException as exc:
            if getattr(exc,'debited',False):self.reservation='DEBIT_CONFIRMED_NO_HTTP'
            elif self.predebit_publish_started:self.reservation='DEBIT_UNCONFIRMED'
            elif owner.read_budget_state(self.safe_path(self.budget_relative),self.day)['credits']!=before:self.reservation='LOCAL_DEBIT_UNPUBLISHED'
            else:self.reservation='NOT_DEBITED'
            raise
        self.reservation='DEBIT_CONFIRMED'
    def before_request(self):
        try:return self._before_request()
        except owner.AdmissionError as exc:
            if exc.code in ('budget_slot_expired','deadline_exceeded','invalid_target'):raise
            self.abort()
        except Exception:self.abort()
    def _before_request(self):
        if not self.active or self.active['called']:raise ValueError('guard callback reentry or missing claim')
        debit=self.active['debit'];owner.guard_provider_request(self,debit,self.compute,self.compute_sha,self.day,clock=self.clock,deadline=self.deadline)
        if owner.git(self.repo,'rev-parse',f'{debit.store_commit_sha}:{self.active["claim"]}')!=self.active['claim_blob']:raise ValueError('claim identity drift')
        ledger=json.loads(self.safe_path(self.budget_relative).read_bytes())
        if ledger['required_outputs']!=[dict(path=self.active['claim'].removeprefix('data/market-data/'),blob_sha=self.active['claim_blob'])]:raise ValueError('claim binding invalid')
        self.active['called']=True
    def retain_raw(self,relative):
        try:return self._retain_raw(relative)
        except Exception:self.abort()
    def _retain_raw(self,relative):
        if not self.active or not self.active['called']:raise ValueError('raw without guarded request')
        prefix=self.raw_prefix(self.active['symbol'])
        if not relative.startswith(prefix) or not re.fullmatch(re.escape(prefix)+r'[0-9a-f]{64}\.raw',relative):raise ValueError('raw path invalid')
        path='data/market-data/'+relative;self.allowed.add(path);raw=self.safe_path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=relative[-68:-4]:raise ValueError('raw hash mismatch')
        self.active['raw']=path;self._manifest([self.active['claim'],path]);self.publish([self.budget_relative,path])
    def finish(self,relative_paths,status):
        try:return self._finish(relative_paths,status)
        except Exception:self.abort()
    def _finish(self,relative_paths,status):
        if self.failed:raise DynamicPublicationUnknown('dynamic owner stopped')
        if not self.active:return []
        if not self.safe_path(self.active['claim']).exists():
            # A pre-publication local mutation must not be smuggled into outer save.
            if owner.git(self.repo,'status','--porcelain'):raise DynamicPublicationUnknown('unpublished local debit; stop')
            self.active=None;return []
        if self.reservation not in ('DEBIT_CONFIRMED','DEBIT_CONFIRMED_NO_HTTP'):self.abort()
        symbol=self.active['symbol'];paths={self.active['claim']}
        for relative in relative_paths:
            if relative.endswith('-twelve-data-budget.json'):continue
            series=bool(re.fullmatch(rf'series/daily/twelve_data_basic/{re.escape(symbol)}/(?:_index|[0-9]{{6}})\.json',relative))
            normalized=bool(re.fullmatch(rf'collector-state/{re.escape(self.day[:7])}/price-only-[0-9a-f]{{64}}\.json',relative))
            if self.intraday_scope:
                series=normalized=False
                if relative==self.active.get('selected_capture'):
                    capture_path='data/market-data/'+relative;self.allowed.add(capture_path)
                    data=json.loads(self.safe_path(capture_path).read_bytes())
                    scope=self.intraday_scope;provider=data.get('provider');window=scope['start'].replace(':','')+'-'+scope['end'].replace(':','')
                    prefix=f"sessions/{scope['trade_date'][:7]}/{scope['trade_date']}/regular/{window}/{provider}/"
                    if (status=='SELECTED_TWELVE')!=(provider=='twelve_data_basic'):raise ValueError('selected fallback mismatch')
                    if provider not in ('nasdaq_public_intraday','twelve_data_basic') or not relative.startswith(prefix) or '/' in relative[len(prefix):]:raise ValueError('intraday capture path')
                    if data.get('trade_date')!=scope['trade_date'] or data.get('session')!='regular' or data.get('window')!={'start':scope['start'],'end':scope['end']}:raise ValueError('intraday capture scope')
                    if not data.get('capture_id','').startswith(scope['stage']+'-'+symbol.lower()+'-') or not data.get('qualified_facts') or any(x.get('symbol')!=symbol for x in data['qualified_facts']):raise ValueError('intraday capture identity')
                    series=True
            if not (series or normalized or relative==self.active.get('raw','').removeprefix('data/market-data/')):raise ValueError('unexpected symbol closure path')
            path='data/market-data/'+relative;self.allowed.add(path);data=self.safe_path(path).read_bytes()
            if normalized and (hashlib.sha256(data).hexdigest()!=relative[-69:-5] or json.loads(data)['symbol']!=symbol):raise ValueError('normalized evidence mismatch')
            paths.add(path)
        terminal=self.active['claim'][:-5]+'-terminal.json';self.allowed.add(terminal)
        self.safe_path(terminal).write_bytes(encode(dict(schema_version=1,mode=self.mode,symbol=symbol,status=status,
            market_fact_authority=False,provider_charge_status='NOT_VERIFIED',reservation=self.reservation,automatic_retry_allowed=False,
            **({'intraday_scope':self.intraday_scope,'fallback_selected':status=='SELECTED_TWELVE','selected_capture':self.active.get('selected_capture')} if self.intraday_scope else {}))))
        paths.add(terminal);self._manifest(paths);self.publish([self.budget_relative,*sorted(paths)])
        self.active=None
        return [p.removeprefix('data/market-data/') for p in paths]
