"""Synthetic clocks and ledgers for the guarded predebit window."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import admit_market_data_baseline as a

DAY='2026-10-15'
BUDGET={'hard_credits_per_day':200,'hard_credits_per_minute':8}

class Clock:
    def __init__(self,second,latency=0):
        self.now=datetime(2026,10,15,12,0,tzinfo=timezone.utc)+timedelta(seconds=second)
        self.elapsed=0.0;self.latency=latency;self.sleeps=[];self.events=[]
    def clock(self):return self.now
    def monotonic(self):return self.elapsed
    def advance(self,seconds):self.now+=timedelta(seconds=seconds);self.elapsed+=seconds
    def sleep(self,seconds):self.events.append('sleep');self.sleeps.append(seconds);self.advance(seconds)


def run(clock, *, deadline=180):
    def consume(*args,**kwargs):clock.events.append('debit')
    def publish(paths):clock.events.append('publish');clock.advance(clock.latency);return 'a'*40
    h=SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=lambda *args,**kwargs:None,consume_twelve_credit=consume))
    writer=SimpleNamespace(budget_relative='budget.json',publish=publish)
    return a.debit_before_request(h,writer,Path('/unused'),DAY,BUDGET,clock=clock.clock,
        sleep=clock.sleep,monotonic=clock.monotonic,deadline=deadline,maturity='07:00')


@pytest.mark.parametrize(('second','latency'),[(51,7.915282),(54,4),(38,0),(38.000001,0)])
def test_late_window_waits_before_debit(second,latency):
    clock=Clock(second,latency);proof=run(clock)
    assert clock.events.index('sleep') < clock.events.index('debit')
    assert proof.minute=='2026-10-15T12:01Z'


def test_early_window_does_not_wait():
    clock=Clock(1,7.915282);run(clock)
    assert not clock.sleeps and clock.events==['debit','publish']


def test_deadline_rejects_wait_without_debit():
    clock=Clock(51)
    with pytest.raises(a.AdmissionError,match='deadline_exceeded'):run(clock,deadline=4)
    assert 'debit' not in clock.events


def test_post_debit_slow_publication_still_blocks_without_refund():
    clock=Clock(1,58)
    with pytest.raises(a.AdmissionError) as exc:run(clock)
    assert exc.value.code=='budget_slot_expired' and exc.value.debited
    assert clock.events.count('debit')==1


def test_budget_file_read_crossing_window_must_recheck_before_debit(tmp_path,monkeypatch):
    import json
    clock=Clock(37);path=tmp_path/'budget.json';counts=[]
    original=a.validate_budget_file
    def slow(*args,**kwargs):
        counts.append(1)
        if len(counts)==2:clock.advance(1.5)
        return original(*args,**kwargs)
    monkeypatch.setattr(a,'validate_budget_file',slow)
    monkeypatch.setattr(a,'git',lambda *args:'f'*40)
    def consume(*args,**kwargs):
        clock.events.append('debit');minute=clock.now.strftime('%Y-%m-%dT%H:%MZ')
        path.write_text(json.dumps({'trade_date':DAY,'credits':1,'minutes':{minute:1}}))
    h=SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=lambda *a,**k:None,consume_twelve_credit=consume))
    w=SimpleNamespace(budget_relative='budget.json',safe_path=lambda _:path,repo=tmp_path,publish=lambda _:'a'*40)
    a.debit_before_request(h,w,tmp_path,DAY,BUDGET,clock=clock.clock,sleep=clock.sleep,monotonic=clock.monotonic,deadline=180,maturity='07:00')
    assert clock.events.index('sleep') < clock.events.index('debit')


def test_et_date_rollover_during_wait_blocks_before_debit():
    clock=Clock(51);clock.now=datetime(2026,10,16,3,59,51,tzinfo=timezone.utc)
    with pytest.raises(a.AdmissionError,match='invalid_target'):run(clock)
    assert 'debit' not in clock.events


@pytest.mark.parametrize('change',['store','compute','budget'])
def test_drift_or_exhaustion_while_waiting_blocks_before_debit(tmp_path,monkeypatch,change):
    import json
    clock=Clock(51);path=tmp_path/'budget.json';state={'store':False,'compute':False}
    def pause(seconds):
        clock.sleep(seconds)
        if change in state:state[change]=True
        if change=='budget':path.write_text(json.dumps({'trade_date':DAY,'credits':200,'minutes':{}}))
    def current():
        if state['store']:raise a.AdmissionError('object_drift')
    def fake_git(repo,*args):
        if args[0]=='rev-parse':return 'a'*40
        if args[0]=='status':return ''
        if args[0]=='ls-remote':return ('b' if state['compute'] else 'a')*40+' refs/heads/main'
        return 'f'*40
    monkeypatch.setattr(a,'git',fake_git)
    def consume(*args,**kwargs):clock.events.append('debit')
    h=SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=lambda *args,**kwargs:None,consume_twelve_credit=consume))
    w=SimpleNamespace(budget_relative='budget.json',safe_path=lambda _:path,repo=tmp_path,publish=lambda _:'a'*40,assert_current=current)
    with pytest.raises(a.AdmissionError) as exc:
        a.debit_before_request(h,w,tmp_path,DAY,BUDGET,clock=clock.clock,sleep=pause,monotonic=clock.monotonic,
            deadline=180,maturity='07:00',compute=Path('/compute'),compute_sha='a'*40)
    assert exc.value.code == ('budget_exhausted' if change=='budget' else 'object_drift')
    assert 'debit' not in clock.events


def test_unknown_publication_does_not_retry_or_redebit():
    clock=Clock(1);calls=[]
    def consume(*args,**kwargs):calls.append('debit')
    def publish(paths):calls.append('publish');raise a.AdmissionError('publication_failed')
    h=SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=lambda *args,**kwargs:None,consume_twelve_credit=consume))
    w=SimpleNamespace(budget_relative='budget.json',publish=publish)
    with pytest.raises(a.AdmissionError):
        a.debit_before_request(h,w,Path('/unused'),DAY,BUDGET,clock=clock.clock,sleep=clock.sleep,
            monotonic=clock.monotonic,deadline=180,maturity='07:00')
    assert calls==['debit','publish']


def test_budget_wait_honors_monotonic_deadline(tmp_path,monkeypatch):
    import json
    import market_data_collectors as collectors
    root=tmp_path/'data';path=collectors._budget_path(root,DAY);path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'trade_date':DAY,'credits':8,'minutes':{'2026-10-15T12:00Z':8}}))
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,15,12,0,51,tzinfo=timezone.utc)
    monkeypatch.setattr(collectors,'datetime',Frozen)
    monkeypatch.setattr(collectors.time,'monotonic',lambda:99)
    slept=[];monkeypatch.setattr(collectors.time,'sleep',lambda n:slept.append(n))
    with pytest.raises(TimeoutError):
        collectors.wait_for_twelve_budget(root,DAY,per_day=200,per_minute=8,deadline=100)
    assert not slept


def test_timing_events_contain_only_stage_and_utc_timestamp():
    clock=Clock(51,4);events=[]
    h=SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=lambda *args,**kwargs:None,consume_twelve_credit=lambda *args,**kwargs:None))
    w=SimpleNamespace(budget_relative='budget.json',publish=lambda _: 'a'*40)
    a.debit_before_request(h,w,Path('/unused'),DAY,BUDGET,clock=clock.clock,sleep=clock.sleep,
        monotonic=clock.monotonic,deadline=180,maturity='07:00',timing=events)
    assert {'window_check','window_wait_start','window_wait_end','debit_start','publication_verified'} <= {e['stage'] for e in events}
    assert all(set(e)=={'stage','at_utc'} for e in events)


def test_just_before_headroom_cutoff_does_not_wait():
    clock=Clock(37.999999);run(clock)
    assert not clock.sleeps and clock.events==['debit','publish']
