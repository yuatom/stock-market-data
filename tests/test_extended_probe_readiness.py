import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_nasdaq_extended_probe.py"
spec = importlib.util.spec_from_file_location("evaluate_nasdaq_extended_probe", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

CORE = ["TME", "PLTR", "SPCX", "NVDA", "TSLA", "MU", "META", "ORCL", "TSM", "AMD"]
TARGETS = CORE + ["QQQ", "SPY"]


def make_config(path: Path):
    payload = {
        "nasdaq_extended_hours_probe": {
            "probe_targets": [
                {"symbol": s, "asset_class": "etf" if s in {"QQQ", "SPY"} else "stocks"}
                for s in TARGETS
            ],
            "promotion_gate": {
                "minimum_distinct_trade_sessions": 3,
                "required_core_stock_targets": CORE,
                "required_market_anchor_targets": ["QQQ", "SPY"],
            },
        }
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def make_probe(root: Path, date: str, session: str, missing=None):
    missing = set(missing or [])
    results = []
    for symbol in TARGETS:
        if symbol in missing:
            continue
        asset = "etf" if symbol in {"QQQ", "SPY"} else "stocks"
        results.append(
            {
                "session_candidate": session,
                "symbol": symbol,
                "asset_class": asset,
                "http_status": 200,
                "application_status_success": True,
                "error_class": None,
                "trade_date_resolvable_without_hindsight": True,
                "trade_detail_all_rows_in_candidate_session_window": True,
                "trade_detail_time_parseable_count": 3,
                "trade_date_candidate": date,
                "last_update_timestamp_et": f"{date}T17:00:00-04:00",
                "latest_trade_lag_seconds_vs_last_update": 60,
                "data_top_level_keys": ["infoTable", "tradeDetailTable"],
                "info_table_first_row_keys": ["consolidated", "volume"],
                "trade_detail_first_row_keys": ["price", "shareVolume", "time"],
            }
        )
    doc = {
        "probe": "nasdaq_extended_hours",
        "generated_at": f"{date}T17:01:00-04:00",
        "probe_calendar_date_et": date,
        "results": results,
    }
    path = root / "collector-state" / "probes" / "nasdaq-extended" / date[:7] / date / "170100-et.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


class ReadinessTest(unittest.TestCase):
    def test_three_complete_sessions_are_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            make_config(config)
            for date in ["2026-08-17", "2026-08-18", "2026-08-19"]:
                make_probe(root, date, "premarket")
            result = module.evaluate(config_path=config, store_root=root, sessions=["premarket"])
            session = result["sessions"]["premarket"]
            self.assertTrue(session["promotion_ready"])
            self.assertEqual(session["qualifying_trade_sessions"], 3)

    def test_two_sessions_are_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            make_config(config)
            for date in ["2026-08-17", "2026-08-18"]:
                make_probe(root, date, "after_hours")
            result = module.evaluate(config_path=config, store_root=root, sessions=["after_hours"])
            session = result["sessions"]["after_hours"]
            self.assertFalse(session["promotion_ready"])
            self.assertEqual(session["status"], "insufficient_distinct_trade_sessions")

    def test_missing_required_target_does_not_count_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.yaml"
            make_config(config)
            make_probe(root, "2026-08-17", "premarket")
            make_probe(root, "2026-08-18", "premarket", missing={"QQQ"})
            make_probe(root, "2026-08-19", "premarket")
            result = module.evaluate(config_path=config, store_root=root, sessions=["premarket"])
            session = result["sessions"]["premarket"]
            self.assertFalse(session["promotion_ready"])
            self.assertEqual(session["qualifying_trade_sessions"], 2)
            self.assertIn("QQQ", session["coverage_by_date"]["2026-08-18"]["missing_targets"])


if __name__ == "__main__":
    unittest.main()
