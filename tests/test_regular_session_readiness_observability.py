from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collection as collection

ET = ZoneInfo("America/New_York")


class RegularSessionReadinessObservabilityTest(unittest.TestCase):
    @staticmethod
    def _fact(minute: str, *, second: int = 0) -> dict[str, object]:
        return {
            "symbol": "AAA",
            "asset_class": "stocks",
            "session": "regular",
            "event_time": f"2026-09-10T10:{minute}:{second:02d}-04:00",
            "source_timestamp": f"2026-09-10T10:{minute}:{second:02d}-04:00",
            "last_sale": 100.0,
            "reported_volume": 1.0,
        }

    def _run_real_collection(self, facts: list[dict[str, object]]) -> tuple[dict[str, object], dict[str, object]]:
        access = {"nasdaq_public_intraday": {"max_workers": 1}}
        universe = {"daily_series": [], "context_proxies": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            collection,
            "fetch_nasdaq_regular",
            return_value=(facts, {"provider": collection.NASDAQ, "raw_row_count": len(facts), "timestamp_parseable_count": len(facts), "trade_date_match_count": len(facts), "window_row_count": len(facts), "target_window_match_count": len(facts)}),
        ):
            root = Path(tmp)
            result = collection.collect_regular_window(
                mode="close",
                stage="close",
                trade_date="2026-09-10",
                start_et="10:00",
                end_et="10:03",
                store_root=root,
                universe_config=universe,
                config={"collector": {"budgets": {"twelve_data_basic": {"hard_credits_per_day": 100, "hard_credits_per_minute": 10}}}},
                access=access,
                eligible_universe=[("AAA", "stocks")],
            )
            # Copy the temporary store before its context closes so the test
            # can inspect the exact persisted snapshot.
            snapshot_path = root / str(result["snapshot_path"])
            persisted = json.loads(snapshot_path.read_text(encoding="utf-8"))
            return persisted, result

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
        self.assertIsNone(readiness["snapshot_ready_at"])
        self.assertIsNone(readiness["window_end_to_snapshot_ready_seconds"])

    def test_real_path_sparse_qualified_symbol_is_partial(self) -> None:
        persisted, result = self._run_real_collection([self._fact("00"), self._fact("01")])
        coverage = result["coverage"]["increment"]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(coverage["requested_symbol_count"], 1)
        self.assertEqual(coverage["capture_object_count"], 1)
        self.assertEqual(coverage["qualified_symbol_count"], 1)
        self.assertEqual(coverage["full_window_symbol_count"], 0)
        self.assertEqual(coverage["terminal_observation_symbol_count"], 0)
        self.assertEqual(coverage["missing_symbols"], [])
        self.assertEqual(coverage["partial_symbols"], ["AAA"])
        self.assertFalse(coverage["complete"])
        self.assertEqual(persisted["generated_at"], result["collection_timing"]["snapshot_generated_at"])

    def test_real_path_full_window_can_still_lack_exact_terminal_observation(self) -> None:
        persisted, result = self._run_real_collection(
            [self._fact("00"), self._fact("01"), self._fact("02", second=30)]
        )
        coverage = result["coverage"]["increment"]
        self.assertEqual(coverage["qualified_symbol_count"], 1)
        self.assertEqual(coverage["full_window_symbol_count"], 1)
        self.assertEqual(coverage["terminal_observation_symbol_count"], 0)
        self.assertEqual(coverage["partial_symbols"], ["AAA"])
        self.assertFalse(coverage["complete"])
        self.assertEqual(persisted["coverage"]["increment"]["full_window_symbol_count"], 1)

    def test_real_path_fully_complete_has_all_machine_states(self) -> None:
        _persisted, result = self._run_real_collection(
            [self._fact("00"), self._fact("01"), self._fact("02")]
        )
        coverage = result["coverage"]["increment"]
        self.assertEqual(coverage["missing_symbols"], [])
        self.assertEqual(coverage["partial_symbols"], [])
        self.assertTrue(coverage["complete"])
        self.assertLessEqual(coverage["full_window_symbol_count"], coverage["qualified_symbol_count"])
        self.assertLessEqual(coverage["qualified_symbol_count"], coverage["requested_symbol_count"])
        self.assertLessEqual(coverage["terminal_observation_symbol_count"], coverage["qualified_symbol_count"])

    def test_real_path_reason_classes_are_machine_owned(self) -> None:
        access = {"nasdaq_public_intraday": {"max_workers": 1}}
        universe = {"daily_series": [], "context_proxies": {}}
        reasons = {
            "AAA": "success",
            "BBB": "no_rows",
            "CCC": "outside_window",
            "DDD": "transport_error",
        }

        def fake_nasdaq(symbol, *_args, **_kwargs):
            if symbol == "AAA":
                return [self._fact("00")], {"provider": collection.NASDAQ, "raw_row_count": 1, "timestamp_parseable_count": 1, "trade_date_match_count": 1, "window_row_count": 1, "target_window_match_count": 1}
            if symbol == "BBB":
                return [], {"provider": collection.NASDAQ, "raw_row_count": 0, "timestamp_parseable_count": 0, "trade_date_match_count": 0, "window_row_count": 0, "target_window_match_count": 0}
            if symbol == "CCC":
                return [], {"provider": collection.NASDAQ, "raw_row_count": 3, "timestamp_parseable_count": 3, "trade_date_match_count": 3, "window_row_count": 0, "target_window_match_count": 0}
            raise RuntimeError("connection refused")

        def fake_twelve(symbol, *_args, **_kwargs):
            return [], {"provider": collection.TWELVE, "raw_row_count": 0, "timestamp_parseable_count": 0, "trade_date_match_count": 0, "window_row_count": 0, "target_window_match_count": 0}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(collection, "fetch_nasdaq_regular", side_effect=fake_nasdaq), mock.patch.object(collection, "fetch_twelve_regular", side_effect=fake_twelve):
            result = collection.collect_regular_window(
                mode="close",
                stage="close",
                trade_date="2026-09-10",
                start_et="10:00",
                end_et="10:01",
                store_root=Path(tmp),
                universe_config=universe,
                config={},
                access=access,
                eligible_universe=[(symbol, "stocks") for symbol in reasons],
            )
        for symbol, expected in reasons.items():
            self.assertIn(expected, result["reason_classes"][symbol])
            self.assertIn(expected, result["coverage"]["reason_classes"][symbol])

    def test_coverage_arithmetic_fails_closed(self) -> None:
        with self.assertRaises(collection.CoverageContractError):
            collection._scope_coverage(
                requested_symbols=["AAA"],
                capture_refs=[{"path": "capture", "blob_sha": "0" * 40}],
                facts_by_symbol={"AAA": [self._fact("00")]},
                missing_hint=["AAA"],
                trade_date="2026-09-10",
                target_window={"start": "10:00", "end": "10:01"},
            )

    def test_reason_class_mapping_preserves_provider_taxonomy(self) -> None:
        self.assertEqual(
            collection._reason_from_diagnostic(
                {"raw_row_count": 2, "timestamp_parseable_count": 2, "trade_date_match_count": 2, "window_row_count": 2, "target_window_match_count": 0}
            ),
            "qualification_rejected",
        )
        self.assertEqual(collection._reason_from_exception(RuntimeError("HTTP 429")), "rate_limited")
        self.assertEqual(collection._reason_from_exception(RuntimeError("credit budget exhausted")), "budget_exhausted")

    def test_real_path_records_generated_and_local_ready_timing(self) -> None:
        persisted, result = self._run_real_collection(
            [self._fact("00"), self._fact("01"), self._fact("02")]
        )
        timing = result["collection_timing"]
        self.assertNotEqual(persisted["generated_at"], timing["collection_started_at"])
        observed = [
            datetime.fromisoformat(timing[field])
            for field in ("collection_started_at", "collection_completed_at", "snapshot_generated_at", "snapshot_ready_at")
        ]
        self.assertLessEqual(observed[0], observed[1])
        self.assertLessEqual(observed[1], observed[2])
        self.assertLessEqual(observed[2], observed[3])
        self.assertIsInstance(timing["window_end_to_snapshot_ready_seconds"], float)
        self.assertIn("not_github_queue_or_remote_store_commit_ready", timing["semantics"])
        self.assertNotIn("remote_store_commit_ready", timing)
        self.assertNotIn("github_queue_ready", timing)

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
