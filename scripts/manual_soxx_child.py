"""Disabled manual child adapter: no workflow, issuer, network or CLI.

The caller must supply independent reviewed approval, authenticated exact
workflow-run lookup, real source/platform checks, and the existing single
Store owner. Supplying Python values does not establish these authorities.
"""
from dataclasses import dataclass
import re
from scripts.manual_soxx_repair import (ManualUpdater, RepairIntent, RepairPlan,
                                         MAX_REPAIR_SESSIONS)
from scripts.daily_basis_updates import Candidate, GrantAuthority
from scripts import provider_basis_snapshot as B

COMPUTE='yuatom/stock-market-data'
RESERVATION_ROOT='basis-authority/manual-soxx-dispatch-intents/'

class ChildHeld(RuntimeError):pass

@dataclass(frozen=True,repr=False)
class ApprovedLaunch:
    parent_sha:str
    reservation_sha:str
    intent:RepairIntent
    compute_sha:str
    consumer_sha:str
    grant_sha256:str
    calendar_sha256:str
    repository_id:int
    owner_id:int
    owner_login:str
    workflow_id:int
    workflow_path:str
    job:str
    run_id:int
    not_after:str

@dataclass(frozen=True,repr=False)
class CandidatePendingVerification:
    plan:RepairPlan
    candidate:Candidate

def reservation_records(plan):
    """Canonical broker bytes; child derives them independently from native plan."""
    B._need(type(plan) is RepairPlan and plan.authorized_request_limits==(1,1,1))
    B._hex(plan.logical_scope,64)
    dates=plan.missing_sessions
    B._need(type(dates) is tuple and 1<=len(dates)<=MAX_REPAIR_SESSIONS and
            dates==tuple(sorted({B._day(d) for d in dates})))
    return {RESERVATION_ROOT+d+'.json':B.encode(dict(schema_version=1,
            kind='manual_soxx_dispatch_intent',status='RESERVED_NO_REDISPATCH',
            logical_scope=plan.logical_scope,trade_date=d)) for d in dates}

def _check_platform(approved,env,observe_run):
    B._need(type(approved) is ApprovedLaunch and type(env) is dict and callable(observe_run))
    for key in ('repository_id','owner_id','workflow_id','run_id'):
        value=getattr(approved,key)
        B._need(type(value) is int and value>0)
    B._need(re.fullmatch(r'[A-Za-z0-9-]{1,39}',approved.owner_login) and
            re.fullmatch(r'\.github/workflows/[a-z0-9_-]+\.ya?ml',approved.workflow_path) and
            re.fullmatch(r'[A-Za-z0-9_-]{1,64}',approved.job))
    expected=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY=COMPUTE,GITHUB_SHA=B._hex(approved.compute_sha),
        GITHUB_WORKFLOW_SHA=approved.compute_sha,
        GITHUB_REF='refs/heads/main',GITHUB_WORKFLOW_REF=COMPUTE+'/'+approved.workflow_path+'@refs/heads/main',
        GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID=str(approved.run_id),
        GITHUB_ACTOR=approved.owner_login,GITHUB_TRIGGERING_ACTOR=approved.owner_login,GITHUB_JOB=approved.job)
    B._need(all(env.get(k)==v for k,v in expected.items()))
    run=observe_run(approved.run_id)
    B._need(type(run) is dict and type(run.get('id')) is int and run['id']==approved.run_id and
           type(run.get('workflow_id')) is int and run['workflow_id']==approved.workflow_id and
           type(run.get('run_attempt')) is int and run['run_attempt']==1 and
           run['path']==approved.workflow_path and run['head_sha']==approved.compute_sha and
           run['event']=='workflow_dispatch' and run['head_branch']=='main' and run['status']=='in_progress')
    for key in ('repository','head_repository'):
        identity=run[key]
        B._need(type(identity.get('id')) is int and identity['id']==approved.repository_id and
                identity['full_name']==COMPUTE)
    for key in ('actor','triggering_actor'):
        identity=run[key]
        B._need(type(identity.get('id')) is int and identity['id']==approved.owner_id and
                identity['login']==approved.owner_login)
    return str(approved.run_id)+'-1'

