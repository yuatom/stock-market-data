"""Static selector reaches only the exact pinned fixed-universe targets."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from unittest.mock import patch

import yaml

from test_guarded_history_recovery import RecoveryIntegrationTests, NOW, ROOT, git


class FixedGuardedRecoveryTests(RecoveryIntegrationTests):
    def fixed_selector(self, symbols):
        return {"schema_version": 1, "targets": [
            {"symbol": symbol, "index_blob_sha": git(
                self.repo, "rev-parse",
                f"{self.base}:data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json",
            )} for symbol in symbols
        ]}

    def fixed_recovery(self, selector, *, dynamic_selector=None, http=None):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)
        with patch.object(self.h.collectors, "datetime", Clock), \
             patch.object(self.h.collectors, "_http_json", side_effect=http or self.payload):
            return self.m.recover(
                self.h, compute=self.compute, compute_sha=self.compute_sha, repo=self.repo,
                store_sha=self.base, day="2026-10-18", request_id="456-1", config=self.config,
                universe=self.universe, access=self.access, key="SYNTHETIC", clock=lambda: NOW,
                fixed_selector=selector, dynamic_selector=dynamic_selector,
            )

    def test_singleton_recovery_archives_selected_target_and_preserves_canary(self):
        before_canary = git(self.repo, "rev-parse",
            f"{self.base}:data/market-data/series/daily/twelve_data_basic/BBB/_index.json")
        result = self.fixed_recovery(self.fixed_selector(["AAA"]))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["recovered_count"], 1)
        self.assertEqual(result["requests_attempted"], 1)
        final = result["store_commit_sha"]
        self.assertEqual(git(self.repo, "rev-parse",
            f"{final}:data/market-data/series/daily/twelve_data_basic/BBB/_index.json"), before_canary)
        archive = f"data/market-data/recovery/2026-10-18/twelve_data_basic/daily-baseline-rebase/AAA.json"
        self.assertEqual(json.loads(git(self.repo, "show", f"{final}:{archive}"))["old_records"], self.old)
        diagnostic = json.loads(git(self.repo, "show", f"{final}:{result['result_state_path']}"))
        self.assertEqual(diagnostic["fixed_selection"], self.fixed_selector(["AAA"]))

    def test_invalid_explicit_selection_and_dynamic_mix_stop_before_request_or_write(self):
        selector = self.fixed_selector(["AAA"])
        invalid = copy.deepcopy(selector)
        invalid["targets"][0]["index_blob_sha"] = "0" * 40
        for fixed, dynamic in ((invalid, None), (selector, {"targets": []})):
            with self.subTest(dynamic=dynamic is not None), self.assertRaises(Exception):
                self.fixed_recovery(fixed, dynamic_selector=dynamic)
            self.assertEqual(self.http_count, 0)
            self.assertEqual(git(self.store_remote, "rev-parse", "refs/heads/main"), self.base)
            self.assertFalse((self.repo / "data/market-data/collector-state/2026-10/2026-10-18-twelve-data-budget.json").exists())

    def test_admission_targets_match_the_same_fixed_subset(self):
        selector = self.fixed_selector(["BBB"])
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)
        writer = self.a.StorePublisher(self.repo, self.base, "2026-10-18", "789-1")
        with patch.object(self.h.collectors, "datetime", Clock), \
             patch.object(self.h.collectors, "_http_json", side_effect=self.payload):
            report, _ = self.a.execute(
                self.h, writer, compute=self.compute, compute_sha=self.compute_sha,
                day="2026-10-18", request_id="789-1", config=self.config,
                universe=self.universe, access=self.access, key="SYNTHETIC", clock=lambda: NOW,
                fixed_selector=selector,
            )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["target_count"], 1)
        self.assertEqual(report["results"][0]["symbol"], "BBB")
        self.assertEqual(report["fixed_selection"], selector)

    def test_workflow_exposes_fixed_selector_and_never_ignores_it(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/market-data-collector.yml").read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertIn("fixed_selection", inputs)
        self.assertIn("fixed_selection", workflow["jobs"]["run"]["if"])
        guard = workflow["jobs"]["selector_guard"]
        self.assertIn("workflow_dispatch", guard["if"])
        self.assertIn("fixed and dynamic", json.dumps(guard))
        self.assertIn("guarded daily baseline mode", json.dumps(guard))
        self.assertIn("--fixed-selection", json.dumps(workflow["jobs"]["recovery"]))
        self.assertIn("--fixed-selection", json.dumps(workflow["jobs"]["admission"]))
