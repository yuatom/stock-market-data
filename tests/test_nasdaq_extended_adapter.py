import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "nasdaq_extended_adapter.py"
spec = importlib.util.spec_from_file_location("nasdaq_extended_adapter", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def payload(*, last_update="Data last updated Aug 14, 2026 05:00 PM ET.", rows=None, rcode=200):
    return {
        "status": {"rCode": rcode, "bCodeMessage": None},
        "data": {
            "lastUpdateInfo": ["This page refreshes every 30 seconds.", last_update],
            "tradeDetailTable": {
                "rows": rows
                if rows is not None
                else [
                    {"price": "$970.22", "shareVolume": "15", "time": "16:45:17"},
                    {"price": "$970.18", "shareVolume": "1,100", "time": "16:44:05"},
                ]
            },
        },
    }


class ExtendedAdapterTest(unittest.TestCase):
    def test_after_hours_normalizes_candidate_facts_without_authority(self):
        result = module.normalize_extended_trade_payload(
            payload(),
            session="after_hours",
            symbol="mu",
            asset_class="stocks",
            endpoint_id="extended_trading_post",
        )
        self.assertFalse(result["market_fact_authority"])
        self.assertTrue(result["promotion_required"])
        self.assertEqual(result["trade_date"], "2026-08-14")
        self.assertEqual(result["actual_data_cutoff"], "2026-08-14T16:45:17-04:00")
        self.assertEqual(len(result["qualified_candidate_facts"]), 2)
        latest = result["qualified_candidate_facts"][-1]
        self.assertEqual(latest["symbol"], "MU")
        self.assertEqual(latest["session"], "after_hours")
        self.assertEqual(latest["last_sale"], 970.22)
        self.assertEqual(latest["reported_share_volume"], 15.0)
        self.assertGreaterEqual(latest["observed_delay_seconds"], 0)

    def test_premarket_uses_explicit_last_update_date(self):
        result = module.normalize_extended_trade_payload(
            payload(
                last_update="Data last updated Aug 14, 2026 08:01 AM ET.",
                rows=[{"price": "$225.94", "shareVolume": "300", "time": "07:46:24"}],
            ),
            session="premarket",
            symbol="NVDA",
            asset_class="stocks",
            endpoint_id="extended_trading_pre",
        )
        self.assertEqual(result["trade_date"], "2026-08-14")
        self.assertEqual(result["actual_data_cutoff"], "2026-08-14T07:46:24-04:00")

    def test_wrong_session_clock_fails_closed(self):
        with self.assertRaises(module.ExtendedHoursAdapterError):
            module.normalize_extended_trade_payload(
                payload(rows=[{"price": "$1", "shareVolume": "1", "time": "15:59:59"}]),
                session="after_hours",
                symbol="MU",
                asset_class="stocks",
                endpoint_id="extended_trading_post",
            )

    def test_future_trade_relative_to_last_update_fails_closed(self):
        with self.assertRaises(module.ExtendedHoursAdapterError):
            module.normalize_extended_trade_payload(
                payload(
                    last_update="Data last updated Aug 14, 2026 04:30 PM ET.",
                    rows=[{"price": "$1", "shareVolume": "1", "time": "16:45:00"}],
                ),
                session="after_hours",
                symbol="MU",
                asset_class="stocks",
                endpoint_id="extended_trading_post",
            )

    def test_application_error_fails_closed(self):
        with self.assertRaises(module.ExtendedHoursAdapterError):
            module.normalize_extended_trade_payload(
                payload(rcode=400),
                session="after_hours",
                symbol="MU",
                asset_class="stocks",
                endpoint_id="extended_trading_post",
            )

    def test_missing_or_bad_trade_fields_fail_closed(self):
        for row in (
            {"price": "n/a", "shareVolume": "1", "time": "16:45:00"},
            {"price": "$1", "shareVolume": "-1", "time": "16:45:00"},
            {"price": "$1", "shareVolume": "1", "time": "bad"},
        ):
            with self.subTest(row=row):
                with self.assertRaises(module.ExtendedHoursAdapterError):
                    module.normalize_extended_trade_payload(
                        payload(rows=[row]),
                        session="after_hours",
                        symbol="MU",
                        asset_class="stocks",
                        endpoint_id="extended_trading_post",
                    )


if __name__ == "__main__":
    unittest.main()
