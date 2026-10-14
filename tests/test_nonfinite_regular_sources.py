"""SMD43-P1-01: both native regular providers must discard malformed OHLCV."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import market_data_collectors as base
import market_data_collection as regular

INVALID = ("NaN", "Infinity", "-Infinity", "1e309", float("nan"), float("inf"), float("-inf"))


class NonfiniteRegularProviderTest(unittest.TestCase):
    def test_number_rejects_nonfinite_inputs_and_accepts_finite(self):
        for value in INVALID:
            with self.subTest(value=str(value)):
                self.assertIsNone(regular._number(value))
        self.assertEqual(regular._number("1,000.25"), 1000.25)

    def test_nasdaq_real_parser_discards_invalid_last_sale_or_volume(self):
        timestamp = int(datetime.fromisoformat("2026-10-13T09:30:00-04:00").timestamp() * 1000)
        access = {
            "http": {"timeout_seconds": 10, "user_agent": "test"},
            "nasdaq_public_intraday": {
                "regular_session_endpoint_template": "https://example.invalid/{symbol}/{asset_class}",
                "chart_array": "data.chart",
                "timestamp_field": "x", "last_sale_price_field": "y",
                "reported_minute_volume_field": "w",
            },
        }
        for field in ("y", "w"):
            for value in INVALID:
                with self.subTest(field=field, value=str(value)):
                    rows = [
                        {"x": timestamp + 60000 * minute, "y": "101", "w": "10"}
                        for minute in (0, 1, 2)
                    ]
                    rows[1][field] = value
                    with mock.patch.object(
                        base, "_http_json", return_value={"data": {"chart": rows}}
                    ):
                        facts, _diag = regular.fetch_nasdaq_regular(
                            "KWEB", "etf", "2026-10-13", "09:30", "09:33", access
                        )
                    self.assertEqual(len(facts), 2)
                    self.assertTrue(all(regular._finite_regular_fact(f) for f in facts))
                    self.assertFalse(regular._full_window_from_one_provider(
                        facts, "2026-10-13", "09:30", "09:33"
                    ))

    def test_twelve_real_parser_discards_invalid_close_ohlc_or_volume(self):
        access = {
            "http": {"timeout_seconds": 10, "user_agent": "test"},
            "twelve_data_basic": {
                "base_url": "https://example.invalid", "time_series_path": "/time_series",
                "api_key_env": "TWELVE_DATA_API_KEY",
            },
        }
        config = {"collector": {"budgets": {"twelve_data_basic": {
            "hard_credits_per_day": 200, "hard_credits_per_minute": 8,
        }}}}
        for field in ("open", "high", "low", "close", "volume"):
            for value in INVALID:
                with self.subTest(field=field, value=str(value)):
                    rows = [
                        {
                            "datetime": f"2026-10-13 09:{minute:02d}:00",
                            "open": "101", "high": "102", "low": "100",
                            "close": "101", "volume": "10",
                        }
                        for minute in (30, 31, 32)
                    ]
                    rows[1][field] = value
                    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                        "os.environ", {"TWELVE_DATA_API_KEY": "unit-test"}
                    ), mock.patch.object(
                        base, "_http_json", return_value={"values": rows}
                    ), mock.patch.object(
                        base, "wait_for_twelve_budget"
                    ), mock.patch.object(base, "consume_twelve_credit"):
                        facts, _diag = regular.fetch_twelve_regular(
                            "KWEB", "etf", "2026-10-13", "09:30", "09:33",
                            store_root=Path(tmp), config=config, access=access,
                        )
                    self.assertEqual(len(facts), 2)
                    self.assertTrue(all(regular._finite_regular_fact(f) for f in facts))
                    self.assertFalse(regular._full_window_from_one_provider(
                        facts, "2026-10-13", "09:30", "09:33"
                    ))


if __name__ == "__main__":
    unittest.main()
