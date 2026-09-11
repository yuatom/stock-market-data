from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collection as collection

ET = ZoneInfo("America/New_York")


class RegularSessionReadinessObservabilityTest(unittest.TestCase):
    def test_readiness_is_collector_observed_and_measured_from_target_window_end(self) -> None:
        readiness = collection._readiness_observability(
            trade_date="2026-09-10",
            end_et="10:00",
            collection_started_at="2026-09-10T10:02:30-04:00",
            collection_completed_at="2026-09-10T10:04:15-04:00",
        )
        self.assertEqual(
            readiness["semantics"],
            "collector_observed_after_prewarm_not_github_queue_or_remote_store_commit_ready",
        )
        self.assertEqual(readiness["target_window_end"], "2026-09-10T10:00:00-04:00")
        self.assertEqual(readiness["collection_started_after_target_seconds"], 150.0)
        self.assertEqual(readiness["collection_completed_after_target_seconds"], 255.0)

    def test_increment_complete_can_coexist_with_incomplete_cumulative_stage_snapshot(self) -> None:
        full_universe = [("AAA", "stocks"), ("BBB", "stocks"), ("CCC", "stocks")]
        increment_universe = [("CCC", "stocks")]
        coverage = collection._coverage_observability(
            full_universe=full_universe,
            increment_universe=increment_universe,
            capture_refs=[{"path": "capture-ccc"}],
            missing=[],
            snapshot_missing=["BBB"],
            prior_refs=[{"path": "capture-aaa"}],
            provider_counts={"nasdaq_public_intraday": 1},
            readiness={
                "semantics": "collector_observed_after_prewarm_not_github_queue_or_remote_store_commit_ready",
                "target_window_end": "2026-09-10T16:00:00-04:00",
                "collection_started_at": "2026-09-10T16:01:00-04:00",
                "collection_completed_at": "2026-09-10T16:02:00-04:00",
                "collection_started_after_target_seconds": 60.0,
                "collection_completed_after_target_seconds": 120.0,
            },
        )
        self.assertTrue(coverage["increment"]["complete"])
        self.assertEqual(coverage["increment"]["symbols_missing"], 0)
        self.assertFalse(coverage["stage_snapshot"]["complete"])
        self.assertEqual(coverage["stage_snapshot"]["symbols_required"], 3)
        self.assertEqual(coverage["stage_snapshot"]["symbols_available"], 2)
        self.assertEqual(coverage["stage_snapshot"]["missing_symbols"], ["BBB"])
        self.assertEqual(coverage["symbols_missing_in_increment"], 0)

    def test_full_stage_complete_is_distinct_from_reference_count(self) -> None:
        coverage = collection._coverage_observability(
            full_universe=[("AAA", "stocks"), ("BBB", "stocks")],
            increment_universe=[("AAA", "stocks"), ("BBB", "stocks")],
            capture_refs=[{"path": "a"}, {"path": "b"}],
            missing=[],
            snapshot_missing=[],
            prior_refs=[{"path": "prior-a"}, {"path": "prior-b"}],
            provider_counts={"nasdaq_public_intraday": 2},
            readiness={
                "semantics": "collector_observed_after_prewarm_not_github_queue_or_remote_store_commit_ready",
                "target_window_end": "2026-09-10T10:00:00-04:00",
                "collection_started_at": "2026-09-10T10:00:10-04:00",
                "collection_completed_at": "2026-09-10T10:00:20-04:00",
                "collection_started_after_target_seconds": 10.0,
                "collection_completed_after_target_seconds": 20.0,
            },
        )
        self.assertTrue(coverage["stage_snapshot"]["complete"])
        self.assertEqual(coverage["stage_snapshot"]["symbols_available"], 2)
        self.assertEqual(coverage["total_ref_count"], 4)
        self.assertNotEqual(coverage["stage_snapshot"]["symbols_available"], coverage["total_ref_count"])

    def test_readiness_does_not_claim_github_queue_or_remote_store_commit_ready(self) -> None:
        source = (ROOT / "scripts/market_data_collection.py").read_text(encoding="utf-8")
        self.assertIn("not_github_queue_or_remote_store_commit_ready", source)
        self.assertNotIn("github_queue_started_at", source)
        self.assertNotIn("remote_store_commit_ready_at", source)

    def test_existing_prewarm_and_provider_policy_are_not_changed_by_observability_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("cron: '40 9 * * 1-5'", workflow)
        self.assertIn("cron: '55 9 * * 1-5'", workflow)
        self.assertIn("cron: '25 10 * * 1-5'", workflow)
        self.assertIn("timeout-minutes: 12", runtime)


if __name__ == "__main__":
    unittest.main()
