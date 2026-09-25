"""Fault injection at the actual durability verifier; only local bare Git remotes."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_store_publication as V

RUN = subprocess.run


class PublicationTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work, self.remote = self.root / "work", self.root / "remote.git"
        self.git("init", "--bare", "-q", str(self.remote), cwd=self.root)
        self.git("init", "-b", "main", "-q", str(self.work), cwd=self.root)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        data = self.work / "data/market-data"
        data.mkdir(parents=True)
        (data / "seed.json").write_text("{}\n")
        self.git("add", ".")
        self.git("commit", "-qm", "seed")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "HEAD:main")
        (data / "snapshot.json").write_text('{"qualified":true}\n')
        self.blob = self.git("hash-object", "data/market-data/snapshot.json")
        self.manifest = "data/market-data/collector-state/test-publication.json"
        target = self.work / self.manifest
        target.parent.mkdir()
        target.write_text(json.dumps({
            "schema_version": 1, "request_id": "test", "trade_date": "2026-09-24",
            "stage": "open_60m", "required_outputs": [
                {"path": "snapshot.json", "blob_sha": self.blob},
            ],
        }) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "qualified output")
        self.head = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "HEAD:main")
        self.tag = V.REF_PREFIX + self.head
        self.calls = []

    def git(self, *args, cwd=None):
        out = RUN(["git", *args], cwd=cwd or self.work, text=True,
                  capture_output=True, check=True)
        return out.stdout.strip()

    def intercept(self, fault):
        def execute(cmd, **kwargs):
            op = cmd[3] if len(cmd) > 3 and cmd[:2] == ["git", "-C"] else ""
            if op in {"fetch", "ls-remote", "push"}:
                self.calls.append((list(cmd), dict(kwargs)))
                injected = fault(op, cmd, kwargs)
                if injected is not None:
                    return injected
            return RUN(cmd, **kwargs)
        return execute

    def verify(self, fault=lambda *args: None):
        with mock.patch.object(V.subprocess, "run", side_effect=self.intercept(fault)), \
                mock.patch("time.sleep"):
            return V.verify_publication(self.work, self.head, required_manifest=self.manifest)

    def failed(self, cmd, text="Internal Server Error", code=1):
        return subprocess.CompletedProcess(cmd, code, "", text)

    def count(self, op):
        return sum(cmd[3] == op for cmd, _ in self.calls)

    def assert_bound_write(self):
        for cmd, kwargs in self.calls:
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "Never")
            self.assertFalse(set(cmd) & {"--force", "--force-with-lease", "rebase", "merge", "pull"})
            if cmd[3] == "push":
                self.assertEqual(cmd[4:], ["origin", f"{self.head}:{self.tag}"])

    def advance_remote(self):
        tree = self.git("rev-parse", "HEAD^{tree}")
        other = self.git("commit-tree", tree, "-p", self.head, "-m", "external advance")
        self.git("push", "-q", "origin", f"{other}:refs/heads/main")
        return other

    def test_success_preserves_required_output_receipt(self):
        result = self.verify()
        self.assertEqual(result["status"], "durability_verified")
        self.assertEqual(result["store_commit_sha"], self.head)
        self.assertEqual(result["verified_required_outputs"], [
            {"path": "data/market-data/snapshot.json", "blob_sha": self.blob}])
        self.assertEqual(self.git("ls-remote", "origin", self.tag).split()[0], self.head)
        self.assert_bound_write()

    def test_transient_fetch_retries_then_validates_actual_commit(self):
        result = self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "fetch" and self.count(op) == 1 else None)
        self.assertEqual(result["status"], "durability_verified")
        self.assertEqual(self.count("fetch"), 2)

    def test_transient_ref_read_is_retried(self):
        result = self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "ls-remote" and self.count(op) == 1 else None)
        self.assertEqual(result["status"], "durability_verified")

    def test_transient_tag_push_retries_same_commit(self):
        result = self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "push" and self.count(op) == 1 else None)
        self.assertEqual(result["status"], "durability_verified")
        self.assertEqual(self.count("push"), 2)
        self.assert_bound_write()

    def test_lost_tag_ack_is_verified_without_second_push(self):
        def fault(op, cmd, kwargs):
            if op == "push":
                actual = RUN(cmd, **kwargs)
                self.assertEqual(actual.returncode, 0)
                return self.failed(cmd, "remote end hung up unexpectedly")
        result = self.verify(fault)
        self.assertEqual(result["status"], "durability_verified")
        self.assertEqual(self.count("push"), 1)

    def test_existing_exact_tag_is_idempotent(self):
        self.git("push", "-q", "origin", f"{self.head}:{self.tag}")
        self.assertEqual(self.verify()["status"], "durability_verified")
        self.assertEqual(self.count("push"), 0)

    def test_conflicting_tag_is_not_retargeted(self):
        self.git("push", "-q", "origin", f"{self.base}:{self.tag}")
        with self.assertRaises(V.StorePublicationError):
            self.verify()
        self.assertEqual(self.count("push"), 0)
        self.assertEqual(self.git("ls-remote", "origin", self.tag).split()[0], self.base)

    def test_remote_main_drift_before_tag_write_is_not_accepted(self):
        def fault(op, cmd, kwargs):
            if op == "fetch":
                actual = RUN(cmd, **kwargs)
                self.advance_remote()
                return actual
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("push"), 0)

    def test_remote_drift_after_lost_ack_is_not_success(self):
        def fault(op, cmd, kwargs):
            if op == "push":
                RUN(cmd, **kwargs)
                self.advance_remote()
                return self.failed(cmd)
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("push"), 1)

    def test_local_candidate_drift_before_tag_write_stops(self):
        original = V._verify_required_manifest
        def changed(*args):
            result = original(*args)
            self.git("checkout", "--detach", "-q", self.base)
            return result
        with mock.patch.object(V, "_verify_required_manifest", side_effect=changed):
            with self.assertRaises(V.StorePublicationError):
                self.verify()
        self.assertEqual(self.count("push"), 0)

    def test_persistent_transient_fetch_is_bounded(self):
        with self.assertRaises(V.StorePublicationError):
            self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "fetch" else None)
        self.assertEqual(self.count("fetch"), 3)
        self.assertEqual(self.count("push"), 0)

    def test_permanent_and_unknown_read_errors_are_not_retried_or_leaked(self):
        for error in ("Permission denied", "Authentication failed", "unknown failure"):
            with self.subTest(error=error):
                self.calls.clear()
                with self.assertRaises(V.StorePublicationError) as caught:
                    self.verify(lambda op, cmd, kw: self.failed(cmd, error + " SECRET_SENTINEL") if op == "fetch" else None)
                self.assertEqual(self.count("fetch"), 1)
                self.assertEqual(self.count("push"), 0)
                self.assertNotIn("SECRET_SENTINEL", str(caught.exception))

    def test_permanent_tag_error_with_no_ref_is_not_retried(self):
        with self.assertRaises(V.StorePublicationError):
            self.verify(lambda op, cmd, kw: self.failed(cmd, "Permission denied") if op == "push" else None)
        self.assertEqual(self.count("push"), 1)

    def test_persistent_tag_error_is_bounded(self):
        with self.assertRaises(V.StorePublicationError):
            self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "push" else None)
        self.assertEqual(self.count("push"), 3)
        self.assert_bound_write()

    def test_success_exit_without_tag_readback_is_never_success(self):
        with self.assertRaises(V.StorePublicationError):
            self.verify(lambda op, cmd, kw: subprocess.CompletedProcess(cmd, 0, "", "") if op == "push" else None)
        self.assertEqual(self.count("push"), 3)

    def test_unreadable_tag_after_push_forbids_blind_second_write(self):
        def fault(op, cmd, kwargs):
            if op == "push":
                return self.failed(cmd)
            if op == "ls-remote" and cmd[-1] == self.tag and self.count("push"):
                return self.failed(cmd)
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("push"), 1)

    def test_malformed_or_multiple_ref_output_never_means_absence(self):
        for output in ("broken\n", f"{self.head}\t{self.tag}\n{self.base}\t{self.tag}\n"):
            with self.subTest(output=output):
                self.calls.clear()
                with self.assertRaises(V.StorePublicationError):
                    self.verify(lambda op, cmd, kw: subprocess.CompletedProcess(cmd, 0, output, "") if op == "ls-remote" and cmd[-1] == self.tag else None)
                self.assertEqual(self.count("push"), 0)

    def test_timeout_is_normalized_and_retried(self):
        def fault(op, cmd, kwargs):
            if op == "fetch" and self.count(op) == 1:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30), stderr="SECRET_SENTINEL")
        self.assertEqual(self.verify(fault)["status"], "durability_verified")
        self.assertEqual(self.count("fetch"), 2)
        self.assert_bound_write()

    def test_total_deadline_cannot_be_reset_by_nested_reads(self):
        clock = [0.0]
        def fault(op, cmd, kwargs):
            clock[0] += 30
        with mock.patch.object(V.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(V.StorePublicationError, "DEADLINE"):
                self.verify(fault)
        self.assertLessEqual(len(self.calls), 4)
        self.assert_bound_write()

    def test_missing_required_output_stops_before_tag_creation(self):
        target = self.work / self.manifest
        data = json.loads(target.read_text())
        data["required_outputs"][0]["path"] = "missing.json"
        target.write_text(json.dumps(data))
        self.git("commit", "-qam", "invalid required output")
        self.head = self.git("rev-parse", "HEAD")
        self.tag = V.REF_PREFIX + self.head
        self.git("push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(V.StorePublicationError, "required output missing"):
            self.verify()
        self.assertEqual(self.count("push"), 0)

    def test_cli_transport_failure_emits_safe_json_not_traceback(self):
        fault = lambda op, cmd, kw: self.failed(cmd, "Permission denied https://token:SECRET_SENTINEL@example.invalid/") if op == "fetch" else None
        stderr = io.StringIO()
        with mock.patch.object(V.subprocess, "run", side_effect=self.intercept(fault)), \
                mock.patch.object(sys, "argv", ["verify", "--store-root", str(self.work), "--expected-commit", self.head]), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(V.main(), 1)
        value = json.loads(stderr.getvalue())
        self.assertEqual(value["status"], "store_publication_failed")
        self.assertNotIn("SECRET_SENTINEL", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_exact_fetch_avoids_second_floating_branch_resolution(self):
        # Inject a stale branch-fetch observation while the actual remote ref
        # already points at the candidate. Immutable-object fetch is unaffected.
        def fault(op, cmd, kwargs):
            if op == "fetch" and cmd[-1] in {"main", "refs/heads/main"}:
                stale = list(cmd)
                stale[-1] = self.base
                return RUN(stale, **kwargs)
        result = self.verify(fault)
        self.assertEqual(result["status"], "durability_verified")
        self.assertEqual(result["remote_main_sha"], self.head)
        for cmd, _ in self.calls:
            if cmd[3] == "fetch":
                self.assertEqual(cmd[-1], self.head)
                self.assertIn("--write-fetch-head", cmd)
                self.assertIn("--no-recurse-submodules", cmd)
        self.assert_bound_write()

    def test_exact_fetch_reads_full_main_ref_before_fetch(self):
        self.verify()
        self.assertEqual(self.calls[0][0][3:], ["ls-remote", "origin", "refs/heads/main"])
        self.assertEqual(self.calls[1][0][3], "fetch")
        self.assertEqual(self.calls[1][0][-1], self.head)

    def test_exact_fetch_missing_main_is_not_absence_permission(self):
        def fault(op, cmd, kwargs):
            if op == "ls-remote" and cmd[-1] == "refs/heads/main":
                return subprocess.CompletedProcess(cmd, 0, "", "")
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("fetch"), 0)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_different_main_never_switches_expected_commit(self):
        def fault(op, cmd, kwargs):
            if op == "ls-remote" and cmd[-1] == "refs/heads/main":
                return subprocess.CompletedProcess(cmd, 0, f"{self.base}\trefs/heads/main\n", "")
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("ls-remote"), 1)
        self.assertEqual(self.count("fetch"), 0)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_malformed_main_is_not_retried(self):
        def fault(op, cmd, kwargs):
            if op == "ls-remote" and cmd[-1] == "refs/heads/main":
                return subprocess.CompletedProcess(cmd, 0, "broken\n", "")
        with self.assertRaises(V.StorePublicationError):
            self.verify(fault)
        self.assertEqual(self.count("ls-remote"), 1)
        self.assertEqual(self.count("fetch"), 0)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_rejects_stale_fetch_head_after_success_exit(self):
        def fault(op, cmd, kwargs):
            if op == "fetch":
                actual = RUN(cmd, **kwargs)
                (self.work / ".git/FETCH_HEAD").write_text(f"{self.base}\t\tstale fixture\n")
                return actual
        with self.assertRaisesRegex(V.StorePublicationError, "FETCHED_COMMIT_MISMATCH"):
            self.verify(fault)
        self.assertEqual(self.count("fetch"), 1)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_failed_fetch_does_not_reuse_previous_fetch_head(self):
        self.git("fetch", "--no-tags", "origin", self.head)
        with self.assertRaisesRegex(V.StorePublicationError, "READ_EXHAUSTED"):
            self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "fetch" else None)
        self.assertEqual(self.count("fetch"), 3)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_remote_drift_stops_before_output_verification(self):
        def fault(op, cmd, kwargs):
            if op == "fetch":
                actual = RUN(cmd, **kwargs)
                self.advance_remote()
                return actual
        with mock.patch.object(V, "_verify_required_manifest", wraps=V._verify_required_manifest) as proof:
            with self.assertRaises(V.StorePublicationError):
                self.verify(fault)
            proof.assert_not_called()
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_local_drift_stops_before_output_verification(self):
        def fault(op, cmd, kwargs):
            if op == "fetch":
                actual = RUN(cmd, **kwargs)
                self.git("checkout", "--detach", "-q", self.base)
                return actual
        with mock.patch.object(V, "_verify_required_manifest", wraps=V._verify_required_manifest) as proof:
            with self.assertRaises(V.StorePublicationError):
                self.verify(fault)
            proof.assert_not_called()
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_permanent_main_error_stops_without_fetch(self):
        def fault(op, cmd, kwargs):
            if op == "ls-remote" and cmd[-1] == "refs/heads/main":
                return self.failed(cmd, "Permission denied SECRET_SENTINEL")
        with self.assertRaises(V.StorePublicationError) as caught:
            self.verify(fault)
        self.assertNotIn("SECRET_SENTINEL", str(caught.exception))
        self.assertEqual(self.count("ls-remote"), 1)
        self.assertEqual(self.count("fetch"), 0)
        self.assertEqual(self.count("push"), 0)

    def test_exact_fetch_transient_retry_keeps_immutable_commit(self):
        result = self.verify(lambda op, cmd, kw: self.failed(cmd) if op == "fetch" and self.count(op) == 1 else None)
        self.assertEqual(result["status"], "durability_verified")
        fetches = [cmd for cmd, _ in self.calls if cmd[3] == "fetch"]
        self.assertEqual(len(fetches), 2)
        self.assertTrue(all(cmd[-1] == self.head for cmd in fetches))
        self.assert_bound_write()


class TransportOwnerTests(unittest.TestCase):
    def test_implementation_bounds_match_owner_without_changing_receipt_authority(self):
        import yaml
        owner = yaml.safe_load((ROOT / "config/store-publication-durability.yaml").read_text())
        contract = owner["verification_transport"]
        self.assertEqual(owner["contract_version"], 4)
        self.assertEqual(V.REMOTE_ATTEMPTS, contract["max_attempts_per_operation"])
        self.assertEqual(V.REMOTE_COMMAND_TIMEOUT_SECONDS, contract["command_timeout_seconds"])
        self.assertEqual(V.REMOTE_BUDGET_SECONDS, contract["shared_elapsed_budget_seconds"])
        self.assertEqual(list(V.REMOTE_BACKOFF_SECONDS), contract["backoff_seconds"])
        self.assertTrue(owner["retention_anchor"]["retarget_forbidden"])
        self.assertTrue(owner["consumer_contract"]["proof_required_before_market_data_read_sha_pin"])
        self.assertEqual(owner["durability_receipt"]["status_value"], "durability_verified")
        proof = owner["remote_publication_proof"]
        self.assertEqual(proof["remote_main_mismatch"], "fail_closed")
        self.assertEqual(proof["fetched_commit_mismatch"], "fail_closed")
        order = proof["order"]
        self.assertLess(
            order.index("require_remote_main_equals_pushed_commit_while_writer_lock_is_held"),
            order.index("fetch_same_verified_immutable_commit_without_reresolving_floating_main"),
        )
        self.assertLess(
            order.index("recheck_local_head_and_remote_main_before_output_verification"),
            order.index("verify_changed_non_deleted_path_blob_ids_from_remote_commit"),
        )


if __name__ == "__main__":
    unittest.main()
