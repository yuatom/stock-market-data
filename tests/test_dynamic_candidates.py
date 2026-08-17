import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_dynamic_candidates as dynamic  # noqa: E402


class DynamicCandidateRequestTests(unittest.TestCase):
    def _request(self):
        return {
            "schema_version": 1,
            "request_id": "discovery-20260814-open30-test",
            "requested_at": "2026-08-14T09:46:00-04:00",
            "trade_date": "2026-08-14",
            "stage": "open_30m",
            "request_purpose": "opportunity_discovery",
            "research_repository": "yuatom/stock-dairy",
            "research_repository_commit_sha": "a" * 40,
            "market_data_contract_sha": "b" * 40,
            "candidate_symbols": ["RDDT", "AMAT", "APP"],
            "transaction_id": "open30-test",
        }

    def _write(self, value):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with tmp:
            json.dump(value, tmp)
        return Path(tmp.name)

    def test_valid_bounded_request(self):
        path = self._write(self._request())
        value = dynamic._load_request(path, expected_contract_sha="b" * 40)
        self.assertEqual(value["candidate_symbols"], ["RDDT", "AMAT", "APP"])

    def test_open15_carryover_validation_is_allowed(self):
        value = self._request()
        value.update({"stage": "open_15m", "request_purpose": "carryover_validation", "candidate_symbols": ["RDDT"]})
        path = self._write(value)
        loaded = dynamic._load_request(path)
        self.assertEqual(loaded["stage"], "open_15m")
        self.assertEqual(loaded["request_purpose"], "carryover_validation")

    def test_open15_new_radar_is_rejected(self):
        value = self._request()
        value["stage"] = "open_15m"
        value["request_purpose"] = "opportunity_discovery"
        path = self._write(value)
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path)

    def test_open60_carryover_validation_is_allowed(self):
        value = self._request()
        value.update({"stage": "open_60m", "request_purpose": "carryover_validation", "candidate_symbols": ["RDDT", "UMAC", "RCAT"]})
        path = self._write(value)
        loaded = dynamic._load_request(path)
        self.assertEqual(loaded["stage"], "open_60m")
        self.assertEqual(loaded["request_purpose"], "carryover_validation")

    def test_open60_new_radar_is_rejected(self):
        value = self._request()
        value["stage"] = "open_60m"
        value["request_purpose"] = "opportunity_discovery"
        path = self._write(value)
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path)

    def test_more_than_eight_symbols_rejected(self):
        value = self._request()
        value["candidate_symbols"] = [f"A{i}" for i in range(9)]
        path = self._write(value)
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path)

    def test_non_equity_style_symbol_injection_field_rejected(self):
        value = self._request()
        value["asset_class"] = "crypto"
        path = self._write(value)
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path)

    def test_contract_sha_mismatch_rejected(self):
        path = self._write(self._request())
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path, expected_contract_sha="c" * 40)

    def test_unregistered_stage_is_rejected(self):
        value = self._request()
        value["stage"] = "premarket"
        path = self._write(value)
        with self.assertRaises(dynamic.DynamicCandidateCollectionError):
            dynamic._load_request(path)

    def test_dynamic_contract_keeps_fixed_universe_separate(self):
        text = (ROOT / "config" / "dynamic-candidate-collection.yaml").read_text(encoding="utf-8")
        self.assertIn("fixed_collection_universe_remains_default_baseline: true", text)
        self.assertIn("opportunity_qualification_forbidden: true", text)
        self.assertIn("open15_new_radar_forbidden: true", text)
        self.assertIn("open60_new_radar_forbidden: true", text)
        self.assertIn('open_60m_window: ["10:00", "10:30"]', text)
        self.assertIn("request_symbol_limit: 8", text)


if __name__ == "__main__":
    unittest.main()
