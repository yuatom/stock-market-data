from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class DataPlaneWriteBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load((ROOT / "config/data-plane.yaml").read_text(encoding="utf-8"))

    def test_research_runtime_cannot_write_market_facts(self):
        principles = self.contract["principles"]
        self.assertTrue(principles["market_fact_writes_are_collector_or_explicit_data_maintenance_only"])
        self.assertTrue(principles["research_runtime_market_fact_write_forbidden"])
        self.assertTrue(principles["research_runtime_may_request_collector_but_must_not_bypass_it"])
        runtime = self.contract["payload_writers"]["stock_dairy_research_runtime"]
        self.assertEqual(runtime["executor"], "stock_dairy_chatgpt_runtime")
        self.assertFalse(runtime["market_fact_write_allowed"])
        self.assertEqual(runtime["allowed_control_write_only"], "collector_request_interface")

    def test_collector_remains_market_fact_writer(self):
        writer = self.contract["payload_writers"]["scheduled_and_on_demand_collector"]
        self.assertEqual(writer["executor"], "stock_market_data_github_actions")
        self.assertEqual(writer["allowed_repository"], "yuatom/stock-market-data-store")
        self.assertIn("session_capture", writer["allowed_write_kinds"])
        self.assertIn("stage_snapshot", writer["allowed_write_kinds"])

    def test_consumer_pins_one_read_sha_per_frozen_input(self):
        consumer = self.contract["consumer_interface"]
        self.assertTrue(consumer["one_market_data_read_sha_per_frozen_research_input"])
        self.assertTrue(consumer["consumer_must_not_refresh_read_sha_after_research_input_freeze"])
        self.assertTrue(consumer["store_miss_may_trigger_collector_request"])
        self.assertTrue(consumer["store_miss_must_not_authorize_research_runtime_market_fetch_or_write"])
        self.assertTrue(consumer["cutoff_valid_context_proxy_refs_are_first_class_market_data_refs"])
        self.assertTrue(consumer["proxy_semantics_must_survive_into_frozen_research_input"])

    def test_supported_context_baseline_is_data_plane_owned(self):
        baseline = self.contract["supported_context_baseline"]
        self.assertEqual(baseline["role"], "cutoff_valid_cross_asset_proxy")
        self.assertEqual(
            set(baseline["categories"]),
            {"rates", "volatility", "dollar", "commodities", "crypto"},
        )
        self.assertEqual(baseline["historical_repair_mode"], "historical_context_repair")
        self.assertTrue(baseline["provider_timestamp_must_match_target_trade_date"])
        self.assertTrue(baseline["target_window_timestamp_required"])
        self.assertTrue(self.contract["principles"]["context_proxy_must_not_be_relabelled_as_formal_underlying_metric"])
        self.assertTrue(self.contract["principles"]["unsupported_enrichment_must_not_be_fabricated_from_proxy_data"])

    def test_historical_maintenance_is_explicit_control_state(self):
        interface = self.contract["maintenance_request_interface"]
        self.assertEqual(interface["branch"], "maintenance-requests")
        self.assertEqual(interface["path"], "requests/market-data-maintenance.json")
        self.assertEqual(
            interface["allowed_modes"]["historical_context_repair"],
            "cutoff_valid_cross_asset_context_proxies_only",
        )
        self.assertTrue(interface["research_runtime_implicit_historical_repair_forbidden"])
        self.assertTrue(interface["request_is_control_state_not_market_fact"])


if __name__ == "__main__":
    unittest.main()
