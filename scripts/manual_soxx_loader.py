"""Disabled authenticated-owner read adapter; no issuer, command or effects.

Its independently approved configuration and runtime observations are trust
inputs. Loader reads the existing private Store through the existing strict
owner transport, but neither a tag nor a digest grants authority by itself.
"""
from dataclasses import dataclass
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_remote import RemoteGitStore
from scripts.daily_basis_updates import ET, DailyUpdater, GrantAuthority
from scripts.manual_soxx_repair import MAX_REPAIR_SESSIONS, ManualUpdater, RepairIntent
from scripts.manual_soxx_child import RESERVATION_ROOT
from scripts.manual_soxx_maturity import require_manual_mature
from scripts.private_authority_receipt import ManualReceiptOrigin, ManualRepairReceipt, verify_manual_repair_receipt

_ROOT='basis-authority/manual-soxx-repair/'
_DESCRIPTOR=_ROOT+'descriptor.json'
_RECEIPT=_ROOT+'receipt.json'
_GRANT=_ROOT+'grant.json'
_NATIVE_CAL=_ROOT+'native-calendar.json'
_LOOKAHEAD=_ROOT+'lookahead.json'
_DESC_FILES=frozenset({_DESCRIPTOR,_GRANT,_NATIVE_CAL,_LOOKAHEAD})

class LoaderHeld(RuntimeError):pass

@dataclass(frozen=True,repr=False)
class ApprovedAuthorityRead:
    origin:ManualReceiptOrigin
    carrier_sha:str
    commitment:str
    consumer_sha:str
    verifier_source_sha:str
    grant_sha256:str

@dataclass(frozen=True,repr=False)
class CurrentRuntimePins:
    compute_sha:str
    consumer_sha:str
    verifier_source_sha:str

@dataclass(frozen=True,repr=False)
class ManualPreflight:
    intent:RepairIntent
    receipt:ManualRepairReceipt
    slot_id:str
    authority:GrantAuthority
    grant_raw:bytes
    native_calendar_raw:bytes
    lookahead_raw:bytes

    def __repr__(self):return '<ManualPreflight>'
    @property
    def authorization_authenticated(self):return False
    @property
    def production_activation(self):return False

def _retained(store,sha):
    tag='refs/tags/market-data-read/'+B._hex(sha)
    B._need(store.materialize_ref(tag)==sha)

def _exact_added(store,sha,parent,expected):
    B._need(store.g.commit(sha)[1]==(parent,))
    entries=store.g.text('diff-tree','--no-commit-id','--name-only','--no-renames','-r',
                         parent,sha).splitlines()
    B._need(len(entries)==len(expected) and set(entries)==expected)
    for path in expected:
        B._need(not store.exists(parent,path))
        store.g.normal_blob_identity(sha,path)

