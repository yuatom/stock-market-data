"""Issue #166: native Snapshot producer/schema contract regression tests.

Only synthetic objects are used. Never rewrite or reinterpret immutable Store facts.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collection as collector  # noqa: E402


class SnapshotStageAndReasonContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema = json.loads(
            (ROOT / "schemas/market-data-stage-snapshot.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        cls.schema = schema
        cls.validator = Draft202012Validator(schema, format_checker=FormatChecker())
        cls.dynamic_owner = yaml.safe_load(
            (ROOT / "config/dynamic-candidate-collection.yaml").read_text(encoding="utf-8")
        )

    @staticmethod
    def _snapshot(stage="open_30m", coverage=None):
        return {
            "schema_version": 1,
            "snapshot_id": "synthetic-issue166-snapshot",
            "stage": stage,
            "trade_date": "2026-10-30",
            "generated_at": "2026-10-30T10:00:01-04:00",
            "coverage": coverage if coverage is not None else {"reason_classes": {"AAA": ["success"]}},
            "data_refs": [],
            "missing": [],
        }

    def test_schema_stages_equal_registered_owner_stages(self):
        expected_core = {"premarket", "open_15m", "open_30m", "open_60m", "close"}
        dynamic = self.dynamic_owner
        prefix = dynamic["collection"]["intraday"]["dedicated_snapshot_stage_prefix"]
        expected_dynamic = {prefix + stage for stage in dynamic["request"]["allowed_stages"]}
        declared = set(self.schema["properties"]["stage"]["enum"])
        self.assertEqual(declared, expected_core | expected_dynamic)
        for stage in sorted(declared):
            with self.subTest(stage=stage):
                self.validator.validate(self._snapshot(stage=stage))

    def test_unknown_or_unowned_stages_still_fail_closed(self):
        for stage in ("discovery_premarket", "discovery_unknown", "open_75m", "close_retry"):
            with self.subTest(stage=stage):
                self.assertTrue(list(self.validator.iter_errors(self._snapshot(stage=stage))))

    def test_raw_duplicate_reason_class_is_not_schema_valid(self):
        # Existing immutable historical objects that violate uniqueItems remain invalid.
        historical = self._snapshot(coverage={"reason_classes": {"AAA": ["success", "success"]}})
        self.assertTrue(list(self.validator.iter_errors(historical)))

    def test_new_snapshot_coverage_deduplicates_top_level_and_nested_reasons(self):
        readiness = {
            "semantics": "collector_observed_after_prewarm_not_github_queue_or_remote_store_commit_ready",
            "target_window_end": "2026-10-30T10:00:00-04:00",
            "collection_started_at": "2026-10-30T10:00:01-04:00",
            "collection_completed_at": "2026-10-30T10:00:02-04:00",
            "collection_started_after_target_seconds": 1.0,
            "collection_completed_after_target_seconds": 2.0,
            "snapshot_generated_at": None,
            "snapshot_ready_at": None,
            "window_end_to_snapshot_ready_seconds": None,
        }
        coverage = collector._coverage_observability(
            full_universe=[("AAA", "stocks")],
            increment_universe=[("AAA", "stocks")],
            capture_refs=[],
            missing=[],
            snapshot_missing=[],
            prior_refs=[],
            provider_counts={},
            readiness=readiness,
            reason_classes={"AAA": ["success", "success", "outside_window", "success"]},
        )
        wanted = ["outside_window", "success"]
        for scope in (coverage, coverage["increment"], coverage["stage_snapshot"]):
            self.assertEqual(scope["reason_classes"]["AAA"], wanted)
        self.validator.validate(self._snapshot(coverage=coverage))


if __name__ == "__main__":
    unittest.main()
