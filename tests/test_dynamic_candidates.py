import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_dynamic_candidates as dynamic  # noqa: E402
import verify_store_publication as durability  # noqa: E402


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
            "personal_state_proof": {
                "dynamic_state_read_sha": "c" * 40,
                "blob_sha": "d" * 40,
                "state_version": 5,
                "authority_state": "canonical",
                "content_hash_status": "unavailable_no_script",
                "content_sha256": None,
            },
            "research_universe_resolution_status": "resolved",
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

    def test_production_dynamic_manifest_closes_against_actual_git_blobs_and_verifier(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            remote = root / "remote.git"
            writer = root / "writer"
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", str(writer)], check=True)
            subprocess.run(["git", "-C", str(writer), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(writer), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(writer), "remote", "add", "origin", str(remote)], check=True)

            store_root = writer / "data" / "market-data"
            seed = store_root / "collector-state" / "seed.json"
            seed.parent.mkdir(parents=True, exist_ok=True)
            seed.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(writer), "add", "data/market-data"], check=True)
            subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "seed"], check=True)
            subprocess.run(["git", "-C", str(writer), "branch", "-M", "main"], check=True)
            subprocess.run(["git", "-C", str(writer), "push", "-q", "-u", "origin", "main"], check=True)

            def fake_collect_regular_window(**kwargs):
                snapshot = kwargs["store_root"] / "snapshots" / "2026-08" / "dynamic-open30.json"
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text('{"snapshot":"ok"}\n', encoding="utf-8")
                return {
                    "status": "ok",
                    "snapshot_written": True,
                    "snapshot_path": str(snapshot.relative_to(kwargs["store_root"])),
                }

            request = self._request()
            with mock.patch.object(dynamic, "_ensure_daily_history", return_value=([], [])), mock.patch.object(
                dynamic, "collect_regular_window", side_effect=fake_collect_regular_window
            ):
                result = dynamic.collect_request(
                    request=request,
                    store_root=store_root,
                    store_config_path=ROOT / "config" / "market-data-store.yaml",
                    access_path=ROOT / "config" / "market-data-collector-access.yaml",
                    dynamic_config_path=ROOT / "config" / "dynamic-candidate-collection.yaml",
                )

            manifest_relative = result["publication_manifest_path"]
            manifest = json.loads((store_root / manifest_relative).read_text(encoding="utf-8"))
            self.assertEqual(
                {item["path"] for item in manifest["required_outputs"]},
                {result["result_state_path"], result["snapshot_path"]},
            )

            subprocess.run(["git", "-C", str(writer), "add", "data/market-data"], check=True)
            subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "dynamic outputs"], check=True)
            commit = subprocess.check_output(
                ["git", "-C", str(writer), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(["git", "-C", str(writer), "push", "-q", "origin", "HEAD:main"], check=True)

            for item in manifest["required_outputs"]:
                actual = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(writer),
                        "rev-parse",
                        f"{commit}:data/market-data/{item['path']}",
                    ],
                    text=True,
                ).strip()
                self.assertEqual(item["blob_sha"], actual)

            receipt = durability.verify_publication(
                writer,
                commit,
                required_manifest=f"data/market-data/{manifest_relative}",
            )
            self.assertEqual(receipt["status"], "durability_verified")
            verified = {row["path"]: row["blob_sha"] for row in receipt["verified_required_outputs"]}
            for item in manifest["required_outputs"]:
                self.assertEqual(verified[f"data/market-data/{item['path']}"], item["blob_sha"])

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
