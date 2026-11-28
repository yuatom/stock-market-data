"""Inert manual repair core. No CLI, credential loading, trigger, or issuer.

The hosted owner must independently verify a reviewed private finite grant,
calendar, actual manual caller, readiness, source/release and old-writer drain
before providing owner_gate. That verifier must also bind this exact intent and
run ID; the input object is not authority. No schedule uses this subclass.
"""
from dataclasses import dataclass
import re
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_updates import DailyUpdater, guarded

# One file-only idempotency marker per missing session, in the same existing
# private evidence family and native budget CAS. Bound the new work per request.
MAX_REPAIR_SESSIONS=31
SOURCE_ID={'provider':B.PROVIDER,'symbol':'SOXX','mic':'XNMS',
           'session':'regular','interval':'1day','adjustment':'provider_splits'}
MARKER_ROOT='capture-evidence/daily-basis/manual-repair/'

@dataclass(frozen=True)
class RepairIntent:
    symbol:str
    start_date:str
    end_date:str

@dataclass(frozen=True,repr=False)
class RepairPlan:
    missing_sessions:tuple[str,...]
    authorized_request_limits:tuple[int,int,int]
    logical_scope:str

class ManualUpdater(DailyUpdater):
    @guarded
    def __init__(self,*args,intent,request_id,owner_gate,**kwargs):
        B._need(type(intent) is RepairIntent and intent.symbol=='SOXX' and
                B._day(intent.start_date)<=B._day(intent.end_date))
        B._need(type(request_id) is str and re.fullmatch('[1-9][0-9]*-1',request_id))
        self._intent=intent; self._request_id=request_id; self._owner_gate=owner_gate
        super().__init__(*args,**kwargs)
        g=self.grant
        B._need(len(g['slots'])==1 and g['slots'][0]['cutoff']==intent.end_date and
                (g['request_limit'],g['credit_limit'],g['verification_limit'])==(1,1,1))

    def _assert_owner(self,request_id):
        B._need(request_id==self._request_id and callable(self._owner_gate) and
                self._owner_gate(request_id,self.authority,self._intent) is True)

    def _fresh(self,slot):
        self._assert_owner(self._request_id)
        return super()._fresh(slot)

    def _plan(self,slot,head,old):
        dates=self._sessions(slot)
        B._need(dates[-1]==self._intent.end_date and self._intent.start_date in dates and
                old['last_trade_date'] in dates)
        missing=tuple(dates[dates.index(old['last_trade_date'])+1:])
        B._need(0<len(missing)<=MAX_REPAIR_SESSIONS and self._intent.start_date<=missing[0])
        # Do not salt by grant, Compute pin, run, nominal slot, Store HEAD or
        # active basis ID: overlapping re-issues must collide by source/session.
        logical=B.sha(B.encode({'operation':'manual_soxx_repair_v1','source':SOURCE_ID,
                               'sessions':list(missing)}))
        B._need(all(not self.store.exists(head,self._claim_path(d)) for d in missing))
        return RepairPlan(missing,(1,1,1),logical)

    @staticmethod
    def _claim_path(date):
        return MARKER_ROOT+date[:7]+'/'+date+'.json'

    @guarded
    def preflight_slot(self,slot_id,request_id):
        self._assert_owner(request_id)
        _,head,_,_,old=prepared=super().preflight_slot(slot_id,request_id)
        self._plan(prepared[0],head,old)
        return prepared

    @guarded
    def preflight(self,slot_id,request_id):
        """Read-only simulation: invoke native preflight, never claim or run."""
        slot,head,_,_,old=self.preflight_slot(slot_id,request_id)
        return self._plan(slot,head,old)

    def _validate_claim_context(self,claim):
        # Use independently supplied original producer identity, not a
        # subsequent verifier run or candidate-authored self-consistency.
        B._need(type(claim['request_id']) is str and claim['request_id']==self._request_id)
        B._need(all(type(claim[k]) is int and claim[k]==1 for k in
              ('schema_version','request_limit','credit_limit','verification_limit')))

    def _extra_debit_claims(self,slot,head,previous):
        # Exact parent during debit and during independent candidate validation.
        # Native debit performs its own owner/time check, lock and parent CAS.
        plan=self._plan(slot,head,self._snapshot(previous))
        return {self._claim_path(d):B.encode(dict(schema_version=1,kind='soxx_manual_repair_claim',
                status='CLAIMED_NO_REPLAY',trade_date=d,logical_scope=plan.logical_scope))
                for d in plan.missing_sessions}
