"""Regression tests for full-provider window selection under sparse Nasdaq facts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collection as collector  # noqa: E402


def facts(provider: str, minutes: list[int], *, terminal_seconds: int = 0) -> list[dict[str, object]]:
    result = []
    for minute in minutes:
        suffix = terminal_seconds if minute == 32 else 0
        timestamp = f"2026-10-13T09:{minute:02d}:{suffix:02d}-04:00"
        result.append({
            "symbol": "KWEB",
            "asset_class": "etf",
            "session": "regular",
            "event_time": timestamp,
            "source_timestamp": timestamp,
            "last_sale": 100.0,
            "reported_volume": 1.0,
            "source_contract": provider,
        })
    return result


def diag(provider: str, n: int) -> dict[str, object]:
    return {
        "provider": provider,
        "raw_row_count": n,
        "timestamp_parseable_count": n,
        "trade_date_match_count": n,
        "window_row_count": n,
        "target_window_match_count": n,
    }


class SparseWindowWholeProviderFailoverTest(unittest.TestCase):
    def run_case(self, nasdaq, twelve, *, enabled=True):
        access = {
            "regular_intraday_collection": {
                "fallback_when_primary_is_incomplete_target_window": enabled,
            },
            "nasdaq_public_intraday": {"max_workers": 1},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            collector, "fetch_nasdaq_regular", return_value=(nasdaq, diag(collector.NASDAQ, len(nasdaq)))
        ), mock.patch.object(
            collector, "fetch_twelve_regular", side_effect=twelve if isinstance(twelve, Exception) else None,
            return_value=None if isinstance(twelve, Exception) else (twelve, diag(collector.TWELVE, len(twelve)))
        ) as alternate:
            result = collector.collect_regular_window(
                mode="open_15m",
                stage=None,
                trade_date="2026-10-13",
                start_et="09:30",
                end_et="09:33",
                store_root=Path(tmp),
                universe_config={"daily_series": [], "context_proxies": {}},
                config={"collector": {"budgets": {"twelve_data_basic": {"hard_credits_per_day": 200, "hard_credits_per_minute": 8}}}},
                access=access,
                eligible_universe=[("KWEB", "etf")],
            )
            doc = json.loads((Path(tmp) / result["snapshot_path"]).read_text())
            refs = doc["data_refs"]
            captures = [
                json.loads((Path(tmp) / ref["path"]).read_text())
                for ref in refs
            ]
            return result, doc, refs, captures, alternate.call_count

    def test_incomplete_primary_uses_complete_alternate_one_provider_window(self):
        result, doc, refs, captures, count = self.run_case(
            facts("nasdaq", [30, 32]), facts("twelve", [30, 31, 32])
        )
        self.assertEqual(count, 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_counts"], {collector.TWELVE: 1})
        self.assertTrue(doc["coverage"]["stage_snapshot"]["complete"])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["provider"], collector.TWELVE)
        self.assertEqual(len(captures[0]["qualified_facts"]), 3)

    def test_incomplete_alternate_keeps_qualified_nasdaq_entire_window(self):
        result, doc, refs, captures, count = self.run_case(
            facts("nasdaq", [30, 32]), facts("twelve", [31, 32])
        )
        self.assertEqual(count, 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["provider_counts"], {collector.NASDAQ: 1})
        self.assertEqual(refs[0]["provider"], collector.NASDAQ)
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(captures[0]["qualified_facts"]), 2)

    def test_full_primary_skips_alternate_and_preserves_nasdaq_priority(self):
        result, _doc, refs, _captures, count = self.run_case(
            facts("nasdaq", [30, 31, 32]), facts("twelve", [30, 31, 32])
        )
        self.assertEqual(count, 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(refs[0]["provider"], collector.NASDAQ)

    def test_budget_or_provider_failure_keeps_primary_without_data_loss(self):
        result, _doc, refs, _captures, count = self.run_case(
            facts("nasdaq", [30, 32]), RuntimeError("quota exceeded")
        )
        self.assertEqual(count, 1)
        self.assertEqual(refs[0]["provider"], collector.NASDAQ)
        self.assertEqual(result["status"], "partial")
        self.assertIn("budget_exhausted", result["reason_classes"]["KWEB"])

    def test_zero_primary_accepts_partial_alternate_as_truthful_partial(self):
        result, _doc, refs, captures, count = self.run_case(
            [], facts("twelve", [30, 32])
        )
        self.assertEqual(count, 1)
        self.assertEqual(refs[0]["provider"], collector.TWELVE)
        self.assertEqual(len(captures[0]["qualified_facts"]), 2)
        self.assertEqual(result["status"], "partial")

    def test_alternate_with_nonexact_terminal_is_not_full_qualifying_window(self):
        result, _doc, refs, _captures, count = self.run_case(
            facts("nasdaq", [30, 32]), facts("twelve", [30, 31, 32], terminal_seconds=30)
        )
        self.assertEqual(count, 1)
        self.assertEqual(refs[0]["provider"], collector.NASDAQ)
        self.assertEqual(result["status"], "partial")

    def test_flag_disabled_retains_original_nonempty_primary_selection(self):
        result, _doc, refs, _captures, count = self.run_case(
            facts("nasdaq", [30, 32]), facts("twelve", [30, 31, 32]), enabled=False
        )
        self.assertEqual(count, 0)
        self.assertEqual(refs[0]["provider"], collector.NASDAQ)
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
