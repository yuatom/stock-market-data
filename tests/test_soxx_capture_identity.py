"""Explicit SOXX public profile identity; no alias equivalence or qualification."""
import json
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from raw_response_evidence import validate_request,build_envelope,EvidenceError
from test_raw_response_evidence import request,body,OBSERVED

class SoxxIdentityTests(unittest.TestCase):
    def test_explicit_soxx_xnms_profile_is_supported(self):
        req=dict(request(),symbol='SOXX',mic_code='XNMS')
        payload=body();payload['meta'].update(symbol='SOXX',mic_code='XNMS')
        result=json.loads(build_envelope(json.dumps(payload).encode(),req,OBSERVED))
        self.assertEqual(result['request']['mic_code'],'XNMS')
        self.assertFalse(result['claims']['market_qualified'])

    def test_xnms_is_not_an_alias_for_xnas_response(self):
        req=dict(request(),symbol='SOXX',mic_code='XNMS')
        payload=body();payload['meta'].update(symbol='SOXX',mic_code='XNAS')
        with self.assertRaises(EvidenceError):build_envelope(json.dumps(payload).encode(),req,OBSERVED)

    def test_profile_change_is_not_a_general_market_validator_broadening(self):
        for symbol in ('AAA','IWM','GLD'):
            with self.subTest(symbol=symbol),self.assertRaises(EvidenceError):
                validate_request(dict(request(),symbol=symbol,mic_code='XNMS'))
