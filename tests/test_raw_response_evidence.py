import base64
import copy
import hashlib
import json
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from raw_response_evidence import EvidenceError, build_envelope, verify_envelope


def request():
    return dict(provider='twelve_data', symbol='AAA', interval='1day',
                exchange='NASDAQ', mic_code='XNAS', currency='USD', type='ETF',
                adjust='splits', dp=-1, start_date='2026-11-02', end_date='2026-11-03',
                outputsize=5000, order='asc')


def body():
    return dict(meta=dict(symbol='AAA', interval='1day', currency='USD',
                         exchange_timezone='America/New_York', exchange='NASDAQ',
                         mic_code='XNAS', type='ETF'),
                values=[dict(datetime='2026-11-02', open='33.3333300', high='34.00000',
                             low='32.00', close='33.33333333333', volume='3000')], status='ok')


def raw(data=None):
    return (json.dumps(body() if data is None else data, indent=2)+'\n\n').encode()


OBSERVED='2026-11-10T03:00:00+00:00'


class IntakeTests(unittest.TestCase):
    def test_preserves_exact_response_bytes_and_decimal_lexemes(self):
        source=raw(); before=request(); frozen=copy.deepcopy(before)
        encoded=build_envelope(source,before,OBSERVED)
        doc=json.loads(encoded)
        self.assertEqual(base64.b64decode(doc['raw_response_base64']),source)
        self.assertEqual(doc['records'][0]['open'],'33.3333300')
        self.assertEqual(doc['records'][0]['close'],'33.33333333333')
        self.assertEqual(doc['request']['dp'],-1)
        self.assertEqual(doc['raw_response_sha256'],hashlib.sha256(source).hexdigest())
        self.assertEqual(before,frozen)

    def test_envelope_never_claims_qualification_or_authenticated_capture(self):
        d=json.loads(build_envelope(raw(),request(),OBSERVED))
        self.assertEqual(d['status'],'OFFLINE_EVIDENCE_ONLY')
        for key in ('source_authenticated','observation_time_verified','market_qualified',
                    'rounding_verified','volume_semantics_verified','calendar_verified',
                    'production_activation'):
            self.assertIs(d['claims'][key],False)

    def test_explicit_dp_is_required_and_validated(self):
        for value in [None,True,-2,12,'11',1.5]:
            with self.subTest(value=value),self.assertRaises(EvidenceError):
                q=request();q['dp']=value;build_envelope(raw(),q,OBSERVED)
        q=request();del q['dp']
        with self.assertRaises(EvidenceError):build_envelope(raw(),q,OBSERVED)
        for value in [-1,0,11]:
            q=request();q['dp']=value
            self.assertEqual(json.loads(build_envelope(raw(),q,OBSERVED))['request']['dp'],value)

    def test_request_and_response_schemas_reject_extra_data(self):
        q=request();q['apikey']='synthetic-secret'
        with self.assertRaises(EvidenceError):build_envelope(raw(),q,OBSERVED)
        for path in ['root','meta','row']:
            b=body();target=b if path=='root' else b['meta'] if path=='meta' else b['values'][0]
            target['unexpected']='synthetic-secret'
            with self.subTest(path=path),self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)

    def test_duplicate_json_keys_are_rejected(self):
        data=raw().replace(b'"status": "ok"',b'"status": "error", "status": "ok"')
        with self.assertRaises(EvidenceError):build_envelope(data,request(),OBSERVED)

    def test_numeric_conversion_and_nondecimal_strings_are_rejected(self):
        for value in [33.3,True,None,'NaN','Infinity','-1','1e3',' 1.0','1.','9'*65]:
            b=body();b['values'][0]['open']=value
            with self.subTest(value=value),self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)

    def test_nonfinite_json_numbers_are_rejected(self):
        with self.assertRaises(EvidenceError):build_envelope(raw().replace(b'"3000"',b'NaN'),request(),OBSERVED)

    def test_identity_and_adjustment_mismatch_are_rejected(self):
        for key,value in [('symbol','BBB'),('mic_code','XNYS'),('interval','1min'),('currency','EUR')]:
            b=body();b['meta'][key]=value
            with self.subTest(key=key),self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)
        q=request();q['adjust']='none'
        with self.assertRaises(EvidenceError):build_envelope(raw(),q,OBSERVED)

    def test_dates_and_row_count_are_bounded_without_calendar_claim(self):
        for day in ['2026-11-04','2026-11-01','2026-11-02 00:00:00','2026-02-30']:
            b=body();b['values'][0]['datetime']=day
            with self.subTest(day=day),self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)
        b=body();b['values']*=2
        with self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)
        b=body();b['values']=[]
        with self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)
        q=request();q['outputsize']=0
        with self.assertRaises(EvidenceError):build_envelope(raw(),q,OBSERVED)

    def test_observation_timestamp_requires_timezone(self):
        for t in ['2026-11-10T03:00:00','2026-11-10T03:00:00+00:99','2026-11-10T03:00:00+01:60','yesterday',None]:
            with self.subTest(t=t),self.assertRaises(EvidenceError):build_envelope(raw(),request(),t)

    def test_input_size_and_encoding_are_rejected(self):
        for data in [b'',b'\xff',b' '*4_194_305,'not bytes',b'[]']:
            with self.subTest(kind=type(data)),self.assertRaises(EvidenceError):build_envelope(data,request(),OBSERVED)

    def test_error_response_is_not_captured_as_success(self):
        b=body();b['status']='error'
        with self.assertRaises(EvidenceError):build_envelope(raw(b),request(),OBSERVED)

    def test_independent_envelope_anchor_and_derived_fields_are_verified(self):
        encoded=build_envelope(raw(),request(),OBSERVED)
        digest=hashlib.sha256(encoded).hexdigest()
        self.assertEqual(verify_envelope(encoded,digest)['raw_response'],raw())
        with self.assertRaises(EvidenceError):verify_envelope(encoded,'0'*64)
        doc=json.loads(encoded);doc['records'][0]['open']='99'
        changed=(json.dumps(doc,sort_keys=True,separators=(',',':'))+'\n').encode()
        with self.assertRaises(EvidenceError):verify_envelope(changed,hashlib.sha256(changed).hexdigest())
        doc=json.loads(encoded);doc['claims']['market_qualified']=True
        changed=(json.dumps(doc,sort_keys=True,separators=(',',':'))+'\n').encode()
        with self.assertRaises(EvidenceError):verify_envelope(changed,hashlib.sha256(changed).hexdigest())

    def test_whitespace_change_changes_anchor_not_decimal_evidence(self):
        a=build_envelope(raw(),request(),OBSERVED)
        b=build_envelope(json.dumps(body()).encode(),request(),OBSERVED)
        self.assertNotEqual(hashlib.sha256(a).hexdigest(),hashlib.sha256(b).hexdigest())
        self.assertEqual(json.loads(a)['records'],json.loads(b)['records'])


if __name__=='__main__':unittest.main()
