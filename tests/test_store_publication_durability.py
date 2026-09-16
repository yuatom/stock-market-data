from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_store_publication",
    ROOT / "scripts" / "verify_store_publication.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StorePublicationDurabilityTests(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        remote = root / "remote.git"
        writer = root / "writer"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", str(writer)], check=True)
        subprocess.run(["git", "-C", str(writer), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(writer), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(writer), "remote", "add", "origin", str(remote)], check=True)
        path = writer / "data" / "market-data" / "snapshots" / "fixture.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"version":1}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "data/market-data"], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(writer), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(writer), "push", "-q", "-u", "origin", "main"], check=True)
        return tmp, writer, path

    def test_successful_remote_readback_creates_durable_ref(self):
        tmp, writer, path = self._fixture()
        self.addCleanup(tmp.cleanup)
        path.write_text('{"version":2}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "data/market-data"], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "update"], check=True)
        commit = subprocess.check_output(["git", "-C", str(writer), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(writer), "push", "-q", "origin", "HEAD:main"], check=True)

        receipt = MODULE.verify_publication(writer, commit)
        self.assertEqual(receipt["status"], "durability_verified")
        self.assertEqual(receipt["store_commit_sha"], commit)
        self.assertEqual(receipt["remote_main_sha"], commit)
        self.assertEqual(receipt["durability_ref"], f"refs/tags/market-data-read/{commit}")
        remote_ref = subprocess.check_output(
            ["git", "-C", str(writer), "ls-remote", "origin", receipt["durability_ref"]],
            text=True,
        ).split()[0]
        self.assertEqual(remote_ref, commit)

    def test_remote_main_drift_fails_closed(self):
        tmp, writer, _path = self._fixture()
        self.addCleanup(tmp.cleanup)
        expected = subprocess.check_output(["git", "-C", str(writer), "rev-parse", "HEAD"], text=True).strip()
        other = Path(tmp.name) / "other"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(Path(tmp.name) / "remote.git"), str(other)],
            check=True,
        )
        subprocess.run(["git", "-C", str(other), "config", "user.name", "other"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "other@example.invalid"], check=True)
        extra = other / "data" / "market-data" / "collector-state" / "advance.json"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text('{}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(other), "add", "data/market-data"], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-q", "-m", "advance"], check=True)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:main"], check=True)

        with self.assertRaisesRegex(MODULE.StorePublicationError, "remote main"):
            MODULE.verify_publication(writer, expected)

    def test_existing_durable_ref_cannot_be_retargeted(self):
        tmp, writer, path = self._fixture()
        self.addCleanup(tmp.cleanup)
        path.write_text('{"version":2}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "data/market-data"], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "update"], check=True)
        commit = subprocess.check_output(["git", "-C", str(writer), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(writer), "push", "-q", "origin", "HEAD:main"], check=True)
        bad_target = subprocess.check_output(["git", "-C", str(writer), "rev-parse", "HEAD^"], text=True).strip()
        ref = f"refs/tags/market-data-read/{commit}"
        subprocess.run(["git", "-C", str(writer), "push", "-q", "origin", f"{bad_target}:{ref}"], check=True)

        with self.assertRaisesRegex(MODULE.StorePublicationError, "already targets"):
            MODULE.verify_publication(writer, commit)


if __name__ == "__main__":
    unittest.main()
