from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_store_publication_manifest",
    ROOT / "scripts" / "build_store_publication_manifest.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StorePublicationManifestTest(unittest.TestCase):
    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        store = root / "data" / "market-data"
        seed = store / "collector-state" / "seed.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "data/market-data"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True)
        return tmp, root, store

    def test_manifest_binds_actual_changed_and_declared_output_blobs(self):
        tmp, root, store = self._repo()
        self.addCleanup(tmp.cleanup)
        snapshot = store / "snapshots" / "2026-09" / "fixture.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text('{"ok":true}\n', encoding="utf-8")
        result_file = root / "result.txt"
        result_file.write_text(
            json.dumps({"snapshot_path": "snapshots/2026-09/fixture.json"}) + "\n",
            encoding="utf-8",
        )
        manifest = MODULE.build_manifest(
            store_root=store,
            result_file=result_file,
            request_id="req-1",
            trade_date="2026-09-22",
            stage="open_30m",
        )
        self.assertIsNotNone(manifest)
        value = json.loads(manifest.read_text(encoding="utf-8"))
        outputs = {row["path"]: row["blob_sha"] for row in value["required_outputs"]}
        self.assertEqual(outputs["snapshots/2026-09/fixture.json"], MODULE._git_blob_sha(snapshot.read_bytes()))

    def test_manifest_fails_when_producer_declares_missing_output(self):
        tmp, root, store = self._repo()
        self.addCleanup(tmp.cleanup)
        result_file = root / "result.txt"
        result_file.write_text(
            json.dumps({"snapshot_path": "snapshots/2026-09/missing.json"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MODULE.PublicationManifestError, "declared output does not exist"):
            MODULE.build_manifest(
                store_root=store,
                result_file=result_file,
                request_id="req-1",
                trade_date="2026-09-22",
                stage="open_30m",
            )

    def test_manifest_returns_none_when_run_has_no_store_output(self):
        tmp, root, store = self._repo()
        self.addCleanup(tmp.cleanup)
        result_file = root / "result.txt"
        result_file.write_text(json.dumps({"status": "no_new_qualified_facts"}) + "\n", encoding="utf-8")
        self.assertIsNone(MODULE.build_manifest(
            store_root=store,
            result_file=result_file,
            request_id="req-1",
            trade_date="2026-09-22",
            stage="open_30m",
        ))


if __name__ == "__main__":
    unittest.main()
