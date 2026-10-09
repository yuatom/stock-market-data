from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class LivePremarketContractAlignmentTest(unittest.TestCase):
    def test_store_contract_matches_live_runtime(self) -> None:
        store = yaml.safe_load((ROOT / "config/market-data-store.yaml").read_text(encoding="utf-8"))
        collector = store["collector"]
        self.assertIn("premarket", collector["on_demand_request"]["accepted_modes"])
        self.assertTrue(collector["scheduling"]["live_premarket_scheduled"])
        self.assertEqual(collector["scheduling"]["live_premarket_mode"], "premarket")
        self.assertEqual(collector["schedules_et"]["premarket_shadow_probe"], "07:10")
        self.assertEqual(collector["schedules_et"]["premarket"], "07:20")

        workflow = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("'20 7 * * 1-5': 'premarket'", workflow)
        self.assertIn("{'premarket','open_15m','open_30m','open_60m','close'}", workflow)

    def test_retry_contract_is_bounded_and_meter_aware(self) -> None:
        access = yaml.safe_load((ROOT / "config/market-data-collector-access.yaml").read_text(encoding="utf-8"))
        retry = access["http"]["transient_retry"]
        self.assertEqual(retry["max_attempts"], 3)
        self.assertEqual(retry["backoff_seconds"], [1, 3])
        self.assertFalse(retry["http_error_retry"])
        self.assertFalse(retry["application_error_retry"])
        self.assertTrue(retry["metered_provider_failed_transport_attempt_must_consume_credit"])


if __name__ == "__main__":
    unittest.main()
