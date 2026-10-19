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
from market_data_store import append_daily_bars, read_daily_series  # noqa: E402


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


def _access():
    return {
        "http": {"timeout_seconds": 2, "user_agent": "test"},
        "twelve_data_basic": {
            "base_url": "https://example.invalid",
            "time_series_path": "/time_series",
            "interval_daily": "1day",
        },
    }


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
                    access=_access(),
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
            archive = root / "recovery/2026-10-18/twelve_data_basic/daily-baseline-rebase/AMD.json"
            archive.parent.mkdir(parents=True)
            archive.write_text('{"archive":true}\n')
            archive_before = archive.read_bytes()
            before = read_daily_series(root, provider=collector.TWELVE_PROVIDER, symbol="AMD")
            result = self.collect(root)
            after = read_daily_series(root, provider=collector.TWELVE_PROVIDER, symbol="AMD")
            self.assertEqual(before, after)
            self.assertEqual(archive.read_bytes(), archive_before)
            persisted = json.loads((root / result["result_state_path"]).read_text())
            self.assertEqual(persisted["conflicts"][0]["fields"][0]["old_value"], 100.0)
            self.assertEqual(persisted["store_input_sha"], None)
        self.assert_failure(result, "historical_provider_revision")
        self.assertEqual(result["failure_target_count"], 1)
        self.assertEqual(result["conflict_record_count"], 1)
        detail = result["conflict_diagnostics"][0]
        self.assertEqual((detail["symbol"], detail["trade_date"]), ("AMD", "2026-10-09"))
        self.assertEqual(detail["fields"][0]["field"], "close")
        self.assertEqual((detail["fields"][0]["old_value"], detail["fields"][0]["new_value"]), (100.0, 101.0))
        self.assertEqual(detail["fields"][0]["old_type"], "float")
        self.assertEqual(detail["fields"][0]["new_type"], "float")
        self.assertEqual(len(detail["fields"][0]["old_sha256"]), 64)
        self.assertTrue(detail["store_source"]["shard"]["blob_sha"])
        self.assertEqual(detail["request_parameters"]["outputsize"], 64)
        self.assertEqual(detail["request_parameters"]["interval"], "1day")
        self.assertNotIn("api_key", detail["request_parameters"])
        self.assertTrue(detail["store_source"]["index_path"].endswith("/AMD/_index.json"))
        self.assertNotIn("apikey", json.dumps(detail["request_parameters"]).lower())

    def test_same_legal_rows_from_recovery_and_ordinary_eod_compare_identically(self):
        import rebase_market_data_baseline as recovery  # noqa: PLC0415

        payload = {"values": [{"datetime": "2026-10-09", "open": "99.0", "high": "102.0",
                               "low": "98.0", "close": "100.0", "volume": "12345"}]}
        access = _access()
        with mock.patch.object(collector, "_http_json", return_value=payload):
            ordinary_rows = collector.fetch_twelve_daily("AMD", "synthetic-key", access, outputsize=64)
            recovery_rows = recovery._fetch_replacement_daily(
                "AMD", "synthetic-key", access, outputsize=64,
                trade_date="2026-10-13", effective=None,
            )
        self.assertEqual(ordinary_rows, recovery_rows)
        replacement = recovery_rows[0]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            append_daily_bars(
                root,
                provider=collector.TWELVE_PROVIDER,
                symbol="AMD",
                asset_class="stocks",
                bars=[replacement],
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
            )
            result = self.collect(root, rows=ordinary_rows)
            self.assertEqual(read_daily_series(root, provider=collector.TWELVE_PROVIDER, symbol="AMD"), [replacement])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["conflict_record_count"], 0)
        self.assertEqual(result["changed_series"], 0)

    def test_conflict_diagnostic_is_bounded_with_explicit_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # One symbol can conflict on each retained date; exceed the explicit cap.
            old_rows = []
            new_rows = []
            from datetime import date, timedelta
            start = date(2025, 1, 1)
            for n in range(40):
                day = (start + timedelta(days=n)).isoformat()
                old_rows.append({"trade_date": day, "close": 100.0})
                new_rows.append({"trade_date": day, "close": 101.0})
            append_daily_bars(
                root,
                provider=collector.TWELVE_PROVIDER,
                symbol="AMD",
                asset_class="stocks",
                bars=old_rows,
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
            )
            result = self.collect(root, rows=new_rows)
        self.assertEqual(result["conflict_record_count"], 40)
        self.assertTrue(result["conflict_diagnostics_truncated"])
        self.assertEqual(len(result["conflict_diagnostics"]), collector.MAX_CONFLICT_DIAGNOSTIC_RECORDS)

    def test_missing_and_invalid_inputs_do_not_become_successful_noop(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.collect(Path(d), rows=[])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failure_target_count"], 1)
        self.assertEqual(result["conflict_record_count"], 0)

    def test_private_conflict_values_are_not_in_public_workflow_summary(self):
        workflow = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text()
        sanitizer = workflow.split("allowed = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("conflict_diagnostics", sanitizer)
        self.assertNotIn("old_value", sanitizer)
        self.assertNotIn("new_value", sanitizer)

    def test_persisted_series_read_conflict_is_not_misdiagnosed_as_provider_revision(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.seed_existing(root)
            result = self.collect(
                root, read={"side_effect": collector.MarketDataConflict("private shard detail")}
            )
        self.assert_failure(result, "stored_series_integrity_conflict")

    def test_append_stage_conflict_does_not_auto_rebase(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = self.collect(
                root, append={"side_effect": collector.MarketDataConflict("private append detail")}
            )
            self.assertFalse((root / "series").exists())
        self.assert_failure(result, "immutable_append_conflict")

    def test_unclassified_conflict_and_nonconflict_keep_truthful_types(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(
                collector, "fetch_twelve_daily",
                side_effect=collector.MarketDataConflict("unexpected fetch-phase conflict"),
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

    def test_numeric_type_change_matches_store_canonical_conflict_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            append_daily_bars(
                root,
                provider=collector.TWELVE_PROVIDER,
                symbol="AMD",
                asset_class="stocks",
                bars=[{"trade_date": "2026-10-09", "close": 100}],
                series_semantics="daily_regular_ohlcv",
                adjustment_semantics="provider_reported",
            )
            result = self.collect(root, rows=[{"trade_date": "2026-10-09", "close": 100.0}])
        self.assert_failure(result, "historical_provider_revision")
        self.assertEqual(result["conflict_diagnostics"][0]["fields"][0]["old_type"], "int")
        self.assertEqual(result["conflict_diagnostics"][0]["fields"][0]["new_type"], "float")

    def test_private_diagnostic_persist_path_and_identity_are_declared(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
            "MARKET_DATA_REQUEST_ID": "run-1",
            "MARKET_DATA_COMPUTE_SHA": "a" * 40,
            "MARKET_DATA_STORE_INPUT_SHA": "b" * 40,
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        }, clear=False):
            result = self.collect(Path(d), rows=[])
            path = Path(d) / result["result_state_path"]
            diagnostic = json.loads(path.read_text())
        self.assertEqual(diagnostic["compute_sha"], "a" * 40)
        self.assertEqual(diagnostic["store_input_sha"], "b" * 40)
        self.assertEqual(diagnostic["invocation"]["invocation_id"], "12345-2")
        self.assertEqual(diagnostic["failure_target_count"], 1)
        self.assertEqual(diagnostic["conflict_record_count"], 0)

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
            "failure_target_count": 4,
            "conflict_record_count": 3,
            "conflict_diagnostic_record_count": 2,
            "conflict_diagnostics_truncated": True,
            "result_state_path": "collector-state/private.json",
            "conflict_diagnostics": [{"symbol": "PRIVATE-SYMBOL", "old_value": 91.25,
                                       "new_value": 92.25, "request_url": "https://x/?apikey=SECRET"}],
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
        self.assertEqual(summary["failure_target_count"], 4)
        self.assertEqual(summary["conflict_record_count"], 3)
        self.assertEqual(summary["conflict_diagnostic_record_count"], 2)
        self.assertTrue(summary["conflict_diagnostics_truncated"])
        self.assertEqual(summary["failure_classes"], ["MarketDataConflict"])
        for private in ("PRIVATE-SYMBOL", "PRIVATE-VALUES", "PRIVATE-UNTRUSTED", "91.25", "SECRET"):
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
