"""Manual-only lower timing bound. No calendar issuer or availability proof.

The native reviewed calendar remains unchanged (through approved cutoff). A
separate independently approved lookahead, bound by the private receipt, has
one explicit entry for every civil day from the cutoff through next trading
session. Neither sorting sessions nor claiming completeness is sufficient.
"""
from dataclasses import dataclass
from datetime import date,datetime,timedelta,time
from zoneinfo import ZoneInfo
from scripts import provider_basis_snapshot as B
from scripts.private_authority_receipt import ManualRepairReceipt

ET=ZoneInfo('America/New_York')
SOURCE_URL='https://www.nyse.com/trade/hours-calendars'
class ManualNotReady(RuntimeError):pass

@dataclass(frozen=True,repr=False)
class SettlementBoundary:
    native_not_before_et: datetime
    provider_after_et: datetime

def calendar_window(receipt,native_raw,lookahead_raw,*,now):
    try:
        B._need(type(receipt) is ManualRepairReceipt and isinstance(now,datetime) and
               now.tzinfo is not None and now.utcoffset() is not None and
               receipt.valid_from<=now<receipt.valid_until)
        B._need(B.sha(native_raw)==receipt.native_calendar_sha256 and B.sha(lookahead_raw)==receipt.lookahead_sha256)
        native=B._parse(native_raw);lookahead=B._parse(lookahead_raw)
        B._closed(native,('schema_version','kind','source_url','known_at','sessions'))
        B._closed(lookahead,('schema_version','kind','source_url','known_at',
               'native_calendar_sha256','cutoff','days'))
        B._need(type(native['schema_version']) is int and native['schema_version']==1 and
               native['kind']=='reviewed_finite_daily_calendar' and
               type(lookahead['schema_version']) is int and lookahead['schema_version']==1 and
               lookahead['kind']=='reviewed_manual_soxx_lookahead' and
               lookahead['source_url']==native['source_url']==SOURCE_URL and
               B._stamp(native['known_at'])<=now and B._stamp(lookahead['known_at'])<=now and
               lookahead['cutoff']==receipt.end_date and lookahead['native_calendar_sha256']==receipt.native_calendar_sha256)
        sessions=native['sessions']
        B._need(type(sessions) is list and 1<=len(sessions)<=5000)
        session_dates=[]
        for row in sessions:
            B._closed(row,('trade_date','scheduled_close'))
            session_dates.append(B._day(row['trade_date']))
            B._need(row['scheduled_close'] in ('13:00','16:00'))
        B._need(session_dates==sorted(set(session_dates)) and receipt.start_date in session_dates and
               session_dates[-1]==receipt.end_date)
        target=date.fromisoformat(receipt.end_date)
        days=lookahead['days']
        B._need(type(days) is list and 2<=len(days)<=8)
        for index,row in enumerate(days):
            B._closed(row,('trade_date','scheduled_close'))
            B._need(row['trade_date']==(target+timedelta(days=index)).isoformat())
            if index==0:B._need(row['scheduled_close']==sessions[-1]['scheduled_close'])
            elif index==len(days)-1:B._need(row['scheduled_close'] in ('13:00','16:00'))
            else:B._need(row['scheduled_close'] is None)
        next_trading=date.fromisoformat(days[-1]['trade_date'])
        return SettlementBoundary(datetime.combine(target+timedelta(days=1),time(7),ET),
                                  datetime.combine(next_trading,time(0),ET))
    except Exception:pass
    raise ManualNotReady('MANUAL_SOXX_UNPROVEN_SETTLEMENT_WINDOW')

def require_manual_mature(receipt,native_raw,lookahead_raw,*,now):
    timing=calendar_window(receipt,native_raw,lookahead_raw,now=now)
    if not (now>=timing.native_not_before_et and now>timing.provider_after_et):
        raise ManualNotReady('MANUAL_SOXX_BEFORE_SETTLEMENT_WINDOW')
    return timing

def before_reservation(receipt,native_raw,lookahead_raw,*,clock,proceed):
    require_manual_mature(receipt,native_raw,lookahead_raw,now=clock())
    return proceed()

def before_child_debit(receipt,native_raw,lookahead_raw,*,clock,proceed):
    require_manual_mature(receipt,native_raw,lookahead_raw,now=clock())
    return proceed()
