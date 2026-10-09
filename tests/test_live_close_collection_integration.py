"""Close retry integration: mock providers, never the collection/Store owners."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_market_data as entry
import collection_universe as universe_owner
import market_data_collection as runtime


class LiveCloseCollectionIntegrationTest(unittest.TestCase):
    DATE = "2026-10-07"
    MODES = ("close_retry", "close_final")

    def setUp(self):
        self.universe = universe_owner.load_collection_universe(
            ROOT / "config/collection-universe.json"
        )
        self.eligible = dict(entry._live_close_universe(self.universe))
        self.assertIn("SPY", self.eligible)
        self.assertIn("XLE", self.eligible)

    def facts(self, symbol, minutes=range(45, 60)):
        return [universe_owner.decorate_fact(self.universe, {
            "symbol": symbol,
            "asset_class": self.eligible[symbol],
            "session": "regular",
            "event_time": f"{self.DATE}T15:{minute:02d}:00-04:00",
            "source_timestamp": f"{self.DATE}T15:{minute:02d}:00-04:00",
            "last_sale": 100.0,
            "reported_volume": 1.0,
        }) for minute in minutes]

    def seed(self, store, hints, *, conflicting=False):
        absent = set() if conflicting else {hint.upper() for hint in hints}
        contexts = universe_owner.context_by_symbol(self.universe)
        sectors = set(universe_owner.sector_symbols(self.universe))
        refs = []
        for symbol in sorted(set(self.eligible) - absent):
            facts = self.facts(symbol)
            path, blob = runtime.write_capture(
                store, trade_date=self.DATE, session="regular",
                provider=runtime.NASDAQ, capture_id=f"prior-{symbol.lower()}",
                generated_at=f"{self.DATE}T16:00:05-04:00",
                actual_data_cutoff=facts[-1]["source_timestamp"],
                window={"start": "15:45", "end": "16:00"},
                feed_scope="nasdaq_public_chart_last_sale_volume_v1",
                qualified_facts=facts, missing_symbols=[],
            )
            kind = ("cross_asset_proxy_capture" if symbol in contexts else
                    "sector_context_capture" if symbol in sectors else
                    "regular_intraday_capture")
            refs.append({"path": path, "blob_sha": blob, "kind": kind,
                         "window": "15:45-16:00", "provider": runtime.NASDAQ,
                         "symbol": symbol, "reason_class": "success"})
        prior_path, _changed = runtime.write_snapshot(
            store, stage="close", trade_date=self.DATE, snapshot_id="prior-close",
            generated_at=f"{self.DATE}T16:00:10-04:00", data_refs=refs,
            coverage={}, missing=list(hints),
            target_window={"start": "15:45", "end": "16:00"},
            actual_data_cutoff=f"{self.DATE}T15:59:00-04:00",
        )
        _refs, raw_hints = entry.base._load_prior_snapshot(store, self.DATE, "close")
        self.assertEqual(raw_hints, list(hints))
        immutable = {path: (store / path).read_bytes()
                     for path in [prior_path] + [ref["path"] for ref in refs]}
        return prior_path, immutable

    def invoke(self, store, mode, recovered):
        def nasdaq(symbol, asset, trade_date, start, end, _access):
            self.assertEqual((asset, trade_date, start, end),
                             (self.eligible[symbol], self.DATE, "15:45", "16:00"))
            facts = recovered.get(symbol, [])
            return facts, {"provider": runtime.NASDAQ,
                           "raw_row_count": len(facts),
                           "timestamp_parseable_count": len(facts),
                           "trade_date_match_count": len(facts),
                           "window_row_count": len(facts),
                           "target_window_match_count": len(facts)}

        output = io.StringIO()
        # These are the only mocks. The wrapper, collection, coverage, blob
        # readers and snapshot/capture/state writers all execute real code.
        with mock.patch.object(runtime, "fetch_nasdaq_regular", side_effect=nasdaq) as primary:
            with mock.patch.object(runtime, "fetch_twelve_regular", return_value=([], {
                "provider": runtime.TWELVE, "raw_row_count": 0,
                "timestamp_parseable_count": 0, "trade_date_match_count": 0,
                "window_row_count": 0, "target_window_match_count": 0,
            })) as fallback:
                with contextlib.redirect_stdout(output):
                    rc = entry._run_live_close([
                        "--mode", mode, "--trade-date", self.DATE,
                        "--store-root", str(store),
                        "--universe", str(ROOT / "config/collection-universe.json"),
                        "--config", str(ROOT / "config/market-data-store.yaml"),
                        "--access-config", str(ROOT / "config/market-data-collector-access.yaml"),
                    ])
        self.assertEqual(rc, 0)
        return (json.loads(output.getvalue()),
                sorted(call.args[0] for call in primary.call_args_list),
                sorted(call.args[0] for call in fallback.call_args_list))

    def assert_round_trip(self, hints, recovered, remaining, *, sparse=()):
        requested = sorted({hint.upper() for hint in hints})
        expected_missing = sorted(remaining)
        for mode in self.MODES:
            with self.subTest(mode=mode, hints=hints), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                prior_path, immutable = self.seed(store, hints)
                pointer_path = store / f"snapshots/{self.DATE[:7]}/{self.DATE}/close/latest.json"
                pointer_before = pointer_path.read_bytes()
                result, primary, fallback = self.invoke(store, mode, recovered)
                self.assertEqual(primary, requested)
                self.assertEqual(fallback, expected_missing)
                self.assertEqual(result["snapshot_missing"], expected_missing)
                self.assertEqual(result["missing"], expected_missing)
                self.assertEqual(runtime._read_close_state(store, self.DATE), expected_missing)
                stage = result["coverage"]["stage_snapshot"]
                self.assertEqual(stage["missing_symbols"], expected_missing)
                self.assertEqual(stage["qualified_symbol_count"],
                                 len(self.eligible) - len(expected_missing))
                self.assertEqual(stage["partial_symbols"], sorted(sparse))
                self.assertEqual(stage["full_window_symbol_count"],
                                 len(self.eligible) - len(expected_missing) - len(sparse))
                self.assertEqual(stage["complete"], not expected_missing and not sparse)
                self.assertEqual(result["symbols_requested_in_increment"], len(requested))
                self.assertEqual(result["coverage"]["inherited_ref_count"],
                                 len(self.eligible) - len(requested))

                if recovered:
                    self.assertTrue(result["snapshot_written"])
                    self.assertEqual(result["status"],
                                     "partial" if expected_missing or sparse else "ok")
                    self.assertNotEqual(result["snapshot_path"], prior_path)
                    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                    self.assertEqual(pointer["snapshot_path"], result["snapshot_path"])
                    persisted = entry.base._verify_local_blob(
                        store, pointer["snapshot_path"], pointer["snapshot_blob_sha"]
                    )
                    self.assertEqual(persisted["missing"], expected_missing)
                    self.assertEqual(persisted["coverage"]["stage_snapshot"], stage)
                    refs, persisted_missing = entry.base._load_prior_snapshot(store, self.DATE, "close")
                    self.assertEqual(persisted_missing, expected_missing)
                    self.assertEqual({ref["symbol"] for ref in refs},
                                     set(self.eligible) - set(expected_missing))
                    new_refs = [ref for ref in refs if ref["path"] not in immutable]
                    self.assertEqual({ref["symbol"] for ref in new_refs}, set(recovered))
                else:
                    self.assertFalse(result["snapshot_written"])
                    self.assertEqual(result["status"], "no_new_qualified_facts")
                    self.assertEqual(pointer_path.read_bytes(), pointer_before)

                for path, before in immutable.items():
                    self.assertEqual((store / path).read_bytes(), before)
                if not expected_missing:
                    before = {path.relative_to(store): path.read_bytes()
                              for path in store.rglob("*") if path.is_file()}
                    again, primary_again, fallback_again = self.invoke(store, mode, {})
                    self.assertEqual(again["status"], "nothing_missing")
                    self.assertEqual((primary_again, fallback_again), ([], []))
                    self.assertEqual({path.relative_to(store): path.read_bytes()
                                      for path in store.rglob("*") if path.is_file()}, before)

    def test_lowercase_hint_success_reaches_real_collection_and_snapshot(self):
        self.assert_round_trip(["spy"], {"SPY": self.facts("SPY")}, [])

    def test_mixed_case_hint_success_reaches_real_collection_and_snapshot(self):
        self.assert_round_trip(["SpY"], {"SPY": self.facts("SPY")}, [])

    def test_uppercase_recovery_remains_compatible(self):
        self.assert_round_trip(["SPY"], {"SPY": self.facts("SPY")}, [])

    def test_sparse_recovery_clears_missing_without_claiming_complete_window(self):
        self.assert_round_trip(["spy"], {"SPY": self.facts("SPY", [59])}, [], sparse=["SPY"])

    def test_failed_recovery_keeps_one_canonical_missing_without_new_snapshot(self):
        self.assert_round_trip(["spy"], {}, ["SPY"])

    def test_partial_success_persists_only_the_unrecovered_symbol(self):
        self.assert_round_trip(["spy", "xLe"], {"SPY": self.facts("SPY")}, ["XLE"])

    def test_conflicting_hint_still_stops_before_provider_io(self):
        for mode in self.MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                self.seed(store, ["spy"], conflicting=True)
                before = {path.relative_to(store): path.read_bytes()
                          for path in store.rglob("*") if path.is_file()}
                with mock.patch.object(runtime, "fetch_nasdaq_regular") as primary:
                    with mock.patch.object(runtime, "fetch_twelve_regular") as fallback:
                        with self.assertRaises(runtime.CoverageContractError):
                            entry._run_live_close([
                                "--mode", mode, "--trade-date", self.DATE,
                                "--store-root", str(store),
                                "--universe", str(ROOT / "config/collection-universe.json"),
                                "--config", str(ROOT / "config/market-data-store.yaml"),
                                "--access-config", str(ROOT / "config/market-data-collector-access.yaml"),
                            ])
                        primary.assert_not_called()
                        fallback.assert_not_called()
                self.assertEqual({path.relative_to(store): path.read_bytes()
                                  for path in store.rglob("*") if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
