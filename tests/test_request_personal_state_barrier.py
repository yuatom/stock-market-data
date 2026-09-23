from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "collect_dynamic_candidates",
    ROOT / "scripts" / "collect_dynamic_candidates.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _proof(hash_status: str = "unavailable_no_script") -> dict:
    return {
        "dynamic_state_read_sha": "a" * 40,
        "blob_sha": "b" * 40,
        "state_version": 5,
        "authority_state": "canonical",
        "content_hash_status": hash_status,
        "content_sha256": None if hash_status == "unavailable_no_script" else "c" * 64,
    }


def _request() -> dict:
    return {
        "schema_version": 1,
        "request_id": "req-1",
        "requested_at": "2026-09-22T10:00:00-04:00",
        "trade_date": "2026-09-22",
        "stage": "open_30m",
        "request_purpose": "opportunity_discovery",
        "research_repository": "yuatom/stock-dairy",
        "research_repository_commit_sha": "d" * 40,
        "market_data_contract_sha": "e" * 40,
        "candidate_symbols": ["NVDA"],
        "transaction_id": "open30-20260922-100214-et-01",
        "personal_state_proof": _proof(),
        "research_universe_resolution_status": "resolved",
    }


class RequestPersonalStateBarrierTest(unittest.TestCase):
    def _write(self, value: dict) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "request.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return tmp, path

    def test_dynamic_request_requires_personal_state_barrier(self):
        value = _request()
        value.pop("personal_state_proof")
        tmp, path = self._write(value)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(MODULE.DynamicCandidateCollectionError, "missing fields"):
            MODULE._load_request(path)

    def test_dynamic_request_accepts_no_script_personal_state_proof(self):
        tmp, path = self._write(_request())
        self.addCleanup(tmp.cleanup)
        value = MODULE._load_request(path, expected_contract_sha="e" * 40)
        self.assertEqual(value["personal_state_proof"]["content_hash_status"], "unavailable_no_script")
        self.assertIsNone(value["personal_state_proof"]["content_sha256"])

    def test_dynamic_request_rejects_fabricated_no_script_digest(self):
        value = _request()
        value["personal_state_proof"]["content_sha256"] = "f" * 64
        tmp, path = self._write(value)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(MODULE.DynamicCandidateCollectionError, "requires null content_sha256"):
            MODULE._load_request(path)

    def test_wire_schemas_require_personal_state_barrier(self):
        for relative in (
            "schemas/collector-request.schema.json",
            "schemas/dynamic-candidate-request.schema.json",
        ):
            schema = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            required = set(schema["required"])
            self.assertTrue({"transaction_id", "personal_state_proof", "research_universe_resolution_status"} <= required)
            self.assertEqual(schema["properties"]["research_universe_resolution_status"]["const"], "resolved")


if __name__ == "__main__":
    unittest.main()
