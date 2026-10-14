"""P07 regression: bad provider numerics cannot win or reach the Store."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import market_data_collection as regular
from market_data_store import canonical_bytes, write_capture

INVALID = ("NaN", "Infinity", "-Infinity", "1e309", float("nan"), float("inf"), float("-inf"))


def fact(minute, provider):
    timestamp = f"2026-10-13T09:{minute:02d}:00-04:00"
    result = {
        "symbol": "KWEB", "asset_class": "etf", "session": "regular",
        "event_time": timestamp, "source_timestamp": timestamp,
    }
    if provider == "twelve":
        result.update({"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 2.0})
    else:
        result.update({"last_sale": 101.0, "reported_volume": 2.0})
    return result


def diag(provider, count):
    return {
        "provider": provider, "raw_row_count": count, "timestamp_parseable_count": count,
        "trade_date_match_count": count, "window_row_count": count,
        "target_window_match_count": count,
    }


class NonfiniteProviderSelectionTest(unittest.TestCase):
    def collect(self, nasdaq, twelve):
        access = {
            "regular_intraday_collection": {
                "fallback_when_primary_is_incomplete_target_window": True,
            },
            "nasdaq_public_intraday": {"max_workers": 1},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            regular, "fetch_nasdaq_regular",
            return_value=(nasdaq, diag(regular.NASDAQ, len(nasdaq)))
        ), mock.patch.object(
            regular, "fetch_twelve_regular",
            return_value=(twelve, diag(regular.TWELVE, len(twelve)))
        ) as alternate:
            root = Path(tmp)
            result = regular.collect_regular_window(
                mode="open_15m", stage=None, trade_date="2026-10-13",
                start_et="09:30", end_et="09:33",
                store_root=root, universe_config={"daily_series": [], "context_proxies": {}},
                config={"collector": {"budgets": {"twelve_data_basic": {
                    "hard_credits_per_day": 200, "hard_credits_per_minute": 8,
                }}}}, access=access, eligible_universe=[("KWEB", "etf")],
            )
            captures = list((root / "sessions").rglob("*.json"))
            self.assertEqual(len(captures), 1)
            raw = captures[0].read_text()
            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            snapshot = json.loads((root / result["snapshot_path"]).read_text())
            return result, snapshot["data_refs"], json.loads(raw)["qualified_facts"], alternate.call_count

    def test_complete_alternate_grid_with_nonfinite_close_cannot_replace_primary(self):
        for invalid in INVALID:
            with self.subTest(invalid=str(invalid)):
                primary = [fact(n, "nasdaq") for n in (30, 32)]
                alternate = [fact(n, "twelve") for n in (30, 31, 32)]
                alternate[1]["close"] = invalid
                result, refs, facts, called = self.collect(primary, alternate)
                self.assertEqual(called, 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["provider_counts"], {regular.NASDAQ: 1})
                self.assertEqual([ref["provider"] for ref in refs], [regular.NASDAQ])
                self.assertEqual(len(facts), 2)
                self.assertTrue(all(regular._finite_regular_fact(f) for f in facts))

    def test_invalid_nasdaq_fact_cannot_claim_full_grid_or_reach_store(self):
        for invalid in INVALID:
            with self.subTest(invalid=str(invalid)):
                primary = [fact(n, "nasdaq") for n in (30, 31, 32)]
                primary[1]["last_sale"] = invalid
                alternate = [fact(n, "twelve") for n in (30, 31, 32)]
                result, refs, facts, called = self.collect(primary, alternate)
                self.assertEqual(called, 1)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["provider_counts"], {regular.TWELVE: 1})
                self.assertEqual([ref["provider"] for ref in refs], [regular.TWELVE])
                self.assertEqual(len(facts), 3)
                self.assertTrue(all(regular._finite_regular_fact(f) for f in facts))

    def test_store_rejects_any_nested_nonfinite_market_number(self):
        for invalid in INVALID:
            if isinstance(invalid, str):
                continue
            with self.subTest(invalid=str(invalid)), self.assertRaises(ValueError):
                canonical_bytes({"qualified_facts": [{"last_sale": invalid}]})
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    write_capture(
                        tmp, trade_date="2026-10-13", session="regular",
                        provider="nasdaq_public_intraday", capture_id="nonfinite",
                        generated_at="2026-10-13T10:00:00-04:00",
                        qualified_facts=[{
                            "symbol": "KWEB", "last_sale": invalid,
                            "source_timestamp": "2026-10-13T09:30:00-04:00",
                        }],
                    )
                self.assertEqual(list(Path(tmp).rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
