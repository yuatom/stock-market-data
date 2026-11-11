"""Fake providers only. Logical date scope stays distinct from wire datetime."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import parse_qs,urlsplit
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_raw_response_evidence import request,body
from test_capture_only_response import access
import rebase_market_data_baseline as capture


def module():
    import inclusive_daily_wire
    return inclusive_daily_wire


def logical_request():
    value=request();value.update(start_date='2026-12-02',end_date='2026-12-03')
    return value


def test_exact_same_date_wire_boundary_keeps_logical_request_unchanged():
    logical=logical_request();before=dict(logical)
    params=module().daily_wire_parameters(logical)
    assert logical==before
    assert params['start_date']=='2026-12-02'
    assert params['end_date']=='2026-12-03 23:59:59'
    assert params['outputsize']==logical['outputsize'] and params['format']=='JSON'
    assert 'apikey' not in params


@pytest.mark.parametrize('field,value',[('end_date','2026-12-03 23:59:59'),('end_date','2026-2-03'),
    ('end_date','2026-02-30'),('end_date','2026-12-01'),('start_date',None)])
def test_mapper_never_accepts_already_widened_or_invalid_scope(field,value):
    logical=logical_request();logical[field]=value
    with pytest.raises(ValueError):module().daily_wire_parameters(logical)


@pytest.mark.parametrize('returned,allowed',[('2026-12-02',True),('2026-12-03',True),('2026-12-04',False),('2026-12-01',False)])
def test_opt_in_capture_url_and_logical_response_bounds(monkeypatch,returned,allowed):
    logical=logical_request();doc=body();doc['values']=[dict(doc['values'][0],datetime=returned)]
    raw=json.dumps(doc).encode();seen=[];retained={}
    def http(url,**options):
        query=parse_qs(urlsplit(url).query);seen.append(query)
        assert urlsplit(url).scheme=='https' and urlsplit(url).netloc=='api.twelvedata.com'
        assert query['end_date']==['2026-12-03 23:59:59']
        assert query['start_date']==['2026-12-02'] and query['outputsize']==['5000']
        return raw
    monkeypatch.setattr(capture.collectors,'_http_raw',http)
    def retain(name):
        def save(payload):retained[name]=payload;return hashlib.sha256(payload).hexdigest()
        return save
    args=dict(request=logical,api_key='synthetic-key',access=access(),before_request=lambda:None,
        retain_raw=retain('raw'),retain_envelope=retain('envelope'),
        clock=lambda:datetime(2026,12,5,13,tzinfo=timezone.utc),inclusive_end_of_day=True)
    if allowed:
        result=capture.capture_daily_response(**args)
        assert result['requests_attempted']==1
        assert json.loads(retained['envelope'])['request']==logical
    else:
        with pytest.raises(capture.CaptureOnlyError):capture.capture_daily_response(**args)
        assert 'envelope' not in retained
    assert len(seen)==1 and retained['raw']==raw


def test_wire_plan_is_pre_request_intent_without_credentials():
    plan=module().daily_wire_request_plan(logical_request())
    assert plan['kind']=='provider_wire_request_plan' and plan['credential_omitted'] is True
    assert plan['endpoint']=='https://api.twelvedata.com/time_series'
    assert plan['parameters']==module().daily_wire_parameters(logical_request())
    assert 'apikey' not in json.dumps(plan).lower()
