from __future__ import annotations

import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import market_data_collection as runtime


class HistoricalContextScopeTest(unittest.TestCase):
    full = [(f"S{i:02d}", "etf") for i in range(33)]
    maintenance = full[17:]
    date = "2026-09-30"

    def facts(self, symbol, minutes=range(45, 60), hour="15"):
        return [{"symbol": symbol, "event_time": f"{self.date}T{hour}:{minute:02d}:00-04:00",
                 "source_timestamp": f"{self.date}T{hour}:{minute:02d}:00-04:00"}
                for minute in minutes]

    def run_collection(self, prior=None, new=None, prior_missing=(), mode=None):
        prior = {} if prior is None else prior
        new = {} if new is None else new
        old = copy.deepcopy(prior)
        refs = [{"path": f"prior/{symbol}.json", "blob_sha": "a" * 40, "symbol": symbol}
                for symbol in prior]
        requested = []

        def fetch(symbol, *_args, **_kwargs):
            requested.append(symbol)
            facts = copy.deepcopy(new.get(symbol, []))
            n = len(facts)
            return facts, {"raw_row_count": n, "timestamp_parseable_count": n,
                           "trade_date_match_count": n, "window_row_count": n,
                           "target_window_match_count": n}

        def capture(_root, **kwargs):
            return f"new/{kwargs['qualified_facts'][0]['symbol']}.json", "b" * 40

        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runtime.collection, "intraday_universe", return_value=self.full))
            stack.enter_context(mock.patch.object(runtime, "fetch_nasdaq_regular", side_effect=fetch))
            stack.enter_context(mock.patch.object(runtime, "fetch_twelve_regular", return_value=([], {"raw_row_count": 0})))
            stack.enter_context(mock.patch.object(runtime, "_decorate_context_facts", side_effect=lambda _u, f: list(f)))
            stack.enter_context(mock.patch.object(runtime.collection, "context_by_symbol", return_value={}))
            stack.enter_context(mock.patch.object(runtime.collection, "sector_symbols", return_value=[]))
            stack.enter_context(mock.patch.object(runtime.base, "_load_prior_snapshot", return_value=(refs, list(prior_missing))))
            stack.enter_context(mock.patch.object(runtime, "_capture_facts", return_value=prior))
            stack.enter_context(mock.patch.object(runtime, "_actual_cutoff", return_value=None))
            stack.enter_context(mock.patch.object(runtime, "write_capture", side_effect=capture))
            write = stack.enter_context(mock.patch.object(runtime, "write_snapshot", return_value=("snapshot.json", True)))
            result = runtime.collect_regular_window(
                mode=mode or runtime.HISTORICAL_CONTEXT_REPAIR, stage="close", trade_date=self.date,
                start_et="15:45", end_et="16:00", store_root=Path(tmp), universe_config={},
                config={}, access={"nasdaq_public_intraday": {"max_workers": 1}},
                symbols_override=[symbol for symbol, _asset in self.maintenance],
            )
            self.assertEqual(prior, old, "merging must not mutate inherited fact lists")
            self.assertEqual(sorted(requested), [symbol for symbol, _asset in self.maintenance])
            return result, write, refs

    def test_main_routes_maintenance_as_restricted_increment_not_full_scope(self):
        with mock.patch.object(runtime.collection, "load_collection_universe", return_value={}), \
             mock.patch.object(runtime.base, "load_yaml", return_value={}), \
             mock.patch.object(runtime.collection, "close_supported_baseline_universe", return_value=self.maintenance), \
             mock.patch.object(runtime, "collect_regular_window", return_value={}) as collect, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.main(["--mode", runtime.HISTORICAL_CONTEXT_REPAIR,
                                           "--trade-date", self.date, "--maintenance-authorized"]), 0)
        kwargs = collect.call_args.kwargs
        self.assertNotIn("eligible_universe", kwargs)
        self.assertEqual(kwargs["symbols_override"], [s for s, _ in self.maintenance])
        self.assertEqual((kwargs["stage"], kwargs["start_et"], kwargs["end_et"]), ("close", "15:45", "16:00"))

    def test_maintenance_authorization_still_required(self):
        with mock.patch.object(runtime.collection, "load_collection_universe", return_value={}), \
             mock.patch.object(runtime.base, "load_yaml", return_value={}), \
             mock.patch.object(runtime, "collect_regular_window") as collect:
            with self.assertRaisesRegex(RuntimeError, "maintenance-authorized"):
                runtime.main(["--mode", runtime.HISTORICAL_CONTEXT_REPAIR, "--trade-date", self.date])
            collect.assert_not_called()

    def test_existing_33_symbol_close_keeps_all_refs_with_16_symbol_increment(self):
        prior = {s: self.facts(s) for s, _ in self.full}
        new = {s: self.facts(s) for s, _ in self.maintenance}
        result, write, refs = self.run_collection(prior, new)
        coverage = result["coverage"]
        self.assertEqual(coverage["increment"]["requested_symbol_count"], 16)
        self.assertEqual(coverage["stage_snapshot"]["requested_symbol_count"], 33)
        self.assertEqual(coverage["stage_snapshot"]["qualified_symbol_count"], 33)
        self.assertEqual(result["snapshot_missing"], [])
        self.assertEqual(write.call_args.kwargs["data_refs"][:33], refs)
        self.assertEqual(len(write.call_args.kwargs["data_refs"]), 49)
        # Repeated minutes are not silently deduplicated into complete data.
        self.assertFalse(coverage["stage_snapshot"]["complete"])
        self.assertEqual(coverage["stage_snapshot"]["full_window_symbol_count"], 17)
        self.assertEqual(coverage["stage_snapshot"]["partial_symbols"], [s for s, _ in self.maintenance])

    def test_no_prior_close_marks_uncollected_symbols_missing(self):
        result, write, _ = self.run_collection(new={s: self.facts(s) for s, _ in self.maintenance})
        self.assertEqual(result["snapshot_missing"], [s for s, _ in self.full[:17]])
        self.assertEqual(result["coverage"]["stage_snapshot"]["qualified_symbol_count"], 16)
        self.assertFalse(result["coverage"]["stage_snapshot"]["complete"])
        self.assertEqual(write.call_args.kwargs["missing"], result["snapshot_missing"])

    def test_no_new_facts_does_not_erase_complete_prior_or_write_snapshot(self):
        result, write, _ = self.run_collection(prior={s: self.facts(s) for s, _ in self.full})
        self.assertEqual(result["status"], "no_new_qualified_facts")
        self.assertFalse(result["snapshot_written"])
        self.assertEqual(result["missing"], [s for s, _ in self.maintenance])
        self.assertEqual(result["snapshot_missing"], [])
        self.assertTrue(result["coverage"]["stage_snapshot"]["complete"])
        write.assert_not_called()

    def test_no_prior_and_no_new_is_explicit_missing_without_snapshot(self):
        result, write, _ = self.run_collection()
        self.assertEqual(result["snapshot_missing"], [s for s, _ in self.full])
        self.assertEqual(result["coverage"]["stage_snapshot"]["qualified_symbol_count"], 0)
        write.assert_not_called()

    def test_failed_refresh_does_not_mark_inherited_symbol_missing(self):
        prior = {s: self.facts(s) for s, _ in self.full}
        result, _, _ = self.run_collection(prior, {"S18": self.facts("S18")})
        self.assertIn("S17", result["missing"])
        self.assertNotIn("S17", result["snapshot_missing"])
        self.assertEqual(result["coverage"]["stage_snapshot"]["qualified_symbol_count"], 33)

    def test_only_window_valid_inherited_facts_supply_close_coverage(self):
        prior = {s: self.facts(s) for s, _ in self.full}
        prior["S00"] = self.facts("S00", range(0, 30), hour="10")
        result, _, _ = self.run_collection(prior, prior_missing=["S00"])
        self.assertEqual(result["snapshot_missing"], ["S00"])
        self.assertEqual(result["coverage"]["stage_snapshot"]["qualified_symbol_count"], 32)

    def test_partial_and_terminal_counts_remain_distinct(self):
        prior = {s: self.facts(s) for s, _ in self.full}
        prior["S00"] = self.facts("S00", range(45, 59))
        result, _, _ = self.run_collection(prior)
        stage = result["coverage"]["stage_snapshot"]
        self.assertEqual(stage["missing_symbols"], [])
        self.assertEqual(stage["partial_symbols"], ["S00"])
        self.assertEqual(stage["full_window_symbol_count"], 32)
        self.assertEqual(stage["terminal_observation_symbol_count"], 32)

    def test_unknown_inherited_symbol_is_still_rejected_even_outside_window(self):
        for hour in ("10", "15"):
            with self.subTest(hour=hour), self.assertRaisesRegex(runtime.CoverageContractError, "outside requested scope"):
                self.run_collection(prior={"UNKNOWN": self.facts("UNKNOWN", hour=hour)})

    def test_unknown_prior_missing_is_still_rejected(self):
        with self.assertRaisesRegex(runtime.CoverageContractError, "missing symbols must be requested"):
            self.run_collection(prior_missing=["UNKNOWN"])

    def test_contradictory_prior_missing_is_rejected(self):
        with self.assertRaisesRegex(runtime.CoverageContractError, "cannot mark a qualified symbol missing"):
            self.run_collection(prior={"S17": self.facts("S17")}, prior_missing=["S17"])

    def test_previously_empty_fact_list_can_be_filled_without_mutating_prior(self):
        result, _, _ = self.run_collection(prior={"S17": []}, new={"S17": self.facts("S17")}, prior_missing=["S17"])
        self.assertNotIn("S17", result["snapshot_missing"])
        self.assertEqual(result["coverage"]["stage_snapshot"]["qualified_symbol_count"], 1)

    def test_increment_override_outside_canonical_scope_rejected_before_fetch(self):
        with mock.patch.object(runtime.collection, "intraday_universe", return_value=self.full), \
             mock.patch.object(runtime, "fetch_nasdaq_regular") as fetch:
            with self.assertRaisesRegex(RuntimeError, "outside eligible"):
                runtime.collect_regular_window(mode=runtime.HISTORICAL_CONTEXT_REPAIR, stage="close",
                    trade_date=self.date, start_et="15:45", end_et="16:00", store_root=Path("unused"),
                    universe_config={}, config={}, access={}, symbols_override=["UNKNOWN"])
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
