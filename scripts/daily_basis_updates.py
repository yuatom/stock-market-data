"""Finite daily literal-vintage producer, admission, and selected-current reader.

Authority objects are trusted invocation dependencies, not proof of consent.
No schedule, remote publisher or credentials are created. LocalGitStore performs
real local Git transactions only. A hosted owner must supply authenticated
source/permission/lock/free-usage checks and separately verified private logs.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import re
import sys
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_store import LocalGitStore
from scripts.raw_response_evidence import verify_envelope,validate_request
# Existing proof/capture owners use their established script-local imports.
_legacy_scripts=str(Path(__file__).resolve().parent)
if _legacy_scripts not in sys.path:sys.path.insert(0,_legacy_scripts)
from scripts.build_price_basis_metric_proof import build_price_basis_proof

ET=ZoneInfo('America/New_York')
CATALOG=B.catalog_path(B.PROVIDER,'SOXX')
GRANT_KEYS=('schema_version','kind','repository','repository_id','owner_id','compute_sha','consumer_sha',
    'workflow_source_sha','workflow_id','calendar_sha256','initial_selector','baseline_ref','request_template',
    'slots','request_limit','credit_limit','verification_limit','hard_credits_per_day',
    'hard_credits_per_minute','maturity_next_day_et','private_destination','actions_free_only',
    'allowed_actions','volume_policy')


class DailyBasisError(RuntimeError):pass


def guarded(function):
    def call(*args,**kwargs):
        try:return function(*args,**kwargs)
        except Exception:raise DailyBasisError('DAILY_BASIS_STOPPED_INSPECT_NO_RETRY') from None
    return call


@dataclass(frozen=True,repr=False)
class GrantAuthority:
    grant_sha256:str
    compute_sha:str
    consumer_sha:str
    workflow_source_sha:str
    calendar_sha256:str


@dataclass(frozen=True,repr=False)
class Candidate:
    grant_sha256:str
    slot_id:str
    expected_head:str
    selector:dict
    receipt_commitment:str
    receipt_ref:str
    receipt_commit:str
    receipt_blob_sha:str
    receipt_until:str


class DailyUpdater:
    @guarded
    def __init__(self,store,grant_raw,calendar_raw,authority,*,clock):
        B._need(type(authority) is GrantAuthority)
        self.store=store;self.clock=clock;self.authority=authority
        self.grant_raw=grant_raw;self.calendar_raw=calendar_raw
        g=B._parse(grant_raw);c=B._parse(calendar_raw)
        B._need(B.sha(grant_raw)==B._hex(authority.grant_sha256,64))
        B._closed(g,GRANT_KEYS)
        B._need(g['schema_version']==1 and type(g['schema_version']) is int and g['kind']=='finite_daily_basis_grant')
        for key in ('compute_sha','consumer_sha','workflow_source_sha'):
            B._need(g[key]==B._hex(getattr(authority,key)))
        B._need(g['calendar_sha256']==B._hex(authority.calendar_sha256,64)==B.sha(calendar_raw))
        B._need(g['repository']=='yuatom/stock-market-data-store' and g['private_destination']=='capture-evidence/daily-basis')
        for key in ('repository_id','owner_id','workflow_id'):
            B._need(type(g[key]) is int and g[key]>0)
        B._need(g['actions_free_only'] is True and g['allowed_actions']==['capture','private_verify','immutable_catalog_update'])
        B._need(g['volume_policy']=='BLOCKED_UNRESOLVED' and g['maturity_next_day_et']=='07:00')
        B._need(type(g['hard_credits_per_day']) is int and g['hard_credits_per_day']==200 and
                type(g['hard_credits_per_minute']) is int and g['hard_credits_per_minute']==8)
        B._selection(g['initial_selector']);B._need(g['initial_selector']['symbol']=='SOXX')
        req=validate_request(g['request_template'])
        B._need(req['symbol']=='SOXX' and req['mic_code']=='XNMS' and req['dp']==-1 and req['outputsize']==5000)
        slots=g['slots'];B._need(type(slots) is list and 1<=len(slots)<=31)
        for key in ('request_limit','credit_limit','verification_limit'):
            B._need(type(g[key]) is int and g[key]==len(slots))
        seen=set();last=None
        for slot in slots:
            B._closed(slot,('slot_id','execution_day','cutoff','valid_from','valid_until','cleanup_until','receipt_until'))
            B._need(type(slot['slot_id']) is str and re.fullmatch('[a-z0-9][a-z0-9-]{0,39}',slot['slot_id']) and slot['slot_id'] not in seen)
            seen.add(slot['slot_id']);day=B._day(slot['execution_day']);cut=B._day(slot['cutoff'])
            a,b,e=(B._stamp(slot[k]) for k in ('valid_from','valid_until','receipt_until'))
            mature=datetime.combine(datetime.fromisoformat(cut).date()+timedelta(days=1),datetime.strptime('07:00','%H:%M').time(),ET)
            cleanup=B._stamp(slot['cleanup_until'])
            B._need(b+timedelta(minutes=5)<=cleanup<=b+timedelta(minutes=30) and cleanup<=e)
            B._need(mature<=a<b<=e and b-a<=timedelta(hours=12) and e-a<=timedelta(hours=24) and
                a.astimezone(ET).date().isoformat()==day and (b-timedelta(microseconds=1)).astimezone(ET).date().isoformat()==day)
            B._need(last is None or last['cutoff']<cut and B._stamp(last['valid_until'])<=a)
            last=slot
        B._closed(c,('schema_version','kind','source_url','known_at','sessions'))
        B._need(type(c['schema_version']) is int and c['schema_version']==1 and c['kind']=='reviewed_finite_daily_calendar' and
                c['source_url']=='https://www.nyse.com/trade/hours-calendars')
        B._stamp(c['known_at']);dates=[]
        B._need(type(c['sessions']) is list and 1<=len(c['sessions'])<=5000)
        for row in c['sessions']:
            B._closed(row,('trade_date','scheduled_close'));dates.append(B._day(row['trade_date']))
            B._need(row['scheduled_close'] in ('13:00','16:00'))
        B._need(dates==sorted(set(dates)) and dates[0]==req['start_date'] and dates[-1]==slots[-1]['cutoff'])
        B._need(all(s['cutoff'] in dates for s in slots))
        ref=g['baseline_ref'];B._closed(ref,('commit','path','blob_sha','sha256'))
        baseline=B._blob(store.g,ref['commit'],ref['path'],ref['blob_sha'])
        B._need(B.sha(baseline)==B._hex(ref['sha256'],64))
        initial=g['initial_selector'];self._view(initial,dates=[r['trade_date'] for r in c['sessions'] if r['trade_date']<=req['end_date']],now=self.clock())
        snapshot=self._snapshot(initial)
        B._need(snapshot['baseline_evidence_sha256']==ref['sha256'] and snapshot['request']==req)
        B._need(B._stamp(c['known_at'])<=self.clock())
        self._validate_grant_unchanged()

    @property
    def grant(self):return B._parse(self.grant_raw)
    @property
    def calendar(self):return B._parse(self.calendar_raw)
    @property
    def digest(self):return self.authority.grant_sha256
    def _validate_grant_unchanged(self):
        B._need(B.sha(self.grant_raw)==self.digest and B.sha(self.calendar_raw)==self.authority.calendar_sha256)
    def _prefix(self,slot):return f'capture-evidence/daily-basis/{self.digest}/{slot["slot_id"]}'
    def _transition_path(self,slot):return self._prefix(slot)+'/transition.json'
    def _attempt_ref(self,slot):return f'refs/heads/basis-daily-attempts/{self.digest}/{slot["slot_id"]}'
    def _candidate_ref(self,slot):return f'refs/heads/basis-daily-candidates/{self.digest}/{slot["slot_id"]}'
    def _slot(self,slot_id):
        rows=[s for s in self.grant['slots'] if s['slot_id']==slot_id];B._need(len(rows)==1);return rows[0]
    def _sessions(self,slot):return [r['trade_date'] for r in self.calendar['sessions'] if r['trade_date']<=slot['cutoff']]
    def _snapshot(self,sel):
        return B._validate_snapshot(B._parse(B._blob(self.store.g,sel['read_sha'],B.snapshot_path(B.PROVIDER,'SOXX',sel['basis_id']),sel['snapshot_blob_sha'])),sel['basis_id'])
    def _view(self,sel,*,dates,now):
        return B.read_price_view(self.store.repo,sel,B.ReadAuthority(B.sha(B.encode(sel)),sel['admission_blob_sha']),fields=list(B.PRICE_FIELDS),required_sessions=dates,knowledge_cutoff=now.isoformat())
    def _fresh(self,slot):
        self.store.assert_locked();self._validate_grant_unchanged()
        now=self.clock();B._need(isinstance(now,datetime) and now.tzinfo is not None)
        B._need(B._stamp(slot['valid_from'])<=now<B._stamp(slot['valid_until']) and now.astimezone(ET).date().isoformat()==slot['execution_day'])
        mature=datetime.combine(datetime.fromisoformat(slot['cutoff']).date()+timedelta(days=1),datetime.strptime('07:00','%H:%M').time(),ET)
        B._need(now>=mature and B._stamp(self.calendar['known_at'])<=now)
        return now

    def _current_selector(self,head):
        catalog=B._parse(self.store.read(head,CATALOG));sel=catalog|{'read_sha':head};B._selection(sel);return sel

    def _unselected_initial(self,slot,head):
        # First promotion may start from a separately approved isolated candidate.
        # Never select the initial catalog on main merely to make capture runnable.
        B._need(slot == self.grant['slots'][0])
        prior=self.grant['initial_selector']
        parents=self.store.g.commit(prior['read_sha'])[1]
        B._need(len(parents)==1)
        base=parents[0]
        B._need(not self.store.exists(base,CATALOG) and not self.store.exists(head,CATALOG))
        B._need(self.store.g.text('merge-base',base,head)==base)
        history=self.store.g.text('rev-list','--parents','--max-count=513',base+'..'+head).splitlines()
        B._need(len(history)<=512)
        for record in history:
            parts=record.split();B._need(len(parts)>=2)
            for parent in parts[1:]:
                B._need(parent==base or self.store.g.text('merge-base',base,parent)==base)
                B._need(not self.store.g.read('diff-tree','--no-commit-id','--name-only','--no-renames','-r',parent,parts[0],'--',CATALOG))
        return prior

    def _previous(self,slot,head):
        slots=self.grant['slots'];idx=slots.index(slot)
        prior=self.grant['initial_selector']
        for s in slots[:idx]:
            transition=B._parse(self.store.read(head,self._transition_path(s)))
            candidate=self.store.materialize_ref(self._candidate_ref(s));B._hex(candidate)
            B._need(transition['grant_sha256']==self.digest and transition['slot_id']==s['slot_id'] and transition['previous_basis_id']==prior['basis_id'])
            B._need(self.store.g.text('merge-base',candidate,head)==candidate)
            prior=transition['catalog']|{'read_sha':candidate}
        current=(self._current_selector(head) if self.store.exists(head,CATALOG)
                 else self._unselected_initial(slot,head))
        B._need({k:v for k,v in current.items() if k!='read_sha'}=={k:v for k,v in prior.items() if k!='read_sha'})
        snap=self._snapshot(current)
        dates=[d for d in self._sessions(slot) if d<=snap['last_trade_date']]
        self._view(current,dates=dates,now=self.clock())
        B._need(snap['last_trade_date']<slot['cutoff'] and snap['first_trade_date']==self._sessions(slot)[0])
        return current,snap

    def _debit(self,slot,request_id,head,previous):
        now=self._fresh(slot);minute=now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
        B._need(now.second+now.microsecond/1_000_000<38)
        day=slot['execution_day'];path=f'data/market-data/collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        old=B._parse(self.store.read(head,path)) if self.store.exists(head,path) else dict(schema_version=1,trade_date=day,credits=0,minutes={})
        B._closed(old,('schema_version','trade_date','credits','minutes'))
        B._need(type(old['schema_version']) is int and old['schema_version']==1 and old['trade_date']==day and type(old['credits']) is int and 0<=old['credits']<200 and type(old['minutes']) is dict)
        for key,value in old['minutes'].items():
            B._need(type(key) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\dZ',key))
            parsed=datetime.strptime(key,'%Y-%m-%dT%H:%MZ').replace(tzinfo=timezone.utc)
            B._need(parsed<=now and parsed.astimezone(ET).date().isoformat()==day and type(value) is int and 0<=value<=8)
        B._need(sum(old['minutes'].values())<=old['credits'] and old['minutes'].get(minute,0)<8)
        B._need(sum(self.store.exists(head,self._prefix(s)+'/claim.json') for s in self.grant['slots'])<self.grant['request_limit'])
        claim=dict(schema_version=1,kind='daily_basis_attempt',status='CLAIMED_NO_REPLAY',grant_sha256=self.digest,slot_id=slot['slot_id'],request_id=request_id,previous_selector=previous,claimed_at=now.isoformat(),budget_minute=minute,request_limit=1,credit_limit=1,verification_limit=1)
        new=dict(old,credits=old['credits']+1,minutes=dict(old['minutes']))
        new['minutes'][minute]=new['minutes'].get(minute,0)+1
        new['minutes']=dict(sorted(new['minutes'].items())[-16:])
        updates={self._prefix(slot)+'/claim.json':B.encode(claim),path:B.encode(new)}
        newhead=self.store.publish(head,updates,replace={path} if self.store.exists(head,path) else set())
        return newhead,minute,path,B.blob_sha(B.encode(new))

    @guarded
    def run_slot(self,slot_id,request_id,*,capture):
        slot=self._slot(slot_id);self._fresh(slot)
        B._need(type(request_id) is str and re.fullmatch('[1-9][0-9]*-1',request_id))
        head=self.store.head();prefix=self._prefix(slot)
        B._need(not self.store.exists(head,prefix+'/claim.json') and self.store.ref(self._candidate_ref(slot)) is None)
        B._need(self.store.ref(self._attempt_ref(slot)) is None)
        previous,old=self._previous(slot,head)
        head,minute,budget_path,budget_blob=self._debit(slot,request_id,head,previous)
        evidence_head=head
        evidence_ref_expected='0'*40
        request=dict(self.grant['request_template'],end_date=slot['cutoff'])
        guarded_once=False
        def before_request():
            nonlocal guarded_once
            B._need(not guarded_once);guarded_once=True
            self._fresh(slot);B._need(self.store.head()==head)
            B._need(self.store.g.normal_blob_identity(head,budget_path)==budget_blob)
            now=self.clock().astimezone(timezone.utc)
            B._need(now.strftime('%Y-%m-%dT%H:%MZ')==minute and now.second<58)
        retained={}
        publication_uncertain=False
        def retain(name,raw):
            nonlocal evidence_head,evidence_ref_expected,publication_uncertain
            self.store.assert_locked();self._validate_grant_unchanged()
            B._need(B._stamp(slot['valid_from'])<=self.clock()<B._stamp(slot['cleanup_until']))
            B._need(guarded_once and name not in retained and not publication_uncertain)
            next_head=self.store.commit(evidence_head,{prefix+'/'+name:raw},replace=set())
            publication_uncertain=True
            self.store.cas(self._attempt_ref(slot),next_head,evidence_ref_expected)
            publication_uncertain=False;evidence_head=next_head;evidence_ref_expected=next_head
            retained[name]=raw;return B.sha(raw)
        # Every exception after durable claim leaves an at-most-once consumed
        # slot. Do not refund, repeat HTTP, roll back or fabricate success.
        try:
            result=capture(request=request,before_request=before_request,
                retain_raw=lambda raw:retain('response.raw',raw),
                retain_envelope=lambda raw:retain('envelope.json',raw),clock=self.clock)
        except Exception:
            # Only confirmed retained-ref state permits a separate terminal.
            # Never write another ref after an uncertain evidence publication.
            if guarded_once and not publication_uncertain and self.clock()<B._stamp(slot['cleanup_until']):
                retain('terminal.json',B.encode(dict(schema_version=1,status='CAPTURE_STOPPED_INSPECT_NO_RETRY',grant_sha256=self.digest,slot_id=slot_id,request_id=request_id,market_qualified=False,production_activation=False)))
            raise
        B._need(guarded_once and set(retained)=={'response.raw','envelope.json'})
        envelope=retained['envelope.json'];verified=verify_envelope(envelope,B.sha(envelope))
        B._need(verified['raw_response']==retained['response.raw'] and verified['request']==request)
        B._need(result.get('status')=='CAPTURE_RETAINED_UNQUALIFIED' and result.get('envelope_sha256')==B.sha(envelope) and result.get('raw_response_sha256')==B.sha(retained['response.raw']))
        terminal=dict(status='CAPTURE_RETAINED_UNQUALIFIED',schema_version=1,request_id=request_id,compute_sha=self.grant['compute_sha'],grant_sha256=self.digest,slot_id=slot_id,envelope_sha256=B.sha(envelope),raw_response_sha256=B.sha(retained['response.raw']),market_qualified=False,production_activation=False)
        retain('terminal.json',B.encode(terminal))
        now=self._fresh(slot);B._need(self.store.head()==head)
        B._need(B._stamp(verified['observed_at'])<=now and B._stamp(verified['observed_at'])>=B._stamp(slot['valid_from']) and B._stamp(old['observed_at'])<B._stamp(verified['observed_at']))
        B._need([r['datetime'] for r in verified['records']]==self._sessions(slot))
        new_by={r['datetime']:r for r in verified['records']}
        for row in old['records']:
            B._need(row['datetime'] in new_by)
            for field in (*B.PRICE_FIELDS,'volume'):
                B._need(Decimal(row[field])==Decimal(new_by[row['datetime']][field]))
        ref=self.grant['baseline_ref'];baseline=B._blob(self.store.g,ref['commit'],ref['path'],ref['blob_sha'])
        snapshot=B.prepare_snapshot(envelope,B.sha(envelope),baseline,old['split_event'])
        B._need(snapshot['unresolved_volume_dates']==old['unresolved_volume_dates'] and snapshot['volume_differences']==old['volume_differences'])
        admission=dict(schema_version=1,kind='provider_snapshot_price_admission',status='ADMITTED_PRICE_ONLY',basis_id=snapshot['basis_id'],allowed_fields=list(B.PRICE_FIELDS),volume_policy='BLOCKED_UNRESOLVED',admitted_at=now.isoformat(),proof_model='reviewed_workflow_receipt_not_provider_signature',envelope_ref=dict(path=prefix+'/envelope.json',blob_sha=B.blob_sha(envelope)),workflow_evidence=dict(compute_sha=self.grant['compute_sha'],request_id=request_id,terminal_ref=dict(path=prefix+'/terminal.json',blob_sha=B.blob_sha(B.encode(terminal)))),unresolved_volume_dates=snapshot['unresolved_volume_dates'])
        ar=B.encode(admission);sr=B.encode(snapshot)
        catalog=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id=snapshot['basis_id'],snapshot_blob_sha=B.blob_sha(sr),admission_blob_sha=B.blob_sha(ar))
        transition=dict(schema_version=1,kind='immutable_daily_basis_transition',grant_sha256=self.digest,slot_id=slot_id,previous_basis_id=old['basis_id'],previous_selector=previous,catalog=catalog,calendar_sha256=self.authority.calendar_sha256,compute_sha=self.authority.compute_sha,consumer_sha=self.authority.consumer_sha,overlap_count=len(old['records']),new_count=len(snapshot['records'])-len(old['records']),price_comparison='EXACT_DECIMAL',volume_policy='BLOCKED_UNRESOLVED')
        updates={B.snapshot_path(B.PROVIDER,'SOXX',snapshot['basis_id']):sr,B.admission_path(B.PROVIDER,'SOXX',B.blob_sha(ar)):ar,CATALOG:B.encode(catalog),self._transition_path(slot):B.encode(transition)}
        updates.update({prefix+'/'+name:raw for name,raw in retained.items()})
        preliminary=self.store.commit(head,updates,replace={CATALOG})
        sel=catalog|{'read_sha':preliminary};auth=B.ReadAuthority(B.sha(B.encode(sel)),sel['admission_blob_sha'])
        proof=build_price_basis_proof(self.store.repo,sel,auth,required_sessions=self._sessions(slot),knowledge_cutoff=now.isoformat(),compute_sha=self.authority.compute_sha,scope='prior_session_core_context')
        updates[prefix+'/price-proof.json']=B.encode(proof)
        candidate=self.store.commit(head,updates,replace={CATALOG})
        self.store.cas(self._candidate_ref(slot),candidate,'0'*40)
        selector=catalog|{'read_sha':candidate}
        self._view(selector,dates=self._sessions(slot),now=now)
        raw=B.encode(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=self.authority.compute_sha,selector=selector,admission_blob_sha=selector['admission_blob_sha'],nonce=secrets.token_hex(32),valid_from=now.isoformat(),valid_until=slot['receipt_until']))
        commitment=B.receipt_commitment(raw);receipt_ref=f'refs/heads/basis-read-receipts/{self.authority.compute_sha}/{commitment}'
        receipt_commit=self.store.commit(candidate,{'basis-authority/receipt.json':raw},replace={'basis-authority/receipt.json'} if self.store.exists(candidate,'basis-authority/receipt.json') else set())
        self.store.cas(receipt_ref,receipt_commit,'0'*40)
        B._need(self.store.head()==head)
        return Candidate(self.digest,slot_id,head,selector,commitment,receipt_ref,receipt_commit,B.blob_sha(raw),slot['receipt_until'])

    @guarded
    def validate_candidate(self,candidate):
        """Independent semantic and exact-path closure, not trust in creator/ref."""
        B._need(type(candidate) is Candidate and candidate.grant_sha256==self.digest)
        slot=self._slot(candidate.slot_id);head=candidate.expected_head
        B._need(self.store.ref(self._candidate_ref(slot))==candidate.selector['read_sha'])
        B._need(self.store.g.commit(candidate.selector['read_sha'])[1]==(head,))
        previous,old=self._previous(slot,head);prefix=self._prefix(slot)
        claim=B._parse(self.store.read(head,prefix+'/claim.json'))
        B._closed(claim,('schema_version','kind','status','grant_sha256','slot_id','request_id','previous_selector','claimed_at','budget_minute','request_limit','credit_limit','verification_limit'))
        B._need(claim['grant_sha256']==self.digest and claim['slot_id']==candidate.slot_id and
                claim['kind']=='daily_basis_attempt' and claim['status']=='CLAIMED_NO_REPLAY' and
                claim['previous_selector']['basis_id']==old['basis_id'] and
                claim['request_limit']==claim['credit_limit']==claim['verification_limit']==1)
        # Claim+budget lives on main. Raw, envelope and unqualified terminal
        # are independently retained on the private attempt ref even if main
        # or the acquisition window moves during the authorized response.
        day=slot['execution_day'];budget=f'data/market-data/collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        evidence_head=self.store.ref(self._attempt_ref(slot));B._hex(evidence_head)
        cursor=evidence_head
        for name in ('terminal.json','envelope.json','response.raw'):
            parents=self.store.g.commit(cursor)[1];B._need(len(parents)==1)
            delta=self.store.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',parents[0],cursor).splitlines()
            B._need(set(delta)=={prefix+'/'+name} and not self.store.exists(parents[0],prefix+'/'+name))
            B._need(self.store.read(candidate.selector['read_sha'],prefix+'/'+name)==self.store.read(evidence_head,prefix+'/'+name))
            cursor=parents[0]
        B._need(cursor==head)
        parents=self.store.g.commit(head)[1];B._need(len(parents)==1)
        delta=self.store.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',parents[0],head).splitlines()
        B._need(set(delta)=={prefix+'/claim.json',budget} and not self.store.exists(parents[0],prefix+'/claim.json'))
        cursor=parents[0]
        expected_previous,_=self._previous(slot,cursor)
        B._need(claim['previous_selector']==expected_previous)
        before=B._parse(self.store.read(cursor,budget)) if self.store.exists(cursor,budget) else dict(schema_version=1,trade_date=day,credits=0,minutes={})
        after=B._parse(self.store.read(head,budget));minute=claim['budget_minute']
        expected_minutes=dict(before['minutes']);expected_minutes[minute]=expected_minutes.get(minute,0)+1
        expected_budget=dict(before,credits=before['credits']+1,minutes=dict(sorted(expected_minutes.items())[-16:]))
        B._need(after==expected_budget and after['credits']<=200 and after['minutes'][minute]<=8)
        envelope=self.store.read(evidence_head,prefix+'/envelope.json')
        e=verify_envelope(envelope,B.sha(envelope))
        B._need(e['raw_response']==self.store.read(evidence_head,prefix+'/response.raw') and
                e['request']==dict(self.grant['request_template'],end_date=slot['cutoff']) and
                [r['datetime'] for r in e['records']]==self._sessions(slot))
        terminal=B._parse(self.store.read(evidence_head,prefix+'/terminal.json'))
        B._need(terminal==dict(status='CAPTURE_RETAINED_UNQUALIFIED',schema_version=1,request_id=claim['request_id'],compute_sha=self.authority.compute_sha,grant_sha256=self.digest,slot_id=slot['slot_id'],envelope_sha256=B.sha(envelope),raw_response_sha256=B.sha(e['raw_response']),market_qualified=False,production_activation=False))
        B._need(B._stamp(slot['valid_from'])<=B._stamp(claim['claimed_at'])<=B._stamp(e['observed_at'])<B._stamp(slot['valid_until']))
        ref=self.grant['baseline_ref'];baseline=B._blob(self.store.g,ref['commit'],ref['path'],ref['blob_sha'])
        snapshot=B.prepare_snapshot(envelope,B.sha(envelope),baseline,old['split_event'])
        B._need(self._snapshot(candidate.selector)==snapshot and snapshot['volume_differences']==old['volume_differences'])
        by_date={r['datetime']:r for r in snapshot['records']}
        for row in old['records']:
            for field in (*B.PRICE_FIELDS,'volume'):B._need(Decimal(row[field])==Decimal(by_date[row['datetime']][field]))
        catalog={k:v for k,v in candidate.selector.items() if k!='read_sha'}
        transition=B._parse(self.store.read(candidate.selector['read_sha'],self._transition_path(slot)))
        B._need(transition==dict(schema_version=1,kind='immutable_daily_basis_transition',grant_sha256=self.digest,slot_id=slot['slot_id'],previous_basis_id=old['basis_id'],previous_selector=claim['previous_selector'],catalog=catalog,calendar_sha256=self.authority.calendar_sha256,compute_sha=self.authority.compute_sha,consumer_sha=self.authority.consumer_sha,overlap_count=len(old['records']),new_count=len(snapshot['records'])-len(old['records']),price_comparison='EXACT_DECIMAL',volume_policy='BLOCKED_UNRESOLVED'))
        expected_paths={B.snapshot_path(B.PROVIDER,'SOXX',snapshot['basis_id']),B.admission_path(B.PROVIDER,'SOXX',candidate.selector['admission_blob_sha']),CATALOG,self._transition_path(slot),prefix+'/price-proof.json'}
        expected_paths.update(prefix+'/'+name for name in ('response.raw','envelope.json','terminal.json'))
        delta=self.store.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',head,candidate.selector['read_sha']).splitlines()
        B._need(set(delta)==expected_paths)
        B._need(all(not self.store.exists(head,p) for p in expected_paths-{CATALOG}))
        auth=B.ReadAuthority(B.sha(B.encode(candidate.selector)),candidate.selector['admission_blob_sha'])
        proof=build_price_basis_proof(self.store.repo,candidate.selector,auth,required_sessions=self._sessions(slot),knowledge_cutoff=self.clock().isoformat(),compute_sha=self.authority.compute_sha,scope='prior_session_core_context')
        B._need(self.store.read(candidate.selector['read_sha'],prefix+'/price-proof.json')==B.encode(proof))
        raw=B._blob(self.store.g,candidate.receipt_commit,'basis-authority/receipt.json',candidate.receipt_blob_sha)
        sel,_=B.verify_consumer_receipt(raw,candidate.receipt_commitment,self.authority.compute_sha,now=self.clock().isoformat())
        B._need(sel==candidate.selector and self.store.ref(candidate.receipt_ref)==candidate.receipt_commit)
        receipt=B._receipt(raw)
        B._need(receipt['valid_until']==slot['receipt_until']==candidate.receipt_until and
                B._stamp(slot['valid_from'])<=B._stamp(receipt['valid_from'])<B._stamp(slot['valid_until']))
        return True

    def _verification_ref(self,slot):return f'refs/heads/basis-daily-verification/{self.digest}/{slot["slot_id"]}'

    @guarded
    def reserve_verification(self,candidate):
        """Create durable one-dispatch intent BEFORE dispatch, never retry it.

        A transport timeout leaves this reservation consumed. A later matching
        existing run may be inspected, but another dispatch is forbidden.
        """
        slot=self._slot(candidate.slot_id);self._fresh(slot);self.validate_candidate(candidate)
        ref=self._verification_ref(slot);B._need(self.store.ref(ref) is None)
        raw=B.encode(dict(schema_version=1,kind='daily_basis_verification_intent',grant_sha256=self.digest,slot_id=slot['slot_id'],selector=candidate.selector,receipt_commitment=candidate.receipt_commitment,workflow_source_sha=self.authority.workflow_source_sha,status='RESERVED_NO_REDISPATCH'))
        commit=self.store.commit(candidate.selector['read_sha'],{'basis-authority/daily-verification-intent.json':raw},replace=set())
        self.store.cas(ref,commit,'0'*40)
        return ref

    @guarded
    def delivery_expectation(self,candidate,*,run_id,for_read=False):
        from scripts.daily_basis_delivery import DailyDeliveryExpectation
        B._need(type(candidate) is Candidate and candidate.grant_sha256==self.digest)
        slot=self._slot(candidate.slot_id)
        B._need(type(for_read) is bool)
        if not for_read:self._fresh(slot)
        else:
            self.store.assert_locked();self._validate_grant_unchanged()
            B._need(self.clock()<B._stamp(candidate.receipt_until))
        self.validate_candidate(candidate)
        B._need(self.store.ref(self._candidate_ref(slot))==candidate.selector['read_sha'])
        reservation=self.store.ref(self._verification_ref(slot));B._hex(reservation)
        path='basis-authority/daily-verification-intent.json'
        blob=self.store.g.normal_blob_identity(reservation,path)
        self.store.g.single_addition(reservation,candidate.selector['read_sha'],path,blob)
        intent=B._parse(self.store.read(reservation,path))
        B._need(intent==dict(schema_version=1,kind='daily_basis_verification_intent',grant_sha256=self.digest,slot_id=slot['slot_id'],selector=candidate.selector,receipt_commitment=candidate.receipt_commitment,workflow_source_sha=self.authority.workflow_source_sha,status='RESERVED_NO_REDISPATCH'))
        return DailyDeliveryExpectation(self.digest,candidate.slot_id,self.grant['repository_id'],self.grant['owner_id'],self.grant['workflow_id'],self.authority.workflow_source_sha,self.authority.compute_sha,run_id,candidate)

    @guarded
    def promote(self,candidate,delivery):
        from scripts.daily_basis_delivery import VerifiedDailyDelivery
        B._need(type(candidate) is Candidate and type(delivery) is VerifiedDailyDelivery)
        slot=self._slot(candidate.slot_id);now=self._fresh(slot)
        B._need(delivery.grant_sha256==self.digest==candidate.grant_sha256 and delivery.slot_id==candidate.slot_id)
        B._need(delivery.candidate==candidate and B._stamp(delivery.verified_at)<=now<B._stamp(candidate.receipt_until))
        self.validate_candidate(candidate)
        B._need(self.store.head()==candidate.expected_head and self.store.ref(self._candidate_ref(slot))==candidate.selector['read_sha'])
        B._need(self.store.g.commit(candidate.selector['read_sha'])[1]==(candidate.expected_head,))
        selector,_=B.load_consumer_receipt(self.store.repo,candidate.receipt_commitment,self.authority.compute_sha,now=now.isoformat())
        B._need(selector==candidate.selector)
        self._view(selector,dates=self._sessions(slot),now=now)
        self.store.cas('refs/heads/main',candidate.selector['read_sha'],candidate.expected_head)
        return candidate.selector

    @guarded
    def read_current(self,delivery,*,fields,read_sha=None):
        from scripts.daily_basis_delivery import VerifiedDailyDelivery
        B._need(type(delivery) is VerifiedDailyDelivery and delivery.grant_sha256==self.digest)
        now=self.clock();c=delivery.candidate;slot=self._slot(c.slot_id)
        B._need(B._stamp(delivery.verified_at)<=now<B._stamp(c.receipt_until))
        self.validate_candidate(c)
        current=self._current_selector(self.store.head())
        B._need({k:v for k,v in current.items() if k!='read_sha'}=={k:v for k,v in c.selector.items() if k!='read_sha'})
        B._need(self.store.g.text('merge-base',c.selector['read_sha'],self.store.head())==c.selector['read_sha'])
        selector,authority=B.load_consumer_receipt(self.store.repo,c.receipt_commitment,self.authority.compute_sha,now=now.isoformat())
        B._need(selector==c.selector)
        # A daily receipt remains bound to its immutable candidate. Derive a
        # distinct finite-grant read authority only after proving unchanged
        # complete closure at the target and the currently selected main head.
        current_head=self.store.head();target=current_head if read_sha is None else B._hex(read_sha)
        B._need(self.store.g.text('merge-base',target,current_head)==target and
                self.store.g.text('merge-base',selector['read_sha'],target)==selector['read_sha'])
        prefix=self._prefix(slot)
        paths=[CATALOG,B.snapshot_path(B.PROVIDER,'SOXX',selector['basis_id']),
            B.admission_path(B.PROVIDER,'SOXX',selector['admission_blob_sha']),
            self._transition_path(slot),prefix+'/price-proof.json',prefix+'/response.raw',
            prefix+'/envelope.json',prefix+'/terminal.json']
        history=self.store.g.text('rev-list','--parents','--max-count=513',selector['read_sha']+'..'+current_head).splitlines()
        B._need(len(history)<=512)
        for record in history:
            parts=record.split();B._need(len(parts)>=2)
            for parent in parts[1:]:
                B._need(not self.store.g.read('diff-tree','--no-commit-id','--name-only','--no-renames','-r',parent,parts[0],'--',*paths))
        for path in paths:
            identity=self.store.g.normal_blob_identity(selector['read_sha'],path)
            B._need(self.store.g.normal_blob_identity(target,path)==identity==self.store.g.normal_blob_identity(current_head,path))
        retention='refs/tags/market-data-read/'+target
        if hasattr(self.store,'materialize_ref'):self.store.materialize_ref(retention)
        B._need(self.store.ref(retention)==target)
        lifted=dict(selector,read_sha=target)
        lifted_authority=B.ReadAuthority(B.sha(B.encode(lifted)),lifted['admission_blob_sha'])
        result=B.read_price_view(self.store.repo,lifted,lifted_authority,fields=fields,required_sessions=self._sessions(slot),knowledge_cutoff=now.isoformat())
        B._need(self.store.head()==current_head and self.clock()<B._stamp(c.receipt_until))
        return dict(result,admitted_candidate_sha=selector['read_sha'],daily_grant_sha256=self.digest)


def provider_capture(api_key,access):
    """Real existing bounded HTTP primitive. Construct only in approved hosted owner.

    Construction performs no I/O. Credentials never go in objects, receipts or
    logs. Calling this transport IS a provider request and needs the finite grant.
    """
    from scripts.rebase_market_data_baseline import capture_daily_response
    def capture(**kwargs):
        return capture_daily_response(api_key=api_key,access=access,inclusive_end_of_day=True,**kwargs)
    return capture


from scripts.daily_basis_delivery import verify_daily_delivery
