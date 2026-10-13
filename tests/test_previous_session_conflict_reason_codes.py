"""Regression coverage for typed EOD conflicts without auto-rebase or raw data disclosure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collectors as collector  # noqa: E402
from market_data_store import MarketDataConflict, append_daily_bars, read_daily_series  # noqa: E402


WATCHLIST = {"core_watchlist": ["AMD"], "instruments": {"AMD": {"asset_class": "stocks"}}}
COMPLETENESS = {
    "minute_matrix_requirements": {"tracked_benchmarks": []},
    "sector_close_capability": {"exact_sector_etfs": []},
}
CONFIG = {
    "collector": {
        "budgets": {
            "twelve_data_basic": {"hard_credits_per_day": 200, "hard_credits_per_minute": 8}
        },
        "twelve_data_daily": {"history_outputsize": 64, "bootstrap_minimum_records": 1},
    }
}
BARS = [{"trade_date": "2026-10-09", "close": 101.0}]


class PreviousSessionConflictReasonCodeTest(unittest.TestCase):
    def collect(self, root: Path, rows=BARS, **patches):
        with (
            mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fixture-only"}, clear=False),
            mock.patch.object(collector, "wait_for_twelve_budget", return_value=None),
            mock.patch.object(collector, "consume_twelve_credit", return_value=None),
            mock.patch.object(collector, "fetch_twelve_daily", return_value=rows),
        ):
            with (
                mock.patch.object(collector, "read_daily_series", **patches["read"])
                if "read" in patches else mock.patch.object(
                    collector, "read_daily_series", wraps=collector.read_daily_series
                )
            ), (
                mock.patch.object(collector, "append_daily_bars", **patches["append"])
                if "append" in patches else mock.patch.object(
                    collector, "append_daily_bars", wraps=collector.append_daily_bars
                )
            ):
                return collector.collect_previous_session_eod(
                    trade_date="2026-10-13",
                    store_root=root,
                    watchlist=WATCHLIST,
                    completeness=COMPLETENESS,
                    config=CONFIG,
                    access={},
                )

    def seed_existing(self, root: Path):
        append_daily_bars(
            root,
            provider=collector.TWELVE_PROVIDER,
            symbol="AMD",
            asset_class="stocks",
            bars=[{"trade_date": "2026-10-09", "close": 100.0}],
            series_semantics="daily_regular_ohlcv",
            adjustment_semantics="provider_reported",
        )

    def assert_failure(self, result, expected):
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["symbols_requested"], 1)
        self.assertEqual(result["changed_series"], 0)
        self.assertEqual(result["bootstrapped_series"], 0)
        self.assertEqual(result["failures"][0]["reason"], "MarketDataConflict")
        self.assertEqual(result["failures"][0]["reason_code"], expected)

    def test_provider_revision_has_typed_reason_and_does_not_mutate_store(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.seed_existing(root)
            before = read_daily_series(root, provider=collector.TWELVE_PROVIDER, symbol="AMD")
            result = self.collect(root)
            after = read_daily_series(root, provider=collector.TWELVE_PROVIDER, symbol="AMD")
            self.assertEqual(before, after)
        self.assert_failure(result, "historical_provider_revision")

    def test_persisted_series_read_conflict_is_not_misdiagnosed_as_provider_revision(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.seed_existing(root)
            result = self.collect(
                root, read={"side_effect": MarketDataConflict("private shard detail")}
            )
        self.assert_failure(result, "stored_series_integrity_conflict")

    def test_append_stage_conflict_does_not_auto_rebase(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = self.collect(
                root, append={"side_effect": MarketDataConflict("private append detail")}
            )
            self.assertFalse((root / "series").exists())
        self.assert_failure(result, "immutable_append_conflict")

    def test_unclassified_conflict_and_nonconflict_keep_truthful_types(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(
                collector, "fetch_twelve_daily",
                side_effect=MarketDataConflict("unexpected fetch-phase conflict"),
            ):
                with (
                    mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fixture-only"}),
                    mock.patch.object(collector, "wait_for_twelve_budget", return_value=None),
                    mock.patch.object(collector, "consume_twelve_credit", return_value=None),
                ):
                    result = collector.collect_previous_session_eod(
                        trade_date="2026-10-13", store_root=root,
                        watchlist=WATCHLIST, completeness=COMPLETENESS,
                        config=CONFIG, access={},
                    )
            self.assert_failure(result, "unclassified_market_data_conflict")
            with mock.patch.object(
                collector, "fetch_twelve_daily", side_effect=RuntimeError("private provider error")
            ):
                with (
                    mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fixture-only"}),
                    mock.patch.object(collector, "wait_for_twelve_budget", return_value=None),
                    mock.patch.object(collector, "consume_twelve_credit", return_value=None),
                ):
                    other = collector.collect_previous_session_eod(
                        trade_date="2026-10-13", store_root=root,
                        watchlist=WATCHLIST, completeness=COMPLETENESS,
                        config=CONFIG, access={},
                    )
        self.assertEqual(other["failures"][0]["reason"], "RuntimeError")
        self.assertNotIn("reason_code", other["failures"][0])

    def test_equal_duplicate_remains_noop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.seed_existing(root)
            result = self.collect(root, rows=[{"trade_date": "2026-10-09", "close": 100.0}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["changed_series"], 0)
        self.assertEqual(result["failures"], [])

    def test_budget_failure_still_aborts_instead_of_becoming_partial(self):
        with tempfile.TemporaryDirectory() as d:
            with (
                mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fixture-only"}),
                mock.patch.object(
                    collector, "wait_for_twelve_budget",
                    side_effect=collector.BudgetExceeded("budget limit"),
                ),
            ):
                with self.assertRaises(collector.BudgetExceeded):
                    collector.collect_previous_session_eod(
                        trade_date="2026-10-13", store_root=Path(d),
                        watchlist=WATCHLIST, completeness=COMPLETENESS,
                        config=CONFIG, access={},
                    )


class WorkflowSafeSummaryTest(unittest.TestCase):
    def run_summary(self, result):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text()
        )
        step = next(
            s for s in workflow["jobs"]["collect"]["steps"]
            if s["name"] == "Collect qualified facts into private Store checkout"
        )
        script = step["run"].split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "result.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            source = "Path('/tmp/collector-output.txt')"
            self.assertIn(source, script)
            script = script.replace(source, f"Path({str(path)!r})")
            proc = subprocess.run(
                [sys.executable, "-c", script],
                check=False, capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, json.loads(proc.stdout)

    def test_typed_counts_are_allowlisted_and_private_details_are_not_logged(self):
        raw = {
            "mode": "previous_session_eod", "status": "partial",
            "failures": [
                {"symbol": "PRIVATE-SYMBOL", "reason": "MarketDataConflict",
                 "reason_code": "historical_provider_revision", "detail": "PRIVATE-VALUES"},
                {"symbol": "PRIVATE-SYMBOL", "reason": "MarketDataConflict",
                 "reason_code": "stored_series_integrity_conflict", "detail": "PRIVATE-VALUES"},
                {"symbol": "PRIVATE-SYMBOL", "reason": "MarketDataConflict",
                 "reason_code": "historical_provider_revision", "detail": "PRIVATE-VALUES"},
                {"symbol": "PRIVATE-SYMBOL", "reason": "MarketDataConflict",
                 "reason_code": "PRIVATE-UNTRUSTED", "detail": "PRIVATE-VALUES"},
            ],
        }
        text, summary = self.run_summary(raw)
        self.assertEqual(
            summary["failure_reason_code_counts"],
            {"historical_provider_revision": 2, "stored_series_integrity_conflict": 1},
        )
        self.assertEqual(summary["failure_count"], 4)
        self.assertEqual(summary["failure_classes"], ["MarketDataConflict"])
        for private in ("PRIVATE-SYMBOL", "PRIVATE-VALUES", "PRIVATE-UNTRUSTED"):
            self.assertNotIn(private, text)

    def test_other_stage_and_success_do_not_inherit_eod_conflict_counts(self):
        for mode, failures in (
            ("open_30m", [{"reason_code": "immutable_append_conflict", "reason": "MarketDataConflict"}]),
            ("previous_session_eod", []),
        ):
            with self.subTest(mode=mode, failures=failures):
                _raw, summary = self.run_summary(
                    {"mode": mode, "status": "ok", "failures": failures}
                )
                self.assertNotIn("failure_reason_code_counts", summary)


if __name__ == "__main__":
    unittest.main()
