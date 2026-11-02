"""Strict, non-authoritative projection of legacy Snapshot diagnostics."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402
import legacy_snapshot_compat as compat  # noqa: E402


class LegacySnapshotCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_path = ROOT / "schemas/market-data-stage-snapshot.schema.json"
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())
        cls.schema_sha = hashlib.sha1(schema_path.read_bytes()).hexdigest()

    @staticmethod
    def _snapshot(reason_classes=None):
        return {
            "schema_version": 1,
            "snapshot_id": "synthetic-issue166-legacy-snapshot",
            "stage": "open_30m",
            "trade_date": "2026-10-30",
            "generated_at": "2026-10-30T10:00:01-04:00",
            "coverage": {"reason_classes": {"AAA": reason_classes or ["success"]}},
            "data_refs": [],
            "missing": [],
        }

    def _transform(self, document, source_path="data/market-data/snapshots/2026-10/2026-10-30/open_30m/test.json"):
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
        blob = compat.git_blob_sha_bytes(raw)
        return compat.transform_snapshot_bytes(
            raw,
            original_path=source_path,
            expected_original_blob_sha=blob,
            original_schema_sha="2b827576024c9d27af4b589583720fefef1ffa58",
            original_schema_validator=self.validator,
            current_schema_sha=self.schema_sha,
            current_schema_validator=self.validator,
            transformed_at="2026-11-02T12:00:00Z",
            store_commit="bbef1a377ff5a2a7a17d2342a11fb002ff76b9df",
            transformer_commit="a" * 40,
        )

    def test_deduplicates_and_sorts_only_reason_classes_and_validates(self):
        before = self._snapshot(["success", "outside_window", "success"])
        derived_bytes, manifest = self._transform(before)
        derived = json.loads(derived_bytes)
        self.assertEqual(derived["coverage"]["reason_classes"]["AAA"], ["outside_window", "success"])
        self.assertEqual(derived["stage"], before["stage"])
        self.assertEqual(derived["trade_date"], before["trade_date"])
        self.assertEqual(derived["generated_at"], before["generated_at"])
        self.assertEqual(manifest["exact_changed_json_pointers"], ["/coverage/reason_classes/AAA"])
        self.assertEqual(manifest["before_values"], {"/coverage/reason_classes/AAA": ["success", "outside_window", "success"]})
        self.assertEqual(manifest["after_values"], {"/coverage/reason_classes/AAA": ["outside_window", "success"]})
        self.assertTrue(manifest["non_authoritative"])
        self.validator.validate(derived)
        manifest_schema = json.loads((ROOT / "schemas/legacy-snapshot-compatibility-manifest.schema.json").read_text())
        Draft202012Validator.check_schema(manifest_schema)
        Draft202012Validator(manifest_schema, format_checker=FormatChecker()).validate(manifest)

    def test_repeated_execution_is_byte_stable_and_idempotent(self):
        source = self._snapshot(["success", "success", "outside_window"])
        first, first_manifest = self._transform(source)
        second, second_manifest = self._transform(source)
        self.assertEqual(first, second)
        self.assertEqual(first_manifest["derived_content_sha256"], second_manifest["derived_content_sha256"])
        self.assertEqual(
            compat.canonicalize_reason_classes(json.loads(first)),
            json.loads(first),
        )

    def test_rejects_wrong_original_git_blob(self):
        source = self._snapshot(["success", "success"])
        raw = json.dumps(source, separators=(",", ":")).encode()
        with self.assertRaises(compat.CompatibilityTransformError):
            compat.transform_snapshot_bytes(
                raw,
                original_path="data/market-data/snapshots/2026-10/2026-10-30/open_30m/test.json",
                expected_original_blob_sha="0" * 40,
                original_schema_sha="2b827576024c9d27af4b589583720fefef1ffa58",
                original_schema_validator=self.validator,
                current_schema_sha=self.schema_sha,
                current_schema_validator=self.validator,
                transformed_at="2026-11-02T12:00:00Z",
                transformer_commit="a" * 40,
            )

    def test_rejects_unknown_reason_enum(self):
        with self.assertRaises(compat.CompatibilityTransformError):
            self._transform(self._snapshot(["success", "not_a_supported_reason", "not_a_supported_reason"]))

    def test_rejects_non_reason_schema_error_instead_of_coercing(self):
        source = self._snapshot(["success", "success"])
        source["trade_date"] = "not-a-date"
        with self.assertRaises(compat.CompatibilityTransformError):
            self._transform(source)

    def test_field_diff_verifier_rejects_non_reason_change(self):
        before = self._snapshot(["success", "success"])
        after = json.loads(json.dumps(before))
        after["coverage"]["reason_classes"]["AAA"] = ["success"]
        after["trade_date"] = "2026-10-29"
        with self.assertRaises(compat.CompatibilityTransformError):
            compat.verify_allowed_diff(before, after, ["/coverage/reason_classes/AAA"])

    def test_rejects_invalid_original_data_ref_without_coercion(self):
        source = self._snapshot(["success", "success"])
        source["data_refs"] = [{"path": "data/market-data/captures/missing.json"}]
        with self.assertRaises(compat.CompatibilityTransformError):
            self._transform(source)

    def test_rejects_non_snapshot_and_latest_alias_paths(self):
        for path in (
            "../latest.json",
            "data/market-data/snapshots/2026-10/2026-10-30/open_30m/latest.json",
            "data/market-data/sessions/2026-10/capture.json",
        ):
            with self.subTest(path=path), self.assertRaises(compat.CompatibilityTransformError):
                self._transform(self._snapshot(["success", "success"]), source_path=path)


if __name__ == "__main__":
    unittest.main()
