import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PremarketReplayControlContractTest(unittest.TestCase):
    def test_data_plane_exposes_exact_source_replay_mode(self):
        config = yaml.safe_load((ROOT / "config/data-plane.yaml").read_text(encoding="utf-8"))
        maintenance = config["maintenance_request_interface"]
        self.assertEqual(
            maintenance["allowed_modes"]["premarket_probe_replay"],
            "historical_premarket_probe_sample",
        )
        self.assertEqual(
            maintenance["premarket_probe_replay_requires"],
            ["probe_path", "probe_blob_sha"],
        )
        self.assertTrue(maintenance["premarket_probe_replay_sample_only"])
        self.assertTrue(maintenance["premarket_probe_replay_network_fetch_forbidden"])

    def test_maintenance_schema_requires_exact_probe_identity_for_replay(self):
        schema = json.loads(
            (ROOT / "schemas/market-data-maintenance-request.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("premarket_probe_replay", schema["properties"]["mode"]["enum"])
        replay_clause = next(
            clause["then"]
            for clause in schema["allOf"]
            if clause["if"]["properties"]["mode"].get("const") == "premarket_probe_replay"
        )
        self.assertEqual(replay_clause["properties"]["scope"]["const"], "historical_premarket_probe_sample")
        self.assertEqual(set(replay_clause["required"]), {"probe_path", "probe_blob_sha"})
        self.assertIn("nasdaq-extended", schema["properties"]["probe_path"]["pattern"])

    def test_runtime_uses_existing_serialized_writer_and_rebuilds_inventory(self):
        runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("group: market-data-collector", runtime)
        self.assertIn("'premarket_probe_replay': 'historical_premarket_probe_sample'", runtime)
        self.assertIn("probe_blob_sha does not match private Store source", runtime)
        self.assertIn("python scripts/replay_premarket_probe.py", runtime)
        self.assertIn("--probe-path \"$PROBE_PATH\"", runtime)
        self.assertIn("python scripts/build_market_data_inventory.py", runtime)
        self.assertIn("git pull --rebase origin main", runtime)
        self.assertIn("git push origin HEAD:main", runtime)

    def test_replay_does_not_require_twelve_data_secret(self):
        runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn(
            "smoke_readonly|nasdaq_extended_probe|premarket|premarket_probe_replay",
            runtime,
        )


if __name__ == "__main__":
    unittest.main()
