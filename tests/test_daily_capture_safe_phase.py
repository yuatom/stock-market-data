"""Offline safe diagnostic propagation; synthetic transport, real local updater."""
import json
from unittest.mock import patch
import pytest
from scripts import daily_basis_updates as D, run_one_shot_soxx_basis_gap as M
from scripts import rebase_market_data_baseline as R
from test_daily_basis_updates import setup
from test_capture_only_response import access


def output(call):
    try:M.step('CAPTURE_AND_CANDIDATE',call)
    except M.OneShotFailure as exc:return M.failure_result(exc)
    raise AssertionError('expected stop')


def test_real_capture_transport_through_updater_is_safe(tmp_path):
    _,repo,grant,calendar,paths,store,current,updater=setup(tmp_path)
    with store.lock(),patch.object(R.collectors,'_http_raw',side_effect=TimeoutError('SECRET https://private/?token=SECRET')) as http:
        result=output(lambda:updater.run_slot('nov10','100-1',capture=D.provider_capture('synthetic-key',access())))
    assert http.call_count==1
    assert result['failure_detail']=='CAPTURE_TRANSPORT'
    assert result['automatic_retry_allowed'] is False
    assert 'SECRET' not in json.dumps(result)
    assert 'requests_attempted' not in result and 'credits' not in result


@pytest.mark.parametrize('code,phase,count,expected',[
    ('capture_preflight_failed','preflight',0,'CAPTURE_PREFLIGHT'),
    ('capture_preflight_failed','request_guard',0,'CAPTURE_REQUEST_GUARD'),
    ('capture_transport_failed','transport',1,'CAPTURE_TRANSPORT'),
    ('capture_retention_unconfirmed','raw_retention',1,'CAPTURE_RAW_RETENTION'),
    ('capture_evidence_invalid','evidence',1,'CAPTURE_EVIDENCE'),
    ('capture_retention_unconfirmed','envelope_retention',1,'CAPTURE_ENVELOPE_RETENTION'),
    ('SECRET','transport',1,'UNSPECIFIED'),
    ('capture_transport_failed','SECRET',1,'UNSPECIFIED'),
    ('capture_transport_failed','raw_retention',1,'UNSPECIFIED'),
    ('capture_transport_failed','transport',True,'UNSPECIFIED'),
    ('capture_transport_failed','transport',0,'UNSPECIFIED'),
])
def test_closed_pair_only(code,phase,count,expected):
    @D.guarded
    def fail():raise R.CaptureOnlyError(code,phase,count,'SECRET_RAW_SHA')
    result=output(fail)
    assert result['failure_detail']==expected
    assert 'SECRET' not in json.dumps(result)


def test_lookalike_and_terminal_failure_not_misreported():
    class Lookalike(RuntimeError):
        code='capture_transport_failed';phase='transport';requests_attempted=1
    @D.guarded
    def fake():raise Lookalike('SECRET')
    assert output(fake)['failure_detail']=='UNSPECIFIED'
    @D.guarded
    def terminal_failure():
        try:raise R.CaptureOnlyError('capture_transport_failed','transport',1)
        except Exception:raise OSError('SECRET terminal failure')
    assert output(terminal_failure)['failure_detail']=='UNSPECIFIED'


def test_real_terminal_failure_does_not_preserve_transport_label(tmp_path):
    _,repo,grant,calendar,paths,store,current,updater=setup(tmp_path)
    original=store.commit
    def commit(parent,files,**kwargs):
        if any(path.endswith('/terminal.json') for path in files):
            raise OSError('SECRET terminal failed')
        return original(parent,files,**kwargs)
    with store.lock(),patch.object(store,'commit',side_effect=commit),patch.object(R.collectors,'_http_raw',side_effect=TimeoutError('SECRET')) as http:
        result=output(lambda:updater.run_slot('nov10','100-1',capture=D.provider_capture('synthetic-key',access())))
    assert http.call_count==1
    assert result['failure_detail']=='UNSPECIFIED'
    assert 'SECRET' not in json.dumps(result)


def test_malformed_fields_and_nested_guard():
    @D.guarded
    @D.guarded
    def nested():raise R.CaptureOnlyError('capture_transport_failed','transport',1,'SECRET')
    assert output(nested)['failure_detail']=='CAPTURE_TRANSPORT'
    @D.guarded
    def malformed():raise R.CaptureOnlyError('capture_transport_failed','transport',[],'SECRET')
    assert output(malformed)['failure_detail']=='UNSPECIFIED'
