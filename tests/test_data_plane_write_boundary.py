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

    def test_unproven_post_cutover_gates_remain_pending(self):
        migration = self.contract["migration"]
        self.assertIn("external_store_on_demand_request_path_passed", migration["pending_gates"])
        self.assertIn("first_natural_settlement_mature_append_observed", migration["pending_gates"])
        self.assertNotIn("external_store_on_demand_request_path_passed", migration["completed_gates"])


if __name__ == "__main__":
    unittest.main()
