"""SMD43-P1-01: exact in-session non-finite numerics fail closed."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collection_universe
import market_data_collectors as base
import nasdaq_extended_adapter as adapter
import premarket_collection as premarket

INVALID = ("NaN", "Infinity", "-Infinity", "1e309", float("nan"), float("inf"), float("-inf"))


def payload(rows):
    return {
        "status": {"rCode": 200},
        "data": {
            "lastUpdateInfo": ["Data last updated Aug 17, 2026 09:30 AM ET."],
            "tradeDetailTable": {"rows": rows},
        },
    }


def row(price="100", volume="5", time="09:29:55"):
    return {"price": price, "shareVolume": volume, "time": time}


class NonfinitePremarketTest(unittest.TestCase):
    def test_mixed_session_in_window_numeric_values_must_be_finite(self):
        for field in ("price", "shareVolume"):
            for value in INVALID:
                with self.subTest(field=field, value=str(value)):
                    target = row()
                    target[field] = value
                    with self.assertRaisesRegex(adapter.ExtendedHoursAdapterError, "finite"):
                        adapter.normalize_extended_trade_payload(
                            payload([row(time="09:30:00"), target]),
                            session="premarket", symbol="NVDA", asset_class="stocks",
                            endpoint_id="extended_trading_pre",
                            allow_mixed_session_rows=True,
                        )

    def test_premarket_invalid_symbol_does_not_create_market_fact_capture(self):
        universe = collection_universe.load_collection_universe(
            ROOT / "config/collection-universe.json"
        )
        access = base.load_yaml(ROOT / "config/market-data-collector-access.yaml")

        def fake_request(url, **kwargs):
            if "/NVDA/" in url:
                return payload([row(price="1e309")])
            return payload([row(price="101", time="09:29:57")])

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            premarket.base, "_http_json", side_effect=fake_request
        ):
            root = Path(tmp)
            result = premarket.collect_premarket(
                trade_date="2026-08-17", store_root=root,
                universe_config=universe, access=access,
                symbols_override=["NVDA", "PLTR"],
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["missing"], ["NVDA"])
            pointer = json.loads(
                (root / "snapshots/2026-08/2026-08-17/premarket/latest.json").read_text()
            )
            snapshot = json.loads((root / pointer["snapshot_path"]).read_text())
            self.assertEqual([entry["symbol"] for entry in snapshot["data_refs"]], ["PLTR"])
            captures = list((root / "sessions").rglob("*.json"))
            self.assertEqual(len(captures), 1)
            assert "NaN" not in captures[0].read_text()
            assert "Infinity" not in captures[0].read_text()


if __name__ == "__main__":
    unittest.main()
