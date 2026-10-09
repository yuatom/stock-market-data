"""Regression tests for the actual live Close entrypoint and persisted inputs."""
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


class LiveCloseRetrySelectionTest(unittest.TestCase):
    DATE = "2026-10-07"
    ELIGIBLE = [("SPY", "etf"), ("XLE", "etf")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)

    def fact(self, symbol, stamp="2026-10-07T15:59:00-04:00"):
        return {"symbol": symbol, "asset_class": "etf", "session": "regular",
                "event_time": stamp, "source_timestamp": stamp,
                "last_sale": 100.0, "reported_volume": 1.0}

    def snapshot(self, facts_by_symbol, missing=()):
        refs = []
        for symbol, facts in facts_by_symbol.items():
            path, blob = runtime.write_capture(
                self.store, trade_date=self.DATE, session="regular",
                provider=runtime.NASDAQ, capture_id=f"close-{symbol.lower()}-test",
                generated_at="2026-10-07T16:00:05-04:00",
                actual_data_cutoff=facts[-1]["source_timestamp"],
                window={"start": "15:45", "end": "16:00"},
                feed_scope="nasdaq_public_chart_last_sale_volume_v1",
                qualified_facts=facts, missing_symbols=[],
            )
            refs.append({"path": path, "blob_sha": blob,
                         "kind": "regular_intraday_capture", "window": "15:45-16:00",
                         "provider": runtime.NASDAQ, "symbol": symbol})
        runtime.write_snapshot(
            self.store, stage="close", trade_date=self.DATE, snapshot_id="close-test",
            generated_at="2026-10-07T16:00:10-04:00", data_refs=refs,
            coverage={}, missing=list(missing),
            target_window={"start": "15:45", "end": "16:00"},
            actual_data_cutoff="2026-10-07T15:59:00-04:00",
        )
        return refs

    def selection(self):
        return entry._live_close_retry_symbols(self.store, self.DATE, self.ELIGIBLE)

    def invocation_args(self, mode):
        return ["--mode", mode, "--trade-date", self.DATE,
                "--store-root", str(self.store),
                "--universe", str(ROOT / "config/collection-universe.json"),
                "--config", str(ROOT / "config/market-data-store.yaml"),
                "--access-config", str(ROOT / "config/market-data-collector-access.yaml")]

    def invoke(self, mode):
        with mock.patch.object(runtime, "collect_regular_window", return_value={}) as collect:
            with contextlib.redirect_stdout(io.StringIO()):
                result = entry._run_live_close(self.invocation_args(mode))
        return result, collect

    def test_cold_retry_uses_actual_full_live_close_universe(self):
        universe = universe_owner.load_collection_universe(ROOT / "config/collection-universe.json")
        expected = sorted(symbol for symbol, _asset in entry._live_close_universe(universe))
        sectors = set(universe_owner.sector_symbols(universe))
        self.assertTrue(sectors)
        for mode in ("close_retry", "close_final"):
            with self.subTest(mode=mode):
                result, collect = self.invoke(mode)
                self.assertEqual(result, 0)
                self.assertEqual(collect.call_args.kwargs["symbols_override"], expected)
                self.assertTrue(sectors <= set(collect.call_args.kwargs["symbols_override"]))

    def test_empty_advisory_cache_cannot_suppress_absent_snapshot(self):
        runtime._write_close_state(self.store, self.DATE, "close", [])
        for mode in ("close_retry", "close_final"):
            with self.subTest(mode=mode):
                _result, collect = self.invoke(mode)
                self.assertTrue(collect.called)
                self.assertTrue(collect.call_args.kwargs["symbols_override"])

    def test_older_scope_snapshot_does_not_erase_sector_requirement(self):
        self.snapshot({"SPY": [self.fact("SPY")]})
        self.assertEqual(self.selection(), ["XLE"])

    def test_stale_cache_does_not_refetch_qualified_partial_capture(self):
        self.snapshot({symbol: [self.fact(symbol)] for symbol, _asset in self.ELIGIBLE})
        runtime._write_close_state(self.store, self.DATE, "close", ["SPY", "XLE"])
        self.assertEqual(self.selection(), [])

    def test_morning_capture_does_not_resolve_close_window(self):
        self.snapshot({"SPY": [self.fact("SPY", "2026-10-07T10:29:00-04:00")],
                       "XLE": [self.fact("XLE")]})
        self.assertEqual(self.selection(), ["SPY"])

    def test_timestamp_and_session_qualification_preserved(self):
        invalid = ["2026-10-06T15:59:00-04:00", "2026-10-07T16:00:00-04:00",
                   "2026-10-07T15:44:59-04:00", "2026-10-07T15:59:00", "invalid"]
        with mock.patch.object(entry.base, "_load_prior_snapshot", return_value=([{}], [])):
            for stamp in invalid:
                with self.subTest(stamp=stamp), mock.patch.object(
                    runtime, "_capture_facts", return_value={"SPY": [self.fact("SPY", stamp)]}
                ):
                    self.assertEqual(self.selection(), ["SPY", "XLE"])
            fact = self.fact("SPY")
            fact["session"] = "premarket"
            with mock.patch.object(runtime, "_capture_facts", return_value={"SPY": [fact]}):
                self.assertEqual(self.selection(), ["SPY", "XLE"])

    def test_window_start_and_equivalent_utc_are_qualified(self):
        with mock.patch.object(entry.base, "_load_prior_snapshot", return_value=([{}], [])):
            for stamp in ("2026-10-07T15:45:00-04:00", "2026-10-07T19:59:00Z"):
                with self.subTest(stamp=stamp), mock.patch.object(
                    runtime, "_capture_facts", return_value={"SPY": [self.fact("SPY", stamp)]}
                ):
                    self.assertEqual(self.selection(), ["XLE"])

    def test_capture_blob_mismatch_remains_failure(self):
        refs = self.snapshot({"SPY": [self.fact("SPY")]})
        path = self.store / refs[0]["path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            self.selection()

    def test_snapshot_blob_mismatch_remains_failure(self):
        self.snapshot({"SPY": [self.fact("SPY")]})
        pointer_path = self.store / f"snapshots/{self.DATE[:7]}/{self.DATE}/close/latest.json"
        pointer = json.loads(pointer_path.read_text())
        path = self.store / pointer["snapshot_path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            self.selection()

    def test_conflicting_missing_hint_is_not_silently_accepted(self):
        self.snapshot({"SPY": [self.fact("SPY")]}, missing=["SPY"])
        with self.assertRaises(runtime.CoverageContractError):
            self.selection()

    def assert_persisted_hint_rejected(self, hint):
        universe = universe_owner.load_collection_universe(ROOT / "config/collection-universe.json")
        eligible = entry._live_close_universe(universe)
        self.snapshot({symbol: [self.fact(symbol)] for symbol, _asset in eligible}, missing=[hint])
        _refs, persisted_missing = entry.base._load_prior_snapshot(self.store, self.DATE, "close")
        self.assertEqual(persisted_missing, [hint])
        before = {path.relative_to(self.store): path.read_bytes()
                  for path in self.store.rglob("*") if path.is_file()}
        for mode in ("close_retry", "close_final"):
            with self.subTest(mode=mode, hint=hint):
                output = io.StringIO()
                with mock.patch.object(runtime, "collect_regular_window") as collect:
                    with contextlib.redirect_stdout(output):
                        with self.assertRaisesRegex(runtime.CoverageContractError, "Close snapshot"):
                            entry._run_live_close(self.invocation_args(mode))
                    collect.assert_not_called()
                self.assertNotIn("nothing_missing", output.getvalue())
        after = {path.relative_to(self.store): path.read_bytes()
                 for path in self.store.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_lowercase_persisted_conflict_blocks_both_retry_modes(self):
        self.assert_persisted_hint_rejected("spy")

    def test_mixed_case_persisted_conflict_blocks_both_retry_modes(self):
        self.assert_persisted_hint_rejected("SpY")

    def test_out_of_scope_persisted_hint_blocks_both_retry_modes(self):
        self.assert_persisted_hint_rejected("not-in-eligible-universe")

    def test_empty_persisted_hint_blocks_both_retry_modes(self):
        self.assert_persisted_hint_rejected("")

    def test_padded_persisted_hint_is_not_trimmed_into_valid_symbol(self):
        self.assert_persisted_hint_rejected(" SPY ")

    def test_nonconflicting_lowercase_hint_keeps_absent_symbol_selection(self):
        self.snapshot({"XLE": [self.fact("XLE")]}, missing=["spy"])
        self.assertEqual(self.selection(), ["SPY"])
        universe = universe_owner.load_collection_universe(ROOT / "config/collection-universe.json")
        expected = sorted(symbol for symbol, _asset in entry._live_close_universe(universe)
                          if symbol != "XLE")
        for mode in ("close_retry", "close_final"):
            with self.subTest(mode=mode):
                result, collect = self.invoke(mode)
                self.assertEqual(result, 0)
                self.assertEqual(collect.call_args.kwargs["symbols_override"], expected)

    def test_initial_close_still_collects_full_scope(self):
        result, collect = self.invoke("close")
        self.assertEqual(result, 0)
        self.assertIsNone(collect.call_args.kwargs["symbols_override"])

    def test_nonclose_modes_do_not_enter_close_repair(self):
        for mode in ("open_15m", "open_30m", "open_60m", "previous_session_eod", "historical_context_repair"):
            with self.subTest(mode=mode):
                result, collect = self.invoke(mode)
                self.assertIsNone(result)
                collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
