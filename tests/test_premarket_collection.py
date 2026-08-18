import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection_universe as collection  # noqa: E402
import premarket_collection as premarket  # noqa: E402


def payload(*, last_update="Data last updated Aug 17, 2026 07:44 AM ET.", rows=None):
    return {
        "status": {"rCode": 200, "bCodeMessage": None},
        "data": {
            "lastUpdateInfo": ["This page refreshes every 30 seconds.", last_update],
            "tradeDetailTable": {
                "rows": rows or [{"price": "$226.5423", "shareVolume": "5", "time": "07:29:53"}]
            },
        },
    }


class PremarketCollectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = collection.load_collection_universe(ROOT / "config/collection-universe.json")
        import market_data_collectors as base
        cls.access = base.load_yaml(ROOT / "config/market-data-collector-access.yaml")

    def test_first_class_universe_is_exact_core_plus_anchors(self):
        symbols = {symbol for symbol, _asset in collection.premarket_universe(self.universe)}
        self.assertEqual(
            symbols,
            {"TME","PLTR","SPCX","NVDA","TSLA","MU","META","ORCL","TSM","AMD","QQQ","SPY"},
        )

    def test_fetch_binds_trade_date_session_timestamp_and_security_identity(self):
        with mock.patch("premarket_collection.base._http_json", return_value=payload()):
            facts, diag = premarket.fetch_premarket(
                "NVDA", "stocks", "2026-08-17", universe=self.universe, access=self.access
            )
        self.assertEqual(diag["trade_date"], "2026-08-17")
        self.assertEqual(facts[-1]["session"], "premarket")
        self.assertEqual(facts[-1]["event_time"], "2026-08-17T07:29:53-04:00")
        self.assertEqual(facts[-1]["source_timestamp"], facts[-1]["event_time"])
        self.assertEqual(facts[-1]["security_identity"]["symbol"], "NVDA")
        self.assertEqual(facts[-1]["security_identity"]["asset_class"], "stocks")
        self.assertFalse(facts[-1]["realtime_claim"])
        self.assertEqual(facts[-1]["source_contract"], premarket.SOURCE_CONTRACT)

    def test_wrong_trade_date_fails_closed(self):
        with mock.patch("premarket_collection.base._http_json", return_value=payload(last_update="Data last updated Aug 16, 2026 07:44 AM ET.")):
            with self.assertRaisesRegex(Exception, "wrong Premarket trade date"):
                premarket.fetch_premarket(
                    "NVDA", "stocks", "2026-08-17", universe=self.universe, access=self.access
                )

    def test_partial_symbol_failure_is_claim_scoped_and_snapshot_is_written(self):
        def fake_http(url, **_kwargs):
            if "/PLTR/" in url:
                raise RuntimeError("synthetic transport failure")
            return payload()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("premarket_collection.base._http_json", side_effect=fake_http):
            root = Path(tmp)
            result = premarket.collect_premarket(
                trade_date="2026-08-17",
                store_root=root,
                universe_config=self.universe,
                access=self.access,
                symbols_override=["NVDA", "PLTR"],
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["missing"], ["PLTR"])
            self.assertTrue(result["snapshot_written"])
            latest = root / "snapshots" / "2026-08" / "2026-08-17" / "premarket" / "latest.json"
            self.assertTrue(latest.exists())
            pointer = json.loads(latest.read_text(encoding="utf-8"))
            snapshot = root / pointer["path"]
            doc = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(doc["stage"], "premarket")
            self.assertEqual(doc["missing"], ["PLTR"])
            self.assertEqual(doc["coverage"]["symbols_available_in_increment"], 1)

    def test_trigger_contract_has_scheduled_and_on_demand_premarket(self):
        entry = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        runtime = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        schema = json.loads((ROOT / "schemas/collector-request.schema.json").read_text(encoding="utf-8"))
        contract = (ROOT / "config/data-plane.yaml").read_text(encoding="utf-8")
        self.assertIn("'20 7 * * 1-5': 'premarket'", runtime)
        self.assertIn("scripts/premarket_collection.py", runtime)
        self.assertIn("smoke_readonly|nasdaq_extended_probe|premarket", runtime)
        self.assertIn("- premarket", entry)
        self.assertIn("premarket", schema["properties"]["mode"]["enum"])
        self.assertIn("runtime_modes: [premarket, open_15m, open_30m, open_60m, close]", contract)


if __name__ == "__main__":
    unittest.main()
