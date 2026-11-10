import hashlib
import http.client
import io
import json
import sys
import unittest
from datetime import datetime,timezone
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs,urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import market_data_collectors as c
import rebase_market_data_baseline as r
from raw_response_evidence import verify_envelope
from test_raw_response_evidence import request,raw


def access():
    return {'http':{'timeout_seconds':20,'user_agent':'synthetic-capture'},
            'twelve_data_basic':{'base_url':'https://api.twelvedata.com','time_series_path':'/time_series','interval_daily':'1day'}}


def clock():return datetime(2026,11,10,3,0,tzinfo=timezone.utc)


class CaptureOnlyTests(unittest.TestCase):
    def call(self,**changes):
        args=dict(request=request(),api_key='synthetic-key',access=access(),
                  before_request=lambda:None,retain_raw=lambda b:hashlib.sha256(b).hexdigest(),
                  retain_envelope=lambda b:hashlib.sha256(b).hexdigest(),clock=clock)
        args.update(changes)
        return r.capture_daily_response(**args)

    def test_one_request_pins_parameters_and_retains_before_envelope(self):
        events=[];bodies=[]
        def transport(url,**kwargs):
            events.append('transport')
            self.assertEqual(parse_qs(urlsplit(url).query),{
                'symbol':['AAA'],'interval':['1day'],'exchange':['NASDAQ'],'mic_code':['XNAS'],
                'type':['ETF'],'adjust':['splits'],'dp':['-1'],'start_date':['2026-11-02'],
                'end_date':['2026-11-03'],'outputsize':['5000'],'order':['asc'],'format':['JSON'],
                'apikey':['synthetic-key']})
            self.assertEqual(kwargs['maximum_bytes'],4194304)
            return raw()
        def retain_raw(b):events.append('raw');self.assertEqual(b,raw());return hashlib.sha256(b).hexdigest()
        def retain_envelope(b):events.append('envelope');bodies.append(b);return hashlib.sha256(b).hexdigest()
        with (mock.patch.object(c,'_http_raw',side_effect=transport), mock.patch.object(c,'_http_json',side_effect=AssertionError('lossy path forbidden')),
             mock.patch.object(r,'_validated_replacement_bar',side_effect=AssertionError('normalization forbidden')),
             mock.patch.object(r,'validate_replacement',side_effect=AssertionError('qualification forbidden')),
             mock.patch.object(r,'_replace_series',side_effect=AssertionError('Store write forbidden'))):
            result=self.call(before_request=lambda:events.append('guard'),retain_raw=retain_raw,retain_envelope=retain_envelope)
        self.assertEqual(events,['guard','transport','raw','envelope'])
        self.assertEqual(result['status'],'CAPTURE_RETAINED_UNQUALIFIED')
        self.assertFalse(result['market_fact_authority'])
        self.assertFalse(result['production_recovery_accepted'])
        self.assertEqual(result['requests_attempted'],1)
        envelope=verify_envelope(bodies[0],result['envelope_sha256'])
        self.assertEqual(envelope['raw_response'],raw())
        self.assertNotIn('synthetic-key',json.dumps(result))

    def test_preflight_failures_never_reach_transport(self):
        changes=[{'before_request':None},{'retain_raw':None},{'retain_envelope':None},{'api_key':''}]
        q=request();del q['dp'];changes.append({'request':q})
        a=access();a['twelve_data_basic']['base_url']='https://evil.invalid';changes.append({'access':a})
        a=access();a['twelve_data_basic']['time_series_path']='/time_series?apikey=leak';changes.append({'access':a})
        with mock.patch.object(c,'_http_raw',side_effect=AssertionError('transport forbidden')):
            for change in changes:
                with self.subTest(change=change),self.assertRaises(r.CaptureOnlyError):self.call(**change)

    def test_guard_failure_prevents_request(self):
        with mock.patch.object(c,'_http_raw') as transport:
            with self.assertRaises(r.CaptureOnlyError) as raised:
                self.call(before_request=lambda:(_ for _ in ()).throw(RuntimeError('synthetic-secret')))
            self.assertFalse(raised.exception.requests_attempted)
            self.assertNotIn('synthetic-secret',str(raised.exception))
            self.assertIsNone(raised.exception.__context__)
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(transport.call_count,0)

    def test_malformed_response_is_retained_before_parse_failure(self):
        captured=[]
        def retain(b):captured.append(b);return hashlib.sha256(b).hexdigest()
        with mock.patch.object(c,'_http_raw',return_value=b'{broken') as transport:
            with self.assertRaises(r.CaptureOnlyError) as raised:self.call(retain_raw=retain)
        self.assertEqual(captured,[b'{broken'])
        self.assertEqual(transport.call_count,1)
        self.assertEqual(raised.exception.code,'capture_evidence_invalid')
        self.assertEqual(raised.exception.raw_sha256,hashlib.sha256(b'{broken').hexdigest())

    def test_transport_failure_is_sanitized_and_not_retried(self):
        with mock.patch.object(c,'_http_raw',side_effect=RuntimeError('https://url?apikey=synthetic-key')) as transport:
            with self.assertRaises(r.CaptureOnlyError) as raised:self.call()
        self.assertEqual(transport.call_count,1)
        self.assertEqual(raised.exception.code,'capture_transport_failed')
        self.assertNotIn('synthetic-key',str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_uncertain_raw_retention_stops_without_envelope_or_retry(self):
        for callback in [lambda b:None,lambda b:'0'*64,lambda b:(_ for _ in ()).throw(RuntimeError('lost ack'))]:
            envelope=mock.Mock()
            with mock.patch.object(c,'_http_raw',return_value=raw()) as transport:
                with self.assertRaises(r.CaptureOnlyError) as raised:self.call(retain_raw=callback,retain_envelope=envelope)
            self.assertEqual(transport.call_count,1)
            self.assertEqual(envelope.call_count,0)
            self.assertEqual(raised.exception.code,'capture_retention_unconfirmed')
            self.assertEqual(raised.exception.phase,'raw_retention')

    def test_envelope_retention_failure_is_unknown_and_never_retries(self):
        with mock.patch.object(c,'_http_raw',return_value=raw()) as transport:
            with self.assertRaises(r.CaptureOnlyError) as raised:self.call(retain_envelope=lambda b:None)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(raised.exception.code,'capture_retention_unconfirmed')
        self.assertEqual(raised.exception.phase,'envelope_retention')


class RawTransportTests(unittest.TestCase):
    def response(self,payload,content_type='application/json',status=200):
        response=mock.MagicMock();response.status=status;response.headers=Message()
        response.headers['content-type']=content_type
        stream=io.BytesIO(payload);response.read.side_effect=stream.read;response.__enter__.return_value=response
        return response

    def framed_response(self,payload,headers):
        wire=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'+headers+b'\r\n'+payload
        class SocketFixture:
            def makefile(self,*args,**kwargs):return io.BytesIO(wire)
        response=http.client.HTTPResponse(SocketFixture())
        response.begin()
        return response

    def test_advertised_body_truncation_never_becomes_retained_capture(self):
        response=self.framed_response(raw(),f'Content-Length: {len(raw())+100}\r\n'.encode())
        opener=mock.Mock();opener.open.return_value=response
        retained=[]
        def retain(payload):retained.append(payload);return hashlib.sha256(payload).hexdigest()
        with mock.patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(r.CaptureOnlyError) as raised:
                r.capture_daily_response(request=request(),api_key='synthetic-key',access=access(),
                    before_request=lambda:None,retain_raw=retain,retain_envelope=retain,clock=clock)
        self.assertEqual(raised.exception.code,'capture_transport_failed')
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(retained,[])
        self.assertEqual(opener.open.call_count,1)

    def test_ambiguous_or_invalid_framing_headers_are_rejected(self):
        cases=[b'Content-Length: -1\r\n',b'Content-Length: +415\r\n',
               b'Content-Length: invalid\r\n',b'Content-Length: 415, 415\r\n',
               b'Content-Length: 415\r\nContent-Length: 515\r\n',
               b'Content-Length: 415\r\nContent-Length: 415\r\n',
               b'Content-Length: 415\r\nTransfer-Encoding: chunked\r\n',
               b'Transfer-Encoding: unsupported\r\n',b'Transfer-Encoding: chunked \r\n',
               b'Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n']
        for headers in cases:
            with self.subTest(headers=headers):
                response=self.framed_response(raw(),headers)
                opener=mock.Mock();opener.open.return_value=response
                with mock.patch('urllib.request.build_opener',return_value=opener),self.assertRaises(RuntimeError):
                    c._http_raw('https://api.twelvedata.com/time_series',timeout=20,user_agent='test',maximum_bytes=4194304)

    def test_complete_supported_framing_preserves_exact_body(self):
        payload=raw()
        chunked=f'{len(payload):x}\r\n'.encode()+payload+b'\r\n0\r\n\r\n'
        for headers,body in [(f'Content-Length: {len(payload)}\r\n'.encode(),payload),
                             (b'',payload),(b'Transfer-Encoding: chunked\r\n',chunked)]:
            with self.subTest(headers=headers):
                response=self.framed_response(body,headers)
                opener=mock.Mock();opener.open.return_value=response
                with mock.patch('urllib.request.build_opener',return_value=opener):
                    result=c._http_raw('https://api.twelvedata.com/time_series',timeout=20,user_agent='test',maximum_bytes=len(payload))
                self.assertEqual(result,payload)

    def test_truncated_chunked_body_is_rejected(self):
        payload=raw()
        for body in [f'{len(payload)+100:x}\r\n'.encode()+payload,
                     f'{len(payload):x}\r\n'.encode()+payload+b'\r\n']:
            with self.subTest(body=body):
                response=self.framed_response(body,b'Transfer-Encoding: chunked\r\n')
                opener=mock.Mock();opener.open.return_value=response
                with mock.patch('urllib.request.build_opener',return_value=opener),self.assertRaises(http.client.IncompleteRead):
                    c._http_raw('https://api.twelvedata.com/time_series',timeout=20,user_agent='test',maximum_bytes=4194304)

    def test_bounded_raw_http_preserves_bytes_and_disables_redirects(self):
        response=self.response(raw())
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch('urllib.request.build_opener',return_value=opener) as create:
            result=c._http_raw('https://api.twelvedata.com/time_series?apikey=synthetic',timeout=20,user_agent='test',maximum_bytes=4194304)
        self.assertEqual(result,raw())
        self.assertEqual(opener.open.call_count,1)
        self.assertIsNone(create.call_args.args[0].redirect_request(None,None,302,'redirect',{},'https://evil.invalid/'))
        response.read.assert_called_once_with(4194305)

    def test_oversize_body_fails_before_candidate_processing(self):
        response=self.response(b'12345')
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch('urllib.request.build_opener',return_value=opener),self.assertRaises(RuntimeError):
            c._http_raw('https://api.twelvedata.com/time_series',timeout=20,user_agent='test',maximum_bytes=4)

    def test_existing_json_path_keeps_legacy_semantics(self):
        response=self.response(b'{"number":1.25}')
        with mock.patch('urllib.request.urlopen',return_value=response):
            self.assertEqual(c._http_json('https://example.invalid',timeout=1,user_agent='test'),{'number':1.25})

if __name__=='__main__':unittest.main()