def _retained(store,commit):
    tag='refs/tags/market-data-read/'+commit
    if hasattr(store,'materialize_ref'):
        B._need(store.materialize_ref(tag)==commit)
    B._need(store.ref(tag)==commit)

def _reservation_delta(store,approved):
    before=B._hex(approved.parent_sha);after=B._hex(approved.reservation_sha)
    B._need(before!=after and store.head()==after and
            store.g.commit(after)[1]==(before,))
    _retained(store,before);_retained(store,after)
    delta=store.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',
                       before,after).splitlines()
    B._need(1<=len(delta)<=MAX_REPAIR_SESSIONS and len(delta)==len(set(delta)) and all(
        re.fullmatch(re.escape(RESERVATION_ROOT)+r'[0-9]{4}-[0-9]{2}-[0-9]{2}\.json',p)
        for p in delta))
    return set(delta)

class _ReservedUpdater(ManualUpdater):
    def __init__(self,*args,reservation_sha,**kwargs):
        self._reservation_sha=reservation_sha
        super().__init__(*args,**kwargs)

    def _debit(self,slot,request_id,head,previous):
        # Ordinary native preflight may accept a later benign compatible head.
        # This child is specifically bound to the broker's exact reserved head.
        self.store.assert_locked()
        B._need(head==self._reservation_sha==self.store.head() and previous['read_sha']==head)
        return super()._debit(slot,request_id,head,previous)

def run_authorized_child(*,store,approved,observed_env,observe_run,grant_raw,calendar_raw,
                         grant_authority,slot_id,clock,owner_current,capture):
    """Run one bound child through native candidate validation, not promotion."""
    with store.lock():
        try:
            B._need(type(approved) is ApprovedLaunch and callable(owner_current) and
                    callable(clock) and callable(capture) and type(grant_authority) is GrantAuthority)
            B._need(clock()<B._stamp(approved.not_after) and owner_current(approved) is True)
            request_id=_check_platform(approved,observed_env,observe_run)
            B._need(grant_authority.compute_sha==approved.compute_sha and
                    grant_authority.consumer_sha==B._hex(approved.consumer_sha) and
                    grant_authority.grant_sha256==B._hex(approved.grant_sha256,64)==B.sha(grant_raw) and
                    grant_authority.calendar_sha256==B._hex(approved.calendar_sha256,64)==B.sha(calendar_raw))
            grant=B._parse(grant_raw)
            B._need(type(grant['owner_id']) is int and grant['owner_id']==approved.owner_id)
            delta=_reservation_delta(store,approved)
            def gate(rid,authority,intent):
                return (rid==request_id and authority==grant_authority and intent==approved.intent and
                        clock()<B._stamp(approved.not_after) and owner_current(approved) is True)
            updater=_ReservedUpdater(store,grant_raw,calendar_raw,grant_authority,clock=clock,
                       intent=approved.intent,request_id=request_id,owner_gate=gate,
                       reservation_sha=approved.reservation_sha)
            plan=updater.preflight(slot_id,request_id)
            expected=reservation_records(plan)
            B._need(set(expected)==delta and store.head()==approved.reservation_sha)
            for path,raw in expected.items():
                B._need(not store.exists(approved.parent_sha,path) and
                        store.g.normal_blob_identity(approved.reservation_sha,path)==B.blob_sha(raw) and
                        store.read(approved.reservation_sha,path)==raw)
        except Exception:
            raise ChildHeld('MANUAL_SOXX_CHILD_HELD_BEFORE_DEBIT') from None
        # Same shared lock covers preflight and native debit CAS; after debit
        # retain native STOP/unknown semantics rather than claim no debit.
        candidate=updater.run_slot(slot_id,request_id,capture=capture)
        updater.validate_candidate(candidate)
        return CandidatePendingVerification(plan,candidate)
