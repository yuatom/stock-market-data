"""Fixed-window adapter candidate for the existing durable attempt owner.

Invocation metadata MUST come from the existing workflow resolver. This class
neither authenticates a caller nor creates new trigger/retry authorization.
The existing Actions run-attempt id is an accounting key, not a fabricated
business transaction or a cross-run exactly-once promise. Not workflow enabled.
"""
import hashlib
from dynamic_daily_owner import DynamicDailyOwner,encode
from admit_market_data_baseline import StorePublisher

WINDOWS={'open_15m':('09:30','09:45'),'open_30m':('09:45','10:00'),
         'open_60m':('10:00','10:30'),'close':('15:45','16:00')}

class FixedLiveOwner(DynamicDailyOwner):
    def __init__(self,repo,expected,day,run_id,*,compute,compute_sha,stage,symbols,invocation,clock,deadline,collection_mode=None):
        if stage not in WINDOWS:raise ValueError('not a fixed regular window')
        capture_mode=collection_mode or stage
        if (stage=='close' and capture_mode not in {'close','close_retry','close_final'}) or (stage!='close' and capture_mode!=stage):
            raise ValueError('fixed collection mode/stage mismatch')
        # Preserve optional business identities without making them required for
        # schedule/manual or importing dynamic first-introduction restrictions.
        self.request=dict(request_id=run_id,trade_date=day,stage=stage,candidate_symbols=list(symbols),
            control_request_id=invocation.get('request_id'),transaction_id=invocation.get('transaction_id'),
            logical_trigger=invocation.get('trigger'),collection_mode=capture_mode)
        self.request_raw=encode(self.request)
        self.compute=compute
        StorePublisher.__init__(self,repo,expected,day,run_id,verify=self._verify,report_namespace='dynamic-daily-attempt')
        self.compute=compute;self.compute_sha=compute_sha;self.clock=clock;self.deadline=deadline
        self.request_commit=invocation.get('control_ref');self.request_blob=invocation.get('request_blob')
        self.request_sha=hashlib.sha256(self.request_raw).hexdigest()
        self.intraday_scope=None;self.allowed={self.budget_relative};self.active=None
        self.reservation='NOT_STARTED';self.failed=False;self.predebit_publish_started=False
    def reserve(self,symbol,h,budget):
        from admit_market_data_baseline import AdmissionError
        from dynamic_intraday_response import KnownIntradayRejection
        try:
            return super().reserve(symbol,h,budget)
        except AdmissionError as exc:
            # Only deterministic pre-debit budget/time refusals may retain the
            # fixed window's qualified primary facts. No HTTP or publication
            # may have begun, and clean exact HEAD must still be verified.
            if (exc.code not in {'budget_exhausted','deadline_exceeded','budget_slot_expired'}
                    or self.failed or self.reservation!='NOT_DEBITED'
                    or not self.active or self.active.get('called')
                    or self.predebit_publish_started
                    or self.safe_path(self.active['claim']).exists()):
                self.abort()
            self.assert_current()
            self.finish([], 'REJECTED')  # Existing no-claim cleanup; no fake terminal.
            raise KnownIntradayRejection(exc.code) from None
    @property
    def mode(self):return 'fixed_intraday_attempt'
    def business_request_identity(self):return self.request.get('control_request_id')
    def capture_identity_prefix(self,symbol):return self.request['collection_mode']+'-'+symbol.lower()+'-'
    def attempt_namespace(self):return 'fixed-intraday-attempt'
    def raw_prefix(self,symbol):return f'collector-state/{self.day[:7]}/fixed-intraday-{symbol}-'
    def begin_intraday(self,trade_date,stage,start,end):
        if self.active or trade_date!=self.day or stage!=self.request['stage'] or (start,end)!=WINDOWS[stage]:self.abort()
        self.assert_current();self.intraday_outcomes=[]
        self.intraday_scope=dict(provider='twelve_data_basic',stage=stage,trade_date=trade_date,start=start,end=end,interval='1min')

class WorkflowFixedOwnerFactory:
    """In-process adapter for already-resolved workflow metadata, not a resolver."""
    def __init__(self,*,repo,compute,compute_sha,run_id,invocation,clock,deadline):
        from pathlib import Path
        self.repo=Path(repo).resolve();self.compute=compute;self.compute_sha=compute_sha
        self.run_id=run_id;self.invocation=dict(invocation);self.clock=clock;self.deadline=deadline
        self.latest_owner=None
    def __call__(self,*,mode,stage,trade_date,start_et,end_et,store_root,symbols):
        from pathlib import Path
        from admit_market_data_baseline import git
        if Path(store_root).resolve()!=self.repo/'data/market-data':raise ValueError('Store root mismatch')
        if stage not in WINDOWS or (start_et,end_et)!=WINDOWS[stage]:raise ValueError('fixed window mismatch')
        owner=FixedLiveOwner(self.repo,git(self.repo,'rev-parse','HEAD'),trade_date,self.run_id,
            compute=self.compute,compute_sha=self.compute_sha,stage=stage,symbols=symbols,
            invocation=self.invocation,clock=self.clock,deadline=self.deadline,collection_mode=mode)
        self.latest_owner=owner
        return owner
