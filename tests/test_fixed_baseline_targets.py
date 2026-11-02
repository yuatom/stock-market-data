from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fixed_baseline_targets as fixed


class Collection:
    def __init__(self, symbols):
        self.symbols = symbols

    def daily_universe(self, _universe):
        return [(symbol, "stocks") for symbol in self.symbols]

    def ticker_effective_at(self, _universe, _symbol):
        return None


class FixedBaselineTargetsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        self.store.mkdir()
        subprocess.run(["git", "init", "-q", str(self.store)], check=True)
        self.symbols = [f"S{i:02d}" for i in range(1, 34)]
        self.h = SimpleNamespace(collection=Collection(self.symbols))
        self.universe = {}
        self.blobs = {}
        for symbol in self.symbols:
            directory = self.store / fixed.INDEX_PREFIX / symbol
            directory.mkdir(parents=True)
            index = {
                "provider": "twelve_data_basic", "symbol": symbol, "asset_class": "stocks",
                "session": "regular", "interval": "1d",
                "series_semantics": "daily_regular_ohlcv",
                "adjustment_semantics": "provider_reported",
                "series_id": f"twelve_data_basic:{symbol}:regular:1d",
                "identity_effective_from": None,
            }
            (directory / "_index.json").write_text(json.dumps(index, sort_keys=True) + "\n")
        subprocess.run(["git", "-C", str(self.store), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.store), "-c", "user.name=fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture",
        ], check=True)
        self.store_sha = subprocess.check_output(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"], text=True,
        ).strip()
        for symbol in self.symbols:
            self.blobs[symbol] = subprocess.check_output([
                "git", "-C", str(self.store), "rev-parse",
                f"{self.store_sha}:{fixed.INDEX_PREFIX}/{symbol}/_index.json",
            ], text=True).strip()

    def selector(self, symbols):
        return {"schema_version": 1, "targets": [
            {"symbol": symbol, "index_blob_sha": self.blobs[symbol]} for symbol in symbols
        ]}

    def resolve(self, selector):
        return fixed.resolve_fixed_targets(
            self.h, store_repo=self.store, store_sha=self.store_sha,
            selector=selector, universe=self.universe,
        )

    def test_explicit_twenty_one_of_thirty_three_does_not_expand_to_other_twelve(self):
        selected = self.symbols[:21]
        resolved = self.resolve(self.selector(selected))
        self.assertEqual([item["symbol"] for item in resolved], selected)
        self.assertEqual(set(self.symbols) - {item["symbol"] for item in resolved}, set(self.symbols[21:]))
        self.assertEqual(len(resolved), 21)

    def test_singleton_and_multiple_targets_resolve_exactly(self):
        for selected in ([self.symbols[0]], [self.symbols[1], self.symbols[7], self.symbols[9]]):
            with self.subTest(selected=selected):
                self.assertEqual(
                    [item["symbol"] for item in self.resolve(self.selector(selected))], selected,
                )

    def test_omitted_selector_remains_omitted_for_existing_default_path(self):
        self.assertIsNone(fixed.parse_fixed_selection(None))

    def test_malformed_empty_duplicate_json_and_non_object_are_rejected(self):
        for raw in ("", "[]", "{}", '{"schema_version":1,"schema_version":1,"targets":[]}'):
            with self.subTest(raw=raw), self.assertRaises(fixed.FixedTargetError):
                parsed = fixed.parse_fixed_selection(raw)
                self.resolve(parsed)

    def test_empty_repeated_unknown_extra_and_bad_format_targets_are_rejected(self):
        cases = [
            {"schema_version": 1, "targets": []},
            self.selector([self.symbols[0], self.symbols[0]]),
            self.selector(["UNKNOWN"]),
            {"schema_version": 1, "targets": [{"symbol": "s01", "index_blob_sha": self.blobs[self.symbols[0]]}]},
            {"schema_version": 1, "targets": [
                {"symbol": self.symbols[0], "index_blob_sha": self.blobs[self.symbols[0]], "extra": True}
            ]},
            {"schema_version": True, "targets": [
                {"symbol": self.symbols[0], "index_blob_sha": self.blobs[self.symbols[0]]}
            ]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(fixed.FixedTargetError):
                self.resolve(value)

    def test_index_blob_and_index_identity_mismatches_fail_closed(self):
        wrong_blob = self.selector([self.symbols[0]])
        wrong_blob["targets"][0]["index_blob_sha"] = "0" * 40
        with self.assertRaises(fixed.FixedTargetError):
            self.resolve(wrong_blob)
        symbol = self.symbols[1]
        index_path = self.store / fixed.INDEX_PREFIX / symbol / "_index.json"
        index = json.loads(index_path.read_text())
        index["symbol"] = self.symbols[2]
        index_path.write_text(json.dumps(index, sort_keys=True) + "\n")
        subprocess.run(["git", "-C", str(self.store), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.store), "-c", "user.name=fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "bad identity",
        ], check=True)
        bad_head = subprocess.check_output(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"], text=True,
        ).strip()
        bad_blob = subprocess.check_output([
            "git", "-C", str(self.store), "rev-parse",
            f"{bad_head}:{fixed.INDEX_PREFIX}/{symbol}/_index.json",
        ], text=True).strip()
        value = {"schema_version": 1, "targets": [{"symbol": symbol, "index_blob_sha": bad_blob}]}
        with self.assertRaises(fixed.FixedTargetError):
            fixed.resolve_fixed_targets(
                self.h, store_repo=self.store, store_sha=bad_head,
                selector=value, universe=self.universe,
            )

    def test_pinned_store_head_must_match_request_and_fixed_dynamic_mix_is_rejected(self):
        with self.assertRaises(fixed.FixedTargetError):
            fixed.resolve_fixed_targets(
                self.h, store_repo=self.store, store_sha="0" * 40,
                selector=self.selector([self.symbols[0]]), universe=self.universe,
            )
        with self.assertRaises(fixed.FixedTargetError):
            fixed.validate_selection_mix(fixed_selector=self.selector([self.symbols[0]]),
                                         dynamic_selector={"targets": []})
        fixed.validate_selection_mix(fixed_selector=None, dynamic_selector={"targets": []})


if __name__ == "__main__":
    unittest.main()
