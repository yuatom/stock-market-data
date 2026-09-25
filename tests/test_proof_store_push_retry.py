"""Transport fault injection plus real local Git; no external services used."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from push_proof_store_commit import StorePushError, push_exact, transient

BASE, HEAD, OTHER = "1" * 40, "2" * 40, "3" * 40
REF = "refs/heads/main"


def result(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def remote(sha):
    return result(out=f"{sha}\t{REF}\n")


class FakeGit:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []
        self.head = HEAD
        self.dirty = ""
        self.paths = "data/market-data/proof.json\0"
        self.parents = f"{HEAD} {BASE}"

    def __call__(self, *args):
        self.calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return result(out=self.head)
        if args[0] == "status":
            return result(out=self.dirty)
        if args[0] == "rev-list":
            return result(out=self.parents)
        if args[0] == "diff":
            return result(out=self.paths)
        expected, response = self.sequence.pop(0)
        if args[0] != expected:
            raise AssertionError((expected, args))
        return response


class RetryTests(unittest.TestCase):
    def invoke(self, sequence, error=None):
        fake = FakeGit(sequence)
        sleeps = []
        if error:
            with self.assertRaisesRegex(StorePushError, error):
                push_exact(Path("."), BASE, HEAD, run=fake, sleep=sleeps.append)
            output = None
        else:
            output = push_exact(Path("."), BASE, HEAD, run=fake, sleep=sleeps.append)
        self.assertEqual(fake.sequence, [])
        for call in fake.calls:
            self.assertFalse(any(x in call for x in ("--force", "--force-with-lease", "rebase", "merge", "pull")))
            if call[0] == "push":
                self.assertEqual(call, ("push", "--porcelain", "origin", f"{HEAD}:{REF}"))
        return output, fake, sleeps

    def test_first_push_success_requires_readback(self):
        out, _, sleeps = self.invoke([("ls-remote", remote(BASE)), ("push", result()), ("ls-remote", remote(HEAD))])
        self.assertEqual(out["push_attempts"], 1)
        self.assertEqual(out["durability_status"], "NOT_CHECKED")
        self.assertEqual(sleeps, [])

    def test_real_incident_500_retries_identical_commit(self):
        out, _, sleeps = self.invoke([
            ("ls-remote", remote(BASE)), ("push", result(1, err="remote: Internal Server Error")), ("ls-remote", remote(BASE)),
            ("ls-remote", remote(BASE)), ("push", result()), ("ls-remote", remote(HEAD))])
        self.assertEqual(out["push_attempts"], 2)
        self.assertEqual(sleeps, [1])

    def test_lost_acknowledgement_does_not_push_twice(self):
        out, _, _ = self.invoke([("ls-remote", remote(BASE)), ("push", result(124)), ("ls-remote", remote(HEAD))])
        self.assertEqual(out["push_attempts"], 1)

    def test_existing_exact_candidate_is_idempotent(self):
        out, _, _ = self.invoke([("ls-remote", remote(HEAD))])
        self.assertEqual(out["push_attempts"], 0)

    def test_drift_before_push_stops_without_write(self):
        self.invoke([("ls-remote", remote(OTHER))], "STORE_REMOTE_MAIN_ADVANCED")

    def test_drift_after_failed_push_is_not_retried(self):
        self.invoke([("ls-remote", remote(BASE)), ("push", result(1, err="Internal Server Error")), ("ls-remote", remote(OTHER))], "STORE_REMOTE_MAIN_ADVANCED")

    def test_permanent_permission_failure_is_not_retried(self):
        self.invoke([("ls-remote", remote(BASE)), ("push", result(1, err="Permission denied")), ("ls-remote", remote(BASE))], "STORE_PUSH_REJECTED_NON_TRANSIENT")

    def test_unknown_rejection_is_not_assumed_transient(self):
        self.invoke([("ls-remote", remote(BASE)), ("push", result(1, err="unknown failure")), ("ls-remote", remote(BASE))], "STORE_PUSH_REJECTED_NON_TRANSIENT")

    def test_transient_remote_read_never_permits_blind_push(self):
        out, fake, _ = self.invoke([("ls-remote", result(124)), ("ls-remote", remote(BASE)), ("push", result()), ("ls-remote", remote(HEAD))])
        self.assertEqual(out["push_attempts"], 1)

    def test_push_success_without_readback_is_not_success(self):
        seq = []
        for _ in range(3):
            seq.extend([("ls-remote", remote(BASE)), ("push", result()), ("ls-remote", remote(BASE))])
        self.invoke(seq, "STORE_PUSH_READBACK_UNCONFIRMED")

    def test_transport_failure_is_bounded(self):
        seq = []
        for _ in range(3):
            seq.extend([("ls-remote", remote(BASE)), ("push", result(1, err="Internal Server Error")), ("ls-remote", remote(BASE))])
        _, _, sleeps = self.invoke(seq, "STORE_TRANSPORT_RETRIES_EXHAUSTED")
        self.assertEqual(sleeps, [1, 3])

    def test_unreadable_ref_stops_without_write(self):
        self.invoke([("ls-remote", result(2))], "STORE_REMOTE_UNREADABLE")

    def test_invalid_or_multiple_remote_identity_rejected(self):
        for text in ("", f"{HEAD}\trefs/heads/other", f"{BASE}\t{REF}\n{OTHER}\t{REF}"):
            with self.subTest(text=text):
                self.invoke([("ls-remote", result(out=text))], "STORE_REMOTE_IDENTITY_INVALID")

    def test_dirty_changed_head_scope_and_parent_rejected(self):
        for attr, value, error in (("dirty", " M data/market-data/proof.json", "DIRTY"), ("head", OTHER, "CHANGED"), ("paths", "README.md\0", "SCOPE"), ("parents", f"{HEAD} {OTHER}", "SINGLE_BASE_CHILD")):
            with self.subTest(attr=attr):
                fake = FakeGit([])
                setattr(fake, attr, value)
                with self.assertRaisesRegex(StorePushError, error):
                    push_exact(Path("."), BASE, HEAD, run=fake)

    def test_invalid_identity_and_retry_bounds(self):
        for base, head, n in (("main", HEAD, 3), (BASE, BASE, 3), (BASE, HEAD, 4), (BASE, HEAD, 0)):
            with self.subTest(base=base, n=n), self.assertRaises(StorePushError):
                push_exact(Path("."), base, head, attempts=n, run=FakeGit([]))

    def test_permanent_failure_overrides_transport_marker(self):
        self.assertFalse(transient(result(1, err="permission denied; Internal Server Error")))


class RealGitTests(unittest.TestCase):
    def test_bare_remote_push_idempotence_and_real_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare, work = root / "remote.git", root / "work"
            def git(*args, cwd=None):
                return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
            git("init", "--bare", str(bare))
            git("init", str(work))
            git("config", "user.name", "Test", cwd=work)
            git("config", "user.email", "test@example.invalid", cwd=work)
            data = work / "data/market-data"
            data.mkdir(parents=True)
            (data / "proof.json").write_text("base\n")
            git("add", ".", cwd=work)
            git("commit", "-m", "base", cwd=work)
            base = git("rev-parse", "HEAD", cwd=work)
            git("remote", "add", "origin", str(bare), cwd=work)
            git("push", "origin", "HEAD:refs/heads/main", cwd=work)
            (data / "proof.json").write_text("candidate\n")
            git("commit", "-am", "candidate", cwd=work)
            head = git("rev-parse", "HEAD", cwd=work)
            self.assertEqual(push_exact(work, base, head)["store_commit_sha"], head)
            self.assertEqual(push_exact(work, base, head)["push_attempts"], 0)
            # Move remote to a different commit without changing the candidate.
            tree = git("rev-parse", "HEAD^{tree}", cwd=work)
            other = git("commit-tree", tree, "-p", head, "-m", "other", cwd=work)
            git("push", "origin", f"{other}:refs/heads/main", cwd=work)
            with self.assertRaisesRegex(StorePushError, "STORE_REMOTE_MAIN_ADVANCED"):
                push_exact(work, base, head)
            self.assertEqual(git("rev-parse", "HEAD", cwd=work), head)
            self.assertEqual(git("ls-remote", "origin", REF, cwd=work).split()[0], other)


if __name__ == "__main__":
    unittest.main()
