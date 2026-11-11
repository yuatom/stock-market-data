"""Historical market cutoff maturity is separate from today's credit ledger."""
from datetime import datetime
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import admit_market_data_baseline as admission


@pytest.mark.parametrize('cutoff,stamp,allowed',[
    ('2026-12-09','2026-12-11T05:00:00+00:00',True),
    ('2026-12-10','2026-12-11T11:59:59+00:00',False),
    ('2026-12-10','2026-12-11T12:00:00+00:00',True),
    ('2026-12-11','2026-12-11T13:00:00+00:00',False),
    ('2026-12-12','2026-12-11T13:00:00+00:00',False),
    ('2026-12-09','2026-12-11T04:59:59+00:00',False),
])
def test_cutoff_boundary_and_current_accounting_day(cutoff,stamp,allowed):
    args=('2026-12-11',cutoff,'07:00',datetime.fromisoformat(stamp))
    if allowed: admission.validate_historical_target(*args)
    else:
        with pytest.raises(admission.AdmissionError):admission.validate_historical_target(*args)


@pytest.mark.parametrize('day,cutoff,stamp,allowed',[
    ('2026-03-08','2026-03-07','2026-03-08T10:59:59+00:00',False),
    ('2026-03-08','2026-03-07','2026-03-08T11:00:00+00:00',True),
    ('2026-11-01','2026-10-31','2026-11-01T11:59:59+00:00',False),
    ('2026-11-01','2026-10-31','2026-11-01T12:00:00+00:00',True),
])
def test_next_calendar_day_et_boundary_handles_dst(day,cutoff,stamp,allowed):
    args=(day,cutoff,'07:00',datetime.fromisoformat(stamp))
    if allowed:admission.validate_historical_target(*args)
    else:
        with pytest.raises(admission.AdmissionError):admission.validate_historical_target(*args)


@pytest.mark.parametrize('field,value',[('day','2026-12-10'),('cutoff','2026-2-03'),
    ('cutoff','2026-02-30'),('cutoff',None),('maturity','24:00'),('maturity','7:00'),
    ('maturity','07:60'),('maturity',None),('now',datetime(2026,12,11,13))])
def test_invalid_input_is_not_a_maturity_bypass(field,value):
    args=dict(day='2026-12-11',cutoff='2026-12-09',maturity='07:00',now=datetime.fromisoformat('2026-12-11T13:00:00+00:00'))
    args[field]=value
    with pytest.raises(admission.AdmissionError):admission.validate_historical_target(**args)


def test_legacy_current_day_settlement_guard_is_unchanged():
    now=datetime.fromisoformat('2026-12-11T06:00:00+00:00')
    with pytest.raises(admission.AdmissionError):admission.validate_target('2026-12-11','07:00',now)
