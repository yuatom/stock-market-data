"""Static selector reaches only the exact pinned fixed-universe targets."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch

import yaml
import test_guarded_history_recovery as recovery


class FixedGuardedRecoveryTests(recovery.RecoveryIntegrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.universe["daily_series"].append({"symbol": "CCC", "asset_class": "etf"})
        self.h.store.append_daily_bars(
            self.repo / "data/market-data", provider="twelve_data_basic",
            symbol="CCC", asset_class="etf", bars=self.old,
            series_semantics="daily_regular_ohlcv", shard_record_limit=2,
        )
        recovery.git(self.repo, "add", ".")
        recovery.git(self.repo, "commit", "-m", "seed unselected canary")
        recovery.git(self.repo, "push", "origin", "main")
        self.base = recovery.git(self.repo, "rev-parse", "HEAD")

    def fixed_selector(self, symbols):
        return {"schema_version": 1, "targets": [
            {"symbol": symbol, "index_blob_sha": recovery.git(
                self.repo, "rev-parse",
                f"{self.base}:data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json",
            )} for symbol in symbols
        ]}

    def series_subtree(self, commit, symbol):
        prefix = f"data/market-data/series/daily/twelve_data_basic/{symbol}"
        paths = recovery.git(self.repo, "ls-tree", "-r", "--name-only",
                             commit, "--", prefix).splitlines()
        return {path: recovery.git(self.repo, "rev-parse", f"{commit}:{path}") for path in paths}

    def fixed_recovery(self, selector, *, dynamic_selector=None, http=None):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return recovery.NOW if tz is None else recovery.NOW.astimezone(tz)
        with patch.object(self.h.collectors, "datetime", Clock), \
             patch.object(self.h.collectors, "_http_json", side_effect=http or self.payload):
            return self.m.recover(
                self.h, compute=self.compute, compute_sha=self.compute_sha, repo=self.repo,
                store_sha=self.base, day=recovery.DAY, request_id="456-1", config=self.config,
                universe=self.universe, access=self.access, key="SYNTHETIC",
                clock=lambda: recovery.NOW, fixed_selector=selector,
                dynamic_selector=dynamic_selector,
            )

    def test_singleton_recovery_preserves_every_unselected_series_blob(self):
        selected = ["AAA"]
        unselected = ["BBB", "CCC"]
        before = {symbol: self.series_subtree(self.base, symbol) for symbol in unselected}
        result = self.fixed_recovery(self.fixed_selector(selected))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["recovered_count"], 1)
        self.assertEqual(result["requests_attempted"], 1)
        final = result["store_commit_sha"]
        for symbol in unselected:
            self.assertEqual(self.series_subtree(final, symbol), before[symbol])
        archive = (
            f"data/market-data/recovery/{recovery.DAY}/twelve_data_basic/"
            f"daily-baseline-rebase/AAA.json"
        )
        self.assertEqual(
            json.loads(recovery.git(self.repo, "show", f"{final}:{archive}"))["old_records"],
            self.old,
        )
        diagnostic = json.loads(
            recovery.git(self.repo, "show", f"{final}:{result['result_state_path']}")
        )
        self.assertEqual(diagnostic["fixed_selection"], self.fixed_selector(selected))

    def test_one_selected_of_three_uses_one_remaining_credit(self):
        self.seed_budget(199)
        result = self.fixed_recovery(self.fixed_selector(['AAA']))
        self.assertEqual((result['status'], result['recovered_count']), ('PASS', 1))
        self.assertEqual(self.http_count, 1)
        self.assertEqual(self.current_budget()['credits'], 200)

    def test_two_selected_with_one_credit_block_without_spending(self):
        self.seed_budget(199)
        with self.assertRaises(Exception) as caught:
            self.fixed_recovery(self.fixed_selector(['AAA', 'BBB']))
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 199)
        self.assert_no_remote_facts()

    def assert_no_fixed_facts_published(self, unselected_before):
        self.assert_no_remote_facts()
        head = recovery.git(self.store_remote, "rev-parse", "refs/heads/main")
        for symbol, subtree in unselected_before.items():
            self.assertEqual(self.series_subtree(head, symbol), subtree)

    def test_late_provider_rejection_for_selected_subset_never_publishes_partial_history(self):
        selected = ["AAA", "BBB"]
        unselected_before = self.series_subtree(self.base, "CCC")

        request_calls = 0

        def reject_second(url, **kwargs):
            nonlocal request_calls
            request_calls += 1
            if request_calls == 2:
                raise RuntimeError("synthetic second-target Provider failure")
            return self.payload(url, **kwargs)

        with self.assertRaises(Exception):
            self.fixed_recovery(self.fixed_selector(selected), http=reject_second)
        self.assertEqual(request_calls, 2)
        self.assertEqual(self.http_count, 1)
        self.assert_no_fixed_facts_published({"CCC": unselected_before})

    def test_late_invalid_selected_replacement_never_publishes_partial_history(self):
        selected = ["AAA", "BBB"]
        unselected_before = self.series_subtree(self.base, "CCC")

        def reject_second(url, **kwargs):
            value = self.payload(url, **kwargs)
            if self.http_count == 2:
                value["values"][0]["volume"] = "-1"
            return value

        with self.assertRaises(Exception):
            self.fixed_recovery(self.fixed_selector(selected), http=reject_second)
        self.assertEqual(self.http_count, 2)
        self.assert_no_fixed_facts_published({"CCC": unselected_before})

    def test_unselected_series_path_injection_blocks_selected_subset_publication(self):
        selected = ["AAA", "BBB"]
        unselected_before = self.series_subtree(self.base, "CCC")
        original = self.h.rebaser._replace_series

        def inject_unselected_path(**kwargs):
            result = original(**kwargs)
            injected = kwargs["store_root"] / "series/daily/twelve_data_basic/CCC/injected.json"
            injected.write_text('{"unexpected":true}\n', encoding="utf-8")
            return result

        with patch.object(self.h.rebaser, "_replace_series", side_effect=inject_unselected_path):
            with self.assertRaises(Exception):
                self.fixed_recovery(self.fixed_selector(selected))
        self.assertEqual(self.http_count, 2)
        self.assert_no_fixed_facts_published({"CCC": unselected_before})

    def test_successful_multi_target_subset_has_exact_immutable_output_closure(self):
        selected = ["AAA", "BBB"]
        unselected_before = self.series_subtree(self.base, "CCC")
        result = self.fixed_recovery(self.fixed_selector(selected))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["recovered_count"], len(selected))
        final = result["store_commit_sha"]
        self.assertEqual(
            recovery.git(self.store_remote, "rev-parse", f"refs/tags/market-data-read/{final}"),
            final,
        )
        self.assertEqual(self.series_subtree(final, "CCC"), unselected_before)

        expected = {
            recovery.module().INVENTORY,
            result["result_state_path"],
        }
        for symbol in selected:
            index_path = (
                f"data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json"
            )
            index = json.loads(recovery.git(self.repo, "show", f"{final}:{index_path}"))
            expected.add(index_path)
            expected.update(
                f"data/market-data/series/daily/twelve_data_basic/{symbol}/{shard['path']}"
                for shard in index["shards"]
            )
            expected.add(
                f"data/market-data/recovery/{recovery.DAY}/twelve_data_basic/"
                f"daily-baseline-rebase/{symbol}.json"
            )
        receipt = result["verified_required_outputs"]
        actual = {item["path"] for item in receipt}
        self.assertEqual(actual, expected)
        for item in receipt:
            self.assertEqual(
                recovery.git(self.repo, "rev-parse", f"{final}:{item['path']}"),
                item["blob_sha"],
            )
        self.assertFalse(any("/CCC/" in path for path in actual))

    def test_invalid_selection_and_fixed_dynamic_mix_stop_before_request_or_write(self):
        selector = self.fixed_selector(["AAA"])
        invalid = copy.deepcopy(selector)
        invalid["targets"][0]["index_blob_sha"] = "0" * 40
        for fixed, dynamic in ((invalid, None), (selector, {"targets": []})):
            with self.subTest(dynamic=dynamic is not None), self.assertRaises(Exception):
                self.fixed_recovery(fixed, dynamic_selector=dynamic)
            self.assertEqual(self.http_count, 0)
            self.assertEqual(
                recovery.git(self.store_remote, "rev-parse", "refs/heads/main"), self.base
            )
            self.assertFalse((
                self.repo / "data/market-data/collector-state/2026-10/"
                "2026-10-18-twelve-data-budget.json"
            ).exists())

    def test_admission_targets_match_the_same_fixed_subset(self):
        selector = self.fixed_selector(["BBB"])
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return recovery.NOW if tz is None else recovery.NOW.astimezone(tz)
        writer = self.a.StorePublisher(self.repo, self.base, recovery.DAY, "789-1")
        with patch.object(self.h.collectors, "datetime", Clock), \
             patch.object(self.h.collectors, "_http_json", side_effect=self.payload):
            report, _ = self.a.execute(
                self.h, writer, compute=self.compute, compute_sha=self.compute_sha,
                day=recovery.DAY, request_id="789-1", config=self.config,
                universe=self.universe, access=self.access, key="SYNTHETIC",
                clock=lambda: recovery.NOW, fixed_selector=selector,
            )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["target_count"], 1)
        self.assertEqual(report["results"][0]["symbol"], "BBB")
        self.assertEqual(report["fixed_selection"], selector)

    def test_workflow_exposes_fixed_selector_and_never_ignores_it(self):
        workflow = yaml.safe_load(
            (recovery.ROOT / ".github/workflows/market-data-collector.yml").read_text()
        )
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertIn("fixed_selection", inputs)
        self.assertIn("fixed_selection", workflow["jobs"]["run"]["if"])
        guard = workflow["jobs"]["selector_guard"]
        self.assertIn("workflow_dispatch", guard["if"])
        self.assertIn("fixed and dynamic", json.dumps(guard))
        self.assertIn("guarded daily baseline mode", json.dumps(guard))
        self.assertIn("--fixed-selection", json.dumps(workflow["jobs"]["recovery"]))
        self.assertIn("--fixed-selection", json.dumps(workflow["jobs"]["admission"]))
