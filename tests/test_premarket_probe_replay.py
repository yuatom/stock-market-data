import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collection_universe as collection  # noqa: E402
import replay_premarket_probe as replay  # noqa: E402


class PremarketProbeReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = collection.load_collection_universe(ROOT / "config/collection-universe.json")
        cls.targets = collection.premarket_universe(cls.universe)

    def _result(self, symbol, asset_class):
        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "session_candidate": "premarket",
            "candidate_id": "extended_trading_pre",
            "http_status": 200,
            "application_status_success": True,
            "application_status_code": 200,
            "response_status_object": {"rCode": 200, "bCodeMessage": None},
            "error_class": None,
            "trade_date_candidate": "2026-08-17",
            "trade_date_resolvable_without_hindsight": True,
            "trade_detail_all_rows_in_candidate_session_window": True,
            "trade_detail_row_count": 100,
            "trade_detail_time_parseable_count": 100,
            "trade_detail_session_window_match_count": 100,
            "last_update_timestamp_et": "2026-08-17T07:44:00-04:00",
            "last_update_info": [
                "This page refreshes every 30 seconds.",
                "Data last updated Aug 17, 2026 07:44 AM ET.",
            ],
            "trade_detail_first_row": {"price": "$101.25", "shareVolume": "5", "time": "07:29:53"},
            "trade_detail_last_row": {"price": "$100.00", "shareVolume": "10", "time": "04:00:02"},
        }

    def _probe(self):
        return {
            "schema_version": 4,
            "probe": "nasdaq_extended_hours",
            "role": "shadow_transport_probe_only",
            "generated_at": "2026-08-17T07:44:52.491036-04:00",
            "probe_calendar_date_et": "2026-08-17",
            "requested_sessions": ["premarket"],
            "market_fact_authority": False,
            "automatic_promotion_allowed": False,
            "results": [self._result(symbol, asset_class) for symbol, asset_class in self.targets],
        }

    def test_replay_uses_only_preserved_samples_and_writes_12_store_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            probe_path = store / "collector-state/probes/nasdaq-extended/2026-08/2026-08-17/074452-et.json"
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(json.dumps(self._probe(), sort_keys=True) + "\n", encoding="utf-8")
            result = replay.replay_probe(
                probe_path=probe_path,
                trade_date="2026-08-17",
                store_root=store,
                universe_config=self.universe,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbols_available_in_increment"], 12)
            self.assertTrue(result["sample_only"])
            self.assertEqual(result["actual_data_cutoff"], "2026-08-17T07:29:53-04:00")

            latest = json.loads(
                (store / "snapshots/2026-08/2026-08-17/premarket/latest.json").read_text(encoding="utf-8")
            )
            snapshot = json.loads((store / latest["snapshot_path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(snapshot["data_refs"]), 12)
            self.assertTrue(snapshot["coverage"]["historical_probe_replay_sample"])

            first_capture = json.loads((store / snapshot["data_refs"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(len(first_capture["qualified_facts"]), 2)
            fact = first_capture["qualified_facts"][-1]
            self.assertEqual(fact["source_contract"], replay.SOURCE_CONTRACT)
            self.assertEqual(fact["session"], "premarket")
            self.assertFalse(fact["realtime_claim"])
            self.assertTrue(fact["replay_provenance"]["retained_samples_only"])
            self.assertEqual(fact["replay_provenance"]["original_trade_detail_row_count"], 100)
            self.assertEqual(first_capture["feed_scope"], replay.FEED_SCOPE)

    def test_full_row_session_failure_blocks_replay_even_if_saved_samples_look_valid(self):
        probe = self._probe()
        probe["results"][0]["trade_detail_all_rows_in_candidate_session_window"] = False
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            probe_path = store / "collector-state/probes/nasdaq-extended/2026-08/2026-08-17/074452-et.json"
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(json.dumps(probe, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(replay.ProbeReplayError, "every original row"):
                replay.replay_probe(
                    probe_path=probe_path,
                    trade_date="2026-08-17",
                    store_root=store,
                    universe_config=self.universe,
                )

    def test_missing_target_blocks_replay(self):
        probe = self._probe()
        probe["results"] = probe["results"][:-1]
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            probe_path = store / "collector-state/probes/nasdaq-extended/2026-08/2026-08-17/074452-et.json"
            probe_path.parent.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(json.dumps(probe, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(replay.ProbeReplayError, "coverage mismatch"):
                replay.replay_probe(
                    probe_path=probe_path,
                    trade_date="2026-08-17",
                    store_root=store,
                    universe_config=self.universe,
                )


if __name__ == "__main__":
    unittest.main()