def load_manual_preflight(*,store,approved,observe_current,clock):
    """Under the owner's existing Store lock, return private read-only inputs.

    Read-only Store means no publish/CAS/registration: local materialization
    can fetch and cache only sanctioned Git refs. It does not authorize a
    dispatcher, create a reservation, or let one act on cached pin checks.
    """
    try:
        B._need(type(store) is RemoteGitStore and type(approved) is ApprovedAuthorityRead and
                type(approved.origin) is ManualReceiptOrigin and callable(observe_current) and callable(clock))
        store.assert_locked()
        origin=approved.origin
        base=B._hex(origin.authority_store_sha);descriptor=B._hex(origin.descriptor_commit)
        carrier=B._hex(approved.carrier_sha);commitment=B._hex(approved.commitment,64)
        pins=CurrentRuntimePins(B._hex(origin.compute_sha),B._hex(approved.consumer_sha),
                               B._hex(approved.verifier_source_sha))
        expected_grant=B._hex(approved.grant_sha256,64)
        B._need(type(origin.repository_id) is int and type(origin.owner_id) is int and
                type(observe_current_value:=observe_current()) is CurrentRuntimePins and observe_current_value==pins)
        # Existing concrete transport pins these IDs on authenticated metadata.
        B._need(store.head()==base and store._repository_id==origin.repository_id and store._owner_id==origin.owner_id)
        ref='refs/heads/basis-daily-authority/'+pins.compute_sha+'/'+commitment
        B._need(store.remote_ref(ref)==carrier and store.materialize_ref(ref)==carrier)
        for commit in (base,descriptor,carrier):_retained(store,commit)
        verifier_ref='refs/heads/basis-consumer-delivery/'+pins.verifier_source_sha
        B._need(store.remote_ref(verifier_ref)==pins.verifier_source_sha)
        _exact_added(store,descriptor,base,_DESC_FILES)
        _exact_added(store,carrier,descriptor,{_RECEIPT})
        receipt_raw=store.read(carrier,_RECEIPT)
        receipt=verify_manual_repair_receipt(receipt_raw,commitment,origin,now=clock())
        desc=B._parse(store.read(descriptor,_DESCRIPTOR))
        B._need(type(desc) is dict and type(desc.get('schema_version')) is int and desc==dict(
            schema_version=1,kind='manual_soxx_repair_descriptor',symbol='SOXX',
            start_date=receipt.start_date,end_date=receipt.end_date,compute_sha=pins.compute_sha,
            consumer_sha=pins.consumer_sha,verifier_source_sha=pins.verifier_source_sha,
            base_store_sha=base,grant_sha256=expected_grant,
            native_calendar_sha256=receipt.native_calendar_sha256,lookahead_sha256=receipt.lookahead_sha256))
        grant_raw=store.read(descriptor,_GRANT);native=store.read(descriptor,_NATIVE_CAL)
        lookahead=store.read(descriptor,_LOOKAHEAD)
        B._need(B.sha(grant_raw)==expected_grant and B.sha(native)==receipt.native_calendar_sha256 and
                B.sha(lookahead)==receipt.lookahead_sha256)
        require_manual_mature(receipt,native,lookahead,now=clock())
        authority=GrantAuthority(expected_grant,pins.compute_sha,pins.consumer_sha,pins.verifier_source_sha,
                                 receipt.native_calendar_sha256)
        # Reuse native maturity and verified active vintage before any
        # reservation would be possible, with no made-up producer run ID.
        updater=DailyUpdater(store,grant_raw,native,authority,clock=clock)
        grant=updater.grant;slots=grant['slots']
        B._need(len(slots)==1 and slots[0]['cutoff']==receipt.end_date and
                grant['repository_id']==origin.repository_id and grant['owner_id']==origin.owner_id and
                (grant['request_limit'],grant['credit_limit'],grant['verification_limit'])==(1,1,1))
        slot=slots[0];updater._fresh(slot)
        _,old=updater._previous(slot,base);dates=updater._sessions(slot)
        suffix=tuple(d for d in dates if d>old['last_trade_date'])
        B._need(receipt.start_date in dates and dates[-1]==receipt.end_date and
                1<=len(suffix)<=MAX_REPAIR_SESSIONS and receipt.start_date<=suffix[0])
        B._need(not store.exists(base,updater._prefix(slot)+'/claim.json') and
                store.remote_ref(updater._candidate_ref(slot)) is None and
                store.remote_ref(updater._attempt_ref(slot)) is None)
        B._need(all(not store.exists(base,ManualUpdater._claim_path(d)) and
                    not store.exists(base,RESERVATION_ROOT+d+'.json') for d in suffix))
        final_pins=observe_current()
        B._need(type(final_pins) is CurrentRuntimePins and final_pins==pins and
                store.head()==base and store.remote_ref(ref)==carrier and
                store.remote_ref(verifier_ref)==pins.verifier_source_sha)
        # The final source/ref reads can exhaust the lease. Use one new
        # sample after all I/O; only pure checks may follow it.
        final_now=clock()
        receipt=verify_manual_repair_receipt(receipt_raw,commitment,origin,now=final_now)
        require_manual_mature(receipt,native,lookahead,now=final_now)
        B._need(B._stamp(slot['valid_from'])<=final_now<B._stamp(slot['valid_until']) and
                final_now.astimezone(ET).date().isoformat()==slot['execution_day'])
        return ManualPreflight(RepairIntent('SOXX',receipt.start_date,receipt.end_date),receipt,slot['slot_id'],
                               authority,grant_raw,native,lookahead)
    except Exception:
        raise LoaderHeld('MANUAL_SOXX_AUTHORITY_PREFLIGHT_HELD') from None
