from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collection as collection


class CloseWindowScopeRegressionTest(unittest.TestCase):
    @staticmethod
    def _fact(symbol: str, hour: str, minute: int) -> dict[str, object]:
        stamp = f"2026-09-10T{hour}:{minute:02d}:00-04:00"
        return {
            "symbol": symbol,
            "asset_class": "stocks",
            "session": "regular",
            "event_time": stamp,
            "source_timestamp": stamp,
            "last_sale": 100.0,
            "reported_volume": 1.0,
        }

    @staticmethod
    def _diag(count: int) -> dict[str, object]:
        return {
            "provider": collection.NASDAQ,
            "raw_row_count": count,
            "timestamp_parseable_count": count,
            "trade_date_match_count": count,
            "window_row_count": count,
            "target_window_match_count": count,
        }

    @staticmethod
    def _no_rows_diag(provider: str) -> dict[str, object]:
        return {
            "provider": provider,
            "raw_row_count": 0,
            "timestamp_parseable_count": 0,
            "trade_date_match_count": 0,
            "window_row_count": 0,
            "target_window_match_count": 0,
        }

    def _write_prior_open60(self, root: Path, symbols: list[str]) -> None:
        refs: list[dict[str, object]] = []
        for symbol in symbols:
            facts = [self._fact(symbol, "10", minute) for minute in range(0, 30)]
            path, blob = collection.write_capture(
                root,
                trade_date="2026-09-10",
                session="regular",
                provider=collection.NASDAQ,
                capture_id=f"open_60m-{symbol.lower()}-prior",
                generated_at="2026-09-10T10:30:05-04:00",
                actual_data_cutoff="2026-09-10T10:29:00-04:00",
                window={"start": "10:00", "end": "10:30"},
                feed_scope="nasdaq_public_chart_last_sale_volume_v1",
                qualified_facts=facts,
                missing_symbols=[],
            )
            refs.append(
                {
                    "path": path,
                    "blob_sha": blob,
                    "kind": "regular_intraday_capture",
                    "window": "10:00-10:30",
                    "provider": collection.NASDAQ,
                    "symbol": symbol,
                    "reason_class": "success",
                }
            )
        collection.write_snapshot(
            root,
            stage="open_60m",
            trade_date="2026-09-10",
            snapshot_id="open_60m-prior",
            generated_at="2026-09-10T10:30:30-04:00",
            data_refs=refs,
            coverage={},
            missing=[],
            target_window={"start": "09:30", "end": "10:30"},
            actual_data_cutoff="2026-09-10T10:29:00-04:00",
        )

    def test_initial_close_missing_symbol_is_not_qualified_by_inherited_open60_facts(self) -> None:
        access = {"nasdaq_public_intraday": {"max_workers": 1}}
        universe = {"daily_series": [], "context_proxies": {}}
        eligible = [("AAA", "stocks"), ("BBB", "stocks")]

        def fake_nasdaq(symbol, *_args, **_kwargs):
            if symbol == "AAA":
                facts = [self._fact(symbol, "15", minute) for minute in range(45, 60)]
                return facts, self._diag(len(facts))
            return [], self._no_rows_diag(collection.NASDAQ)

        def fake_twelve(symbol, *_args, **_kwargs):
            return [], self._no_rows_diag(collection.TWELVE)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_prior_open60(root, ["AAA", "BBB"])
            with mock.patch.object(collection, "fetch_nasdaq_regular", side_effect=fake_nasdaq), mock.patch.object(
                collection, "fetch_twelve_regular", side_effect=fake_twelve
            ):
                result = collection.collect_regular_window(
                    mode="close",
                    stage="close",
                    trade_date="2026-09-10",
                    start_et="15:45",
                    end_et="16:00",
                    store_root=root,
                    universe_config=universe,
                    config={},
                    access=access,
                    eligible_universe=eligible,
                )

            stage = result["coverage"]["stage_snapshot"]
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["snapshot_missing"], ["BBB"])
            self.assertEqual(stage["qualified_symbol_count"], 1)
            self.assertEqual(stage["missing_symbols"], ["BBB"])
            self.assertEqual(stage["partial_symbols"], [])
            self.assertEqual(result["coverage"]["inherited_ref_count"], 2)
            self.assertTrue(result["snapshot_written"])
            persisted = json.loads((root / str(result["snapshot_path"])).read_text(encoding="utf-8"))
            self.assertEqual(persisted["missing"], ["BBB"])
            self.assertEqual(persisted["coverage"]["stage_snapshot"]["qualified_symbol_count"], 1)

    def test_initial_close_with_no_new_captures_returns_explicit_no_new_state(self) -> None:
        access = {"nasdaq_public_intraday": {"max_workers": 1}}
        universe = {"daily_series": [], "context_proxies": {}}
        eligible = [("AAA", "stocks"), ("BBB", "stocks")]

        def fake_nasdaq(symbol, *_args, **_kwargs):
            return [], self._no_rows_diag(collection.NASDAQ)

        def fake_twelve(symbol, *_args, **_kwargs):
            return [], self._no_rows_diag(collection.TWELVE)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_prior_open60(root, ["AAA", "BBB"])
            with mock.patch.object(collection, "fetch_nasdaq_regular", side_effect=fake_nasdaq), mock.patch.object(
                collection, "fetch_twelve_regular", side_effect=fake_twelve
            ):
                result = collection.collect_regular_window(
                    mode="close",
                    stage="close",
                    trade_date="2026-09-10",
                    start_et="15:45",
                    end_et="16:00",
                    store_root=root,
                    universe_config=universe,
                    config={},
                    access=access,
                    eligible_universe=eligible,
                )

            stage = result["coverage"]["stage_snapshot"]
            self.assertEqual(result["status"], "no_new_qualified_facts")
            self.assertFalse(result["snapshot_written"])
            self.assertEqual(result["snapshot_missing"], ["AAA", "BBB"])
            self.assertEqual(stage["qualified_symbol_count"], 0)
            self.assertEqual(stage["missing_symbols"], ["AAA", "BBB"])
            self.assertEqual(stage["partial_symbols"], [])
            self.assertEqual(result["coverage"]["inherited_ref_count"], 2)
            self.assertEqual(collection._read_close_state(root, "2026-09-10"), ["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
