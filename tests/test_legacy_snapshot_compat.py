"""Strict, non-authoritative projection of legacy Snapshot diagnostics."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
        cls.schema_sha = compat.git_blob_sha_bytes(schema_path.read_bytes())
        cls.commit = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()

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

    def _transform_raw(self, raw, source_path="data/market-data/snapshots/2026-10/2026-10-30/open_30m/test.json", transformer_commit=None):
        return compat.transform_snapshot_bytes(
            raw,
            original_path=source_path,
            expected_original_blob_sha=compat.git_blob_sha_bytes(raw),
            original_schema_sha="2b827576024c9d27af4b589583720fefef1ffa58",
            original_schema_validator=self.validator,
            current_schema_sha=self.schema_sha,
            current_schema_validator=self.validator,
            transformed_at="2026-11-02T12:00:00Z",
            store_commit="bbef1a377ff5a2a7a17d2342a11fb002ff76b9df",
            transformer_commit=transformer_commit or self.commit,
        )

    def _transform(self, document, source_path="data/market-data/snapshots/2026-10/2026-10-30/open_30m/test.json", transformer_commit=None):
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
        return self._transform_raw(raw, source_path=source_path, transformer_commit=transformer_commit)

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
                transformer_commit=self.commit,
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


    def test_duplicate_json_members_at_root_and_nested_levels_fail_closed(self):
        raw = json.dumps(
            self._snapshot(["success", "success"]), separators=(",", ":")
        ).encode("utf-8")
        cases = (
            raw.replace(
                b'"trade_date":"2026-10-30"',
                b'"trade_date":"2026-10-29","trade_date":"2026-10-30"',
            ),
            raw.replace(
                b'"AAA":["success","success"]',
                b'"AAA":["success","success"],"AAA":["success","success"]',
            ),
        )
        for corrupt in cases:
            with self.subTest(corrupt=corrupt[:75]), self.assertRaises(compat.CompatibilityTransformError):
                self._transform_raw(corrupt)
        with self.assertRaises(compat.CompatibilityTransformError):
            compat.strict_json_loads(b'{"a":{"b":1,"b":2}}')
        with self.assertRaises(compat.CompatibilityTransformError):
            compat.strict_json_loads(b'{"a":NaN}')

    def test_wrong_or_nonexistent_transformer_commit_is_rejected(self):
        for claimed in ("a" * 40, "0" * 40):
            if claimed == self.commit:
                continue
            with self.subTest(commit=claimed), self.assertRaises(compat.CompatibilityTransformError):
                self._transform(self._snapshot(["success", "success"]), transformer_commit=claimed)

    def test_changed_executing_script_bytes_are_rejected(self):
        actual_read_bytes = Path.read_bytes
        script = Path(compat.__file__).resolve()
        def tampered_read_bytes(path):
            if path.resolve() == script:
                return b"tampered script bytes"
            return actual_read_bytes(path)
        with mock.patch.object(Path, "read_bytes", tampered_read_bytes):
            with self.assertRaisesRegex(compat.CompatibilityTransformError, "differs from pinned Git bytes"):
                self._transform(self._snapshot(["success", "success"]))

    def test_noncanonical_snapshot_alias_paths_are_rejected(self):
        for path in (
            "data/market-data/snapshots//2026-10/2026-10-30/open_30m/test.json",
            "data/market-data/snapshots/./2026-10/2026-10-30/open_30m/test.json",
            "data/market-data/snapshots/../2026-10/2026-10-30/open_30m/test.json",
        ):
            with self.subTest(path=path), self.assertRaises(compat.CompatibilityTransformError):
                self._transform(self._snapshot(["success", "success"]), source_path=path)

    def test_private_output_checkout_overlap_and_symlink_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "store"
            store.mkdir()
            with self.assertRaises(compat.CompatibilityTransformError):
                compat._trusted_output_root(ROOT, store)
            with self.assertRaises(compat.CompatibilityTransformError):
                compat._trusted_output_root(store, store)
            output = root / "output"
            outside = root / "outside"
            output.mkdir()
            outside.mkdir()
            (output / "derived").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(compat.CompatibilityTransformError):
                compat._write_create_only(output, Path("derived/unwanted.json"), b"secret")
            self.assertFalse((outside / "unwanted.json").exists())

    def test_manifest_pointer_schema_rejects_nested_and_trailing_tokens(self):
        _, manifest = self._transform(self._snapshot(["success", "success"]))
        schema = json.loads(
            (ROOT / "schemas/legacy-snapshot-compatibility-manifest.schema.json").read_text()
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(manifest)
        bad_pointers = (
            "/coverage/stage_snapshot/reason_classes/AAA",
            "/coverage/reason_classes/AAA/trailing",
            "/coverage/reason_classes/AAA~2",
            "/coverage/reason_classes/AAA/extra",
        )
        for bad in bad_pointers:
            with self.subTest(pointer=bad):
                m = copy.deepcopy(manifest)
                m["exact_changed_json_pointers"] = [bad]
                self.assertFalse(validator.is_valid(m))
                m = copy.deepcopy(manifest)
                m["before_values"] = {bad: ["success", "success"]}
                self.assertFalse(validator.is_valid(m))
                m = copy.deepcopy(manifest)
                m["after_values"] = {bad: ["success"]}
                self.assertFalse(validator.is_valid(m))

    @staticmethod
    def _inventory_fixture():
        rows = [
            {
                "store_path": (
                    "data/market-data/snapshots/2026-10/2026-10-30/open_30m/"
                    f"test-{i:03d}.json"
                ),
                "store_blob": "1" * 40,
                "validation_status": "FAIL" if i < 42 else "PASS",
            }
            for i in range(499)
        ]
        return rows

    @staticmethod
    def _build_pinned_report(rows, *, store_sha="b" * 40, schema_sha="c" * 40):
        report = b"".join(compat.canonical_bytes(row) for row in rows)
        target_list = sorted(
            [
                {"store_path": row["store_path"], "store_blob": row["store_blob"]}
                for row in rows if row.get("validation_status") == "FAIL"
            ],
            key=lambda row: row["store_path"],
        )
        inventory = {
            "schema_version": 1,
            "store_commit": store_sha,
            "current_schema_sha": schema_sha,
            "report_sha256": hashlib.sha256(report).hexdigest(),
            "report_rows": 499,
            "target_count": 42,
            "targets": target_list,
        }
        inv_raw = compat.canonical_bytes(inventory)
        return report, inv_raw, hashlib.sha256(inv_raw).hexdigest()

    def test_exact_pinned_499_row_42_target_inventory_passes(self):
        rows = self._inventory_fixture()
        report, inv_raw, inv_sha = self._build_pinned_report(rows)
        targets = compat._load_pinned_targets(
            report, inv_raw, inv_sha,
            store_commit="b" * 40, current_schema_sha="c" * 40,
        )
        self.assertEqual(len(targets), 42)
        self.assertEqual(len({row["store_path"] for row in targets}), 42)

    def test_empty_unknown_partial_duplicate_and_unpinned_inventory_fail(self):
        rows = self._inventory_fixture()
        cases = {
            "empty": [],
            "unknown": [{**r, "validation_status": "UNKNOWN"} if i == 0 else r for i, r in enumerate(rows)],
            "partial": rows[:-1],
            "duplicate": rows[:-1] + [rows[0]],
            "missing_target": [r for i, r in enumerate(rows) if i != 0],
        }
        for label, broken in cases.items():
            with self.subTest(label=label):
                report, inv_raw, inv_sha = self._build_pinned_report(broken)
                with self.assertRaises(compat.CompatibilityTransformError):
                    compat._load_pinned_targets(
                        report, inv_raw, inv_sha,
                        store_commit="b" * 40, current_schema_sha="c" * 40,
                    )
        report, inv_raw, inv_sha = self._build_pinned_report(rows)
        with self.assertRaises(compat.CompatibilityTransformError):
            compat._load_pinned_targets(
                report, inv_raw, "0" * 64,
                store_commit="b" * 40, current_schema_sha="c" * 40,
            )
        inventory = compat.strict_json_loads(inv_raw)
        inventory["targets"][1] = inventory["targets"][0]
        corrupt_inv = compat.canonical_bytes(inventory)
        with self.assertRaises(compat.CompatibilityTransformError):
            compat._load_pinned_targets(
                report, corrupt_inv, hashlib.sha256(corrupt_inv).hexdigest(),
                store_commit="b" * 40, current_schema_sha="c" * 40,
            )

    def test_actual_cli_42_object_create_only_and_repeated_execution(self):
        """Native synthetic Store checkout; no private market payload or network."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "store"
            output = base / "scratch"
            store.mkdir()
            output.mkdir()
            subprocess.run(["git", "init", "-q", str(store)], check=True)
            rows = self._inventory_fixture()
            for i in range(42):
                path = store / rows[i]["store_path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                doc = self._snapshot(["success", "success", "outside_window"])
                doc["snapshot_id"] = f"synthetic-{i:03d}"
                raw = compat.canonical_bytes(doc)
                path.write_bytes(raw)
                rows[i]["store_blob"] = compat.git_blob_sha_bytes(raw)
            subprocess.run(["git", "-C", str(store), "add", "data/market-data/snapshots"], check=True)
            subprocess.run([
                "git", "-C", str(store), "-c", "user.name=unit-test",
                "-c", "user.email=unit-test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic fixtures",
            ], check=True)
            store_sha = subprocess.check_output(
                ["git", "-C", str(store), "rev-parse", "HEAD"], text=True,
            ).strip()
            schema_path = ROOT / "schemas/market-data-stage-snapshot.schema.json"
            schema_blob = compat.git_blob_sha_bytes(schema_path.read_bytes())
            report, inv_raw, inv_sha = self._build_pinned_report(
                rows, store_sha=store_sha, schema_sha=schema_blob,
            )
            report_path = base / "report.jsonl"
            inventory_path = base / "expected-inventory.json"
            report_path.write_bytes(report)
            inventory_path.write_bytes(inv_raw)
            args = [
                "--store-repo", str(store), "--store-commit", store_sha,
                "--object-results", str(report_path),
                "--expected-inventory", str(inventory_path),
                "--expected-inventory-sha256", inv_sha,
                "--original-schema", str(schema_path),
                "--original-schema-sha", schema_blob,
                "--current-schema", str(schema_path),
                "--current-schema-sha", schema_blob,
                "--output-root", str(output),
                "--transformed-at", "2026-11-02T12:00:00Z",
                "--transformer-commit", self.commit,
            ]
            logs = []
            for _ in range(2):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(compat.main(args), 0)
                logs.append(json.loads(stream.getvalue()))
            self.assertEqual([l["emitted_count"] for l in logs], [42, 42])
            self.assertEqual(sum(row["created"] for row in logs[0]["derived"]), 42)
            self.assertEqual(sum(row["created"] for row in logs[1]["derived"]), 0)
            self.assertEqual(len(list(output.rglob("*.manifest.json"))), 42)
            self.assertEqual(len([p for p in output.rglob("test-*.json") if not p.name.endswith(".manifest.json")]), 42)


    def test_cli_rejects_invalid_report_before_any_derived_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "store"
            scratch = base / "scratch"
            store.mkdir()
            scratch.mkdir()
            subprocess.run(["git", "init", "-q", str(store)], check=True)
            subprocess.run([
                "git", "-C", str(store), "-c", "user.name=unit-test",
                "-c", "user.email=unit-test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "empty",
            ], check=True)
            store_sha = subprocess.check_output(
                ["git", "-C", str(store), "rev-parse", "HEAD"], text=True,
            ).strip()
            schema_path = ROOT / "schemas/market-data-stage-snapshot.schema.json"
            schema_sha = compat.git_blob_sha_bytes(schema_path.read_bytes())
            report_path = base / "report.jsonl"
            inventory_path = base / "expected-inventory.json"
            valid = self._inventory_fixture()
            variants = {
                "empty": [],
                "unknown-status": [{**r, "validation_status": "UNRECOGNIZED"} if i == 0 else r for i, r in enumerate(valid)],
                "partial": valid[:-1],
                "duplicate": valid[:-1] + [valid[0]],
            }
            for name, rows in variants.items():
                with self.subTest(name=name):
                    report, inv, inv_sha = self._build_pinned_report(
                        rows, store_sha=store_sha, schema_sha=schema_sha,
                    )
                    report_path.write_bytes(report)
                    inventory_path.write_bytes(inv)
                    args = [
                        "--store-repo", str(store), "--store-commit", store_sha,
                        "--object-results", str(report_path),
                        "--expected-inventory", str(inventory_path),
                        "--expected-inventory-sha256", inv_sha,
                        "--original-schema", str(schema_path), "--original-schema-sha", schema_sha,
                        "--current-schema", str(schema_path), "--current-schema-sha", schema_sha,
                        "--output-root", str(scratch),
                        "--transformed-at", "2026-11-02T12:00:00Z",
                        "--transformer-commit", self.commit,
                    ]
                    with self.assertRaises(compat.CompatibilityTransformError):
                        compat.main(args)
                    self.assertFalse((scratch / "derived").exists())

    def test_duplicate_source_member_cli_fails_without_derived_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "store"
            scratch = base / "scratch"
            store.mkdir()
            scratch.mkdir()
            subprocess.run(["git", "init", "-q", str(store)], check=True)
            subprocess.run([
                "git", "-C", str(store), "-c", "user.name=unit-test",
                "-c", "user.email=unit-test@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "empty",
            ], check=True)
            store_sha = subprocess.check_output(
                ["git", "-C", str(store), "rev-parse", "HEAD"], text=True,
            ).strip()
            raw = json.dumps(
                self._snapshot(["success", "success"]), separators=(",", ":"),
            ).encode("utf-8")
            malicious = raw.replace(
                b'"trade_date":"2026-10-30"',
                b'"trade_date":"2026-10-29","trade_date":"2026-10-30"',
            )
            rows = self._inventory_fixture()
            for row in rows[:42]:
                row["store_blob"] = compat.git_blob_sha_bytes(malicious)
            schema_path = ROOT / "schemas/market-data-stage-snapshot.schema.json"
            schema_sha = compat.git_blob_sha_bytes(schema_path.read_bytes())
            report, inv, inv_sha = self._build_pinned_report(
                rows, store_sha=store_sha, schema_sha=schema_sha,
            )
            report_path = base / "report.jsonl"
            inventory_path = base / "expected-inventory.json"
            report_path.write_bytes(report)
            inventory_path.write_bytes(inv)
            args = [
                "--store-repo", str(store), "--store-commit", store_sha,
                "--object-results", str(report_path),
                "--expected-inventory", str(inventory_path),
                "--expected-inventory-sha256", inv_sha,
                "--original-schema", str(schema_path), "--original-schema-sha", schema_sha,
                "--current-schema", str(schema_path), "--current-schema-sha", schema_sha,
                "--output-root", str(scratch),
                "--transformed-at", "2026-11-02T12:00:00Z",
                "--transformer-commit", self.commit,
            ]
            with mock.patch.object(compat, "_git_bytes", return_value=malicious):
                with self.assertRaisesRegex(compat.CompatibilityTransformError, "duplicate JSON member"):
                    compat.main(args)
            self.assertFalse((scratch / "derived").exists())


if __name__ == "__main__":
    unittest.main()
