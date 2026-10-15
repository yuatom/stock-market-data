"""Offline maintenance regressions; provider I/O is mocked, Store writes are real."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import market_data_store as store
import rebase_market_data_baseline as rebaser

PROVIDER = "twelve_data_basic"
SYMBOL = "TEST"


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


def fixture_bars(start="2024-01-01", end="2026-10-15"):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    result = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        if day.weekday() < 5:
            value = 100.0 + len(result)
            result.append({"trade_date": day.isoformat(), "open": value,
                           "high": value + 2, "low": value - 2,
                           "close": value + 1, "volume": 1000.0})
    return result


class DailyBaselineRebaseWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider_rows = fixture_bars()
        self.existing = [copy.deepcopy(row) for row in self.provider_rows
                         if row["trade_date"] <= "2026-08-14"][-256:]
        self.effective = None
        self.config = {
            "series": {"analysis_windows_may_read_last_sessions": [20, 256],
                       "baseline_initialization": {"explicit_rebase": {
                           "maximum_revised_overlap_records": 3,
                           "revised_overlap_must_be_within_last_existing_sessions": 3}}},
            "collector": {"budgets": {"twelve_data_basic": {
                "hard_credits_per_day": 200, "hard_credits_per_minute": 8}}},
        }
        self.index_path = self.root / "series/daily" / PROVIDER / SYMBOL / "_index.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "unit-test-only"}))
        self.stack.enter_context(patch.object(rebaser, "datetime", FrozenDateTime))
        self.stack.enter_context(patch.object(rebaser.collection, "load_collection_universe", return_value={}))
        self.stack.enter_context(patch.object(rebaser.collection, "daily_universe", return_value=[(SYMBOL, "etf")]))
        self.stack.enter_context(patch.object(rebaser.collection, "ticker_effective_at", side_effect=lambda *a: self.effective))
        self.stack.enter_context(patch.object(rebaser.collectors, "load_yaml", side_effect=lambda path: self.config if str(path) == "store.yaml" else {}))
        self.wait = self.stack.enter_context(patch.object(rebaser.collectors, "wait_for_twelve_budget"))
        self.charge = self.stack.enter_context(patch.object(rebaser.collectors, "consume_twelve_credit"))
        self.fetch = self.stack.enter_context(patch.object(rebaser.collectors, "fetch_twelve_daily", side_effect=self.fetch_latest))

    def fetch_latest(self, symbol, key, access, *, outputsize):
        self.assertEqual(symbol, SYMBOL)
        self.assertGreaterEqual(outputsize, 1)
        self.assertLessEqual(outputsize, 5000)
        return copy.deepcopy(self.provider_rows[-outputsize:])

    def seed(self):
        store.append_daily_bars(self.root, provider=PROVIDER, symbol=SYMBOL,
                                asset_class="etf", bars=self.existing,
                                identity_effective_from=self.effective,
                                series_semantics="daily_regular_ohlcv")
        self.old_index = json.loads(self.index_path.read_text())
        self.old_bytes = self.all_bytes()

    def all_bytes(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def run_rebase(self, trade_date="2026-10-15"):
        return rebaser.rebase(trade_date=trade_date, store_root=self.root,
                              universe_path=Path("universe.json"),
                              store_config_path=Path("store.yaml"),
                              access_path=Path("access.yaml"))

    def assert_rejected_without_write(self, result, detail=None):
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["results"], [])
        self.assertEqual(self.all_bytes(), self.old_bytes)
        if detail:
            self.assertIn(detail, result["failures"][0]["detail"])

    def assert_success_preserves_archive(self, result):
        self.assertEqual(result["status"], "ok", result)
        archive = json.loads((self.root / result["results"][0]["recovery_archive_path"]).read_text())
        self.assertEqual(archive["old_records"], self.existing)
        self.assertEqual(archive["old_index"], self.old_index)
        rows = store.read_daily_series(self.root, provider=PROVIDER, symbol=SYMBOL)
        self.assertTrue({row["trade_date"] for row in self.existing} <= {row["trade_date"] for row in rows})
        self.fetch.assert_called_once()
        self.charge.assert_called_once()
        return rows

    def test_stale_full_baseline_keeps_every_old_date_and_new_rows(self):
        self.provider_rows = self.provider_rows[:-1]  # isolate missing retained dates
        self.seed()
        rows = self.assert_success_preserves_archive(self.run_rebase())
        expected = [row for row in self.provider_rows
                    if self.existing[0]["trade_date"] <= row["trade_date"] < "2026-10-15"]
        self.assertEqual(rows, expected)
        self.assertGreater(len(rows), 256)

    def test_current_bar_does_not_reduce_eligible_target_to_255(self):
        self.existing = copy.deepcopy(self.provider_rows[-257:-1])
        self.seed()
        rows = self.assert_success_preserves_archive(self.run_rebase())
        self.assertEqual(rows, self.existing)
        self.assertEqual(len(rows), 256)

    def test_historical_cutoff_excludes_all_later_provider_rows(self):
        self.seed()
        rows = self.assert_success_preserves_archive(self.run_rebase("2026-09-01"))
        self.assertTrue(all(row["trade_date"] < "2026-09-01" for row in rows))
        self.assertEqual(rows[0], self.existing[0])

    def test_real_tail_revision_is_archived_and_remains_bounded(self):
        self.seed()
        changed_date = self.existing[-1]["trade_date"]
        next(row for row in self.provider_rows if row["trade_date"] == changed_date)["volume"] += 1
        result = self.run_rebase()
        self.assert_success_preserves_archive(result)
        self.assertEqual(result["results"][0]["revised_trade_dates"], [changed_date])
        archive = json.loads((self.root / result["results"][0]["recovery_archive_path"]).read_text())
        self.assertEqual(archive["revisions"][0]["changed_fields"], {"volume": {"old": 1000.0, "new": 1001.0}})

    def test_old_revision_still_rejected_without_archive_or_rewrite(self):
        self.seed()
        next(row for row in self.provider_rows if row["trade_date"] == self.existing[0]["trade_date"])["close"] += 1
        self.assert_rejected_without_write(self.run_rebase(), "outside the authorized settlement tail")

    def test_revision_count_limit_is_not_relaxed(self):
        self.config["series"]["baseline_initialization"]["explicit_rebase"]["maximum_revised_overlap_records"] = 2
        self.seed()
        tail = {row["trade_date"] for row in self.existing[-3:]}
        for row in self.provider_rows:
            if row["trade_date"] in tail:
                row["close"] += 1
        self.assert_rejected_without_write(self.run_rebase(), "exceeds explicit-rebase limit")

    def test_provider_truncation_cannot_drop_retained_history(self):
        self.seed()
        self.fetch.side_effect = lambda *a, **k: copy.deepcopy(self.provider_rows[-256:])
        self.assert_rejected_without_write(self.run_rebase())

    def test_provider_missing_one_old_date_is_rejected(self):
        self.seed()
        missing = self.existing[10]["trade_date"]
        self.provider_rows = [row for row in self.provider_rows if row["trade_date"] != missing]
        self.assert_rejected_without_write(self.run_rebase(), "does not cover existing dates")

    def test_duplicate_retained_provider_date_is_rejected(self):
        self.seed()
        self.provider_rows.append(copy.deepcopy(self.existing[-1]))
        self.provider_rows.sort(key=lambda row: row["trade_date"])
        self.assert_rejected_without_write(self.run_rebase(), "duplicate trade_date")

    def test_oversized_history_fails_before_spending_credit(self):
        self.existing = fixture_bars("2000-01-03", "2000-01-31")
        self.seed()
        self.assert_rejected_without_write(self.run_rebase(), "5000")
        self.wait.assert_not_called()
        self.charge.assert_not_called()
        self.fetch.assert_not_called()

    def test_corrupt_stored_shard_fails_before_provider_access(self):
        self.seed()
        shard = self.index_path.parent / self.old_index["shards"][0]["path"]
        doc = json.loads(shard.read_text())
        doc["records"][0]["close"] += 1
        shard.write_bytes(store.canonical_bytes(doc) + b"\n")
        self.old_bytes = self.all_bytes()
        self.assert_rejected_without_write(self.run_rebase(), "shard integrity mismatch")
        self.charge.assert_not_called()
        self.fetch.assert_not_called()

    def test_identity_limited_series_stays_after_effective_date(self):
        self.effective = "2026-10-01"
        self.existing = [copy.deepcopy(row) for row in self.provider_rows
                         if self.effective <= row["trade_date"] < "2026-10-15"]
        self.seed()
        result = self.run_rebase()
        self.assertEqual(self.assert_success_preserves_archive(result), self.existing)
        self.assertTrue(result["results"][0]["identity_limited"])

    def test_short_existing_history_extends_only_to_target(self):
        self.existing = copy.deepcopy(self.provider_rows[-21:-1])
        self.seed()
        rows = self.assert_success_preserves_archive(self.run_rebase())
        self.assertEqual(rows, self.provider_rows[-257:-1])

    def test_smaller_target_never_truncates_existing_series(self):
        self.config["series"]["analysis_windows_may_read_last_sessions"] = [20]
        self.seed()
        rows = self.assert_success_preserves_archive(self.run_rebase())
        self.assertEqual(rows[0], self.existing[0])
        self.assertGreater(len(rows), 256)

    def test_request_size_is_preserved_in_lineage(self):
        self.seed()
        self.assert_success_preserves_archive(self.run_rebase())
        index = json.loads(self.index_path.read_text())
        self.assertEqual(index["lineage_segments"][0]["history_outputsize"], self.fetch.call_args.kwargs["outputsize"])
        self.assertEqual(index["baseline_initialization"]["target_records"], 256)

    def test_future_target_is_rejected_before_provider_access(self):
        self.seed()
        self.assert_rejected_without_write(self.run_rebase("2026-10-16"), "future")
        self.fetch.assert_not_called()

    def test_provider_semantics_mismatch_is_rejected_before_provider(self):
        self.seed()
        index = json.loads(self.index_path.read_text())
        index["adjustment_semantics"] = "different"
        self.index_path.write_text(json.dumps(index))
        self.old_bytes = self.all_bytes()
        self.assert_rejected_without_write(self.run_rebase(), "semantic mismatch")
        self.fetch.assert_not_called()

    def test_existing_archive_is_not_replaced(self):
        self.seed()
        archive = self.root / f"recovery/2026-10-15/{PROVIDER}/daily-baseline-rebase/{SYMBOL}.json"
        archive.parent.mkdir(parents=True)
        archive.write_text('{"preserve":true}\n')
        self.old_bytes = self.all_bytes()
        self.assert_rejected_without_write(self.run_rebase(), "archive already exists")

    def test_nonfinite_new_row_cannot_create_archive(self):
        self.seed()
        self.provider_rows[-2]["close"] = float("nan")
        self.assert_rejected_without_write(self.run_rebase())

    def test_missing_secret_does_not_fetch_or_write(self):
        self.seed()
        with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": ""}):
            result = self.run_rebase()
        self.assertEqual(result["status"], "secret_missing")
        self.assertEqual(self.all_bytes(), self.old_bytes)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
