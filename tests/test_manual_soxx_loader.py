"""Persistent file-only regression for expiry during the final Store read."""

from datetime import timedelta
from pathlib import Path
import os
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import manual_soxx_loader as M
from scripts import private_authority_receipt as P
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_remote import RemoteGitStore, STORE_REPOSITORY, STORE_URL
from scripts.daily_basis_store import LocalGitStore
from scripts.raw_response_evidence import build_envelope


DAYS = tuple(f"2027-03-0{n}" for n in range(1, 6))
DAY, CUTOFF = DAYS[-1], DAYS[-2]
REPO_ID, OWNER_ID = 11, 12
COMPUTE, CONSUMER = "a" * 40, "b" * 40
ROOT = "basis-authority/manual-soxx-repair/"


def git_cmd(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Fixture",
         "-c", "user.email=fixture@invalid", "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True, timeout=15, env={**os.environ, "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
    )


def git(root, *args):
    result = git_cmd(root, *args)
    result.check_returncode()
    return result.stdout.decode().strip()


class FileTransport:
    """Only the native fixed URL is replaced, with a disposable local bare repo."""

    def __init__(self, checkout, bare):
        self.checkout, self.bare = checkout, bare
        self.pushes = []

    def metadata(self):
        return dict(id=REPO_ID, private=True, full_name=STORE_REPOSITORY, default_branch="main",
                    owner=dict(id=OWNER_ID, login=STORE_REPOSITORY.split("/")[0], type="User"),
                    permissions=dict(pull=True, push=True, admin=False))

    def git(self, *args):
        assert args.count(STORE_URL) == 1
        actual = [str(self.bare) if word == STORE_URL else word for word in args]
        if args[0] == "push":
            self.pushes.append(actual)
            raise AssertionError("the loader must not push even to a fixture")
        return git_cmd(self.checkout, *actual)


def initial_scope(repo):
    """Entirely synthetic folded history with an admitted pre-cutoff tail."""
    repo.mkdir()
    git(repo, "init", "-qb", "main")
    git(repo, "remote", "add", "origin", STORE_URL)
    fields = ("open", "high", "low", "close")
    rows = [dict(datetime=d, **{name: str(n) for name in fields}, volume="300")
            for d, n in zip(DAYS[:3], (11, 12, 13))]
    req = dict(provider="twelve_data", symbol="SOXX", interval="1day", exchange="NASDAQ",
               mic_code="XNMS", currency="USD", type="ETF", adjust="splits", dp=-1,
               start_date=DAYS[0], end_date=DAYS[2], outputsize=5000, order="asc")
    meta = {k: req[k] for k in ("symbol", "interval", "currency", "exchange", "mic_code", "type")}
    raw = B.encode(dict(status="ok", meta=meta | {"exchange_timezone": "America/New_York"}, values=rows))
    envelope = build_envelope(raw, req, CUTOFF + "T12:00:00Z")
    baseline = B.encode([dict(trade_date=v["datetime"],
                  **{k: int(v[k]) * 3 for k in fields}, volume=100) for v in rows[:2]])
    snap = B.prepare_snapshot(envelope, B.sha(envelope), baseline,
                  dict(kind="forward_split", numerator=3, denominator=1,
                       effective_date=DAYS[2], source_url="https://example.invalid/synthetic"))
    bp, ep, tp = ("capture-evidence/synthetic/" + leaf
                  for leaf in ("baseline.json", "envelope.json", "terminal.json"))
    terminal = B.encode(dict(status="CAPTURE_RETAINED_UNQUALIFIED", request_id="100-1",
        compute_sha="c" * 40, envelope_sha256=B.sha(envelope), raw_response_sha256=B.sha(raw)))
    admission = B.encode(dict(schema_version=1, kind="provider_snapshot_price_admission",
        status="ADMITTED_PRICE_ONLY", basis_id=snap["basis_id"], allowed_fields=list(B.PRICE_FIELDS),
        volume_policy="BLOCKED_UNRESOLVED", admitted_at=CUTOFF + "T12:00:01Z",
        proof_model="reviewed_workflow_receipt_not_provider_signature",
        envelope_ref=dict(path=ep, blob_sha=B.blob_sha(envelope)), workflow_evidence=dict(
        compute_sha="c" * 40, request_id="100-1", terminal_ref=dict(path=tp, blob_sha=B.blob_sha(terminal))),
        unresolved_volume_dates=snap["unresolved_volume_dates"]))
    snapshot = B.encode(snap)
    cat = dict(schema_version=1, provider=B.PROVIDER, symbol="SOXX", basis_id=snap["basis_id"],
               snapshot_blob_sha=B.blob_sha(snapshot), admission_blob_sha=B.blob_sha(admission))
    files = {bp: baseline, ep: envelope, tp: terminal,
             B.catalog_path(B.PROVIDER, "SOXX"): B.encode(cat),
             B.snapshot_path(B.PROVIDER, "SOXX", cat["basis_id"]): snapshot,
             B.admission_path(B.PROVIDER, "SOXX", cat["admission_blob_sha"]): admission}
    for p, value in files.items():
        dest = repo / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(value)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthesized admitted seed")
    return req, cat, bp, baseline


def authority_fixture(root, receipt_end):
    seed = root / "seed"
    req, cat, bp, baseline = initial_scope(seed)
    local = LocalGitStore(seed)
    base = local.head()
    slot = dict(slot_id="fixture", execution_day=DAY, cutoff=CUTOFF,
                valid_from=DAY + "T12:00:00Z", valid_until=DAY + "T12:30:00Z",
                cleanup_until=DAY + "T12:40:00Z", receipt_until=DAY + "T23:00:00Z")
    source = "https://www.nyse.com/trade/hours-calendars"
    rows = [dict(trade_date=d, scheduled_close="16:00") for d in DAYS]
    native = B.encode(dict(schema_version=1, kind="reviewed_finite_daily_calendar",
        source_url=source, known_at=CUTOFF + "T20:00:00Z", sessions=rows[:-1]))
    lookahead = B.encode(dict(schema_version=1, kind="reviewed_manual_soxx_lookahead",
        source_url=source, known_at=CUTOFF + "T20:00:00Z", native_calendar_sha256=B.sha(native),
        cutoff=CUTOFF, days=rows[-2:]))
    bind = dict(native_calendar_sha256=B.sha(native), lookahead_sha256=B.sha(lookahead))
    grant = B.encode(dict(schema_version=1, kind="finite_daily_basis_grant",
        repository=STORE_REPOSITORY, repository_id=REPO_ID, owner_id=OWNER_ID,
        compute_sha=COMPUTE, consumer_sha=CONSUMER, workflow_source_sha=base,
        workflow_id=21, calendar_sha256=B.sha(native), initial_selector=cat | {"read_sha": base},
        baseline_ref=dict(commit=base, path=bp, blob_sha=B.blob_sha(baseline), sha256=B.sha(baseline)),
        request_template=req, slots=[slot], request_limit=1, credit_limit=1, verification_limit=1,
        hard_credits_per_day=200, hard_credits_per_minute=8, maturity_next_day_et="07:00",
        private_destination="capture-evidence/daily-basis", actions_free_only=True,
        allowed_actions=["capture", "private_verify", "immutable_catalog_update"],
        volume_policy="BLOCKED_UNRESOLVED"))
    with local.lock():
        desc = local.commit(base, {ROOT + "grant.json": grant, ROOT + "native-calendar.json": native,
            ROOT + "lookahead.json": lookahead, ROOT + "descriptor.json": B.encode(dict(
                schema_version=1, kind="manual_soxx_repair_descriptor", symbol="SOXX", start_date=CUTOFF,
                end_date=CUTOFF, compute_sha=COMPUTE, consumer_sha=CONSUMER, verifier_source_sha=base,
                base_store_sha=base, grant_sha256=B.sha(grant), **bind))}, replace=set())
        origin = P.ManualReceiptOrigin(COMPUTE, base, desc, REPO_ID, OWNER_ID)
        receipt = P.canonical_manual_repair_receipt(dict(schema_version=1, kind="manual_soxx_repair_receipt",
            route="soxx_manual_repair", symbol="SOXX", compute_sha=COMPUTE, authority_store_sha=base,
            descriptor_commit=desc, origin=dict(repository=STORE_REPOSITORY, repository_id=REPO_ID,
            owner_id=OWNER_ID), start_date=CUTOFF, end_date=CUTOFF, **bind,
            valid_from=slot["valid_from"], valid_until=DAY + "T" + receipt_end + "Z", nonce="8" * 64))
        commitment = P.manual_repair_commitment(receipt)
        carrier = local.commit(desc, {ROOT + "receipt.json": receipt}, replace=set())
        local.cas("refs/heads/basis-daily-authority/" + COMPUTE + "/" + commitment, carrier, "0" * 40)
        for pin in (base, desc):
            git(seed, "update-ref", "refs/tags/market-data-read/" + pin, pin)
        verifier = "refs/heads/basis-consumer-delivery/" + base
        git(seed, "update-ref", verifier, base)
    bare, checkout = root / "remote.git", root / "checkout"
    git(root, "clone", "--bare", "--no-hardlinks", str(seed), str(bare))
    git(root, "clone", "--no-hardlinks", str(bare), str(checkout))
    git(checkout, "remote", "set-url", "origin", STORE_URL)
    transport = FileTransport(checkout, bare)
    store = RemoteGitStore(checkout, "fixture-not-a-credential", source_recheck=lambda: None,
            platform_guard=lambda: None, expected_repository_id=REPO_ID,
            expected_owner_id=OWNER_ID, transport=transport)
    approved = M.ApprovedAuthorityRead(origin, carrier, commitment, CONSUMER, base, B.sha(grant))
    return store, transport, bare, approved, M.CurrentRuntimePins(COMPUTE, CONSUMER, base), verifier


class TestManualLoaderExpiry(TestCase):
    def test_final_native_io_requires_fresh_manual_preflight(self):
        cases = (
            ("12:15:00", "12:15:00", timedelta(), True),
            ("12:40:00", "12:30:00", timedelta(), True),
            ("12:15:00", "12:15:00", timedelta(microseconds=1), False),
        )
        for receipt_end, final_time, epsilon, hold in cases:
            with self.subTest(receipt_end=receipt_end, final_time=final_time, epsilon=epsilon), TemporaryDirectory() as tmp:
                store, transport, bare, approved, pins, verifier = authority_fixture(Path(tmp), receipt_end)
                before = git(bare, "show-ref")
                now = [B._stamp(DAY + "T12:05:01Z")]
                real_read = store.remote_ref
                probes = []

                def observed_ref(ref):
                    result = real_read(ref)
                    if ref == verifier:
                        probes.append(result)
                        if len(probes) == 2:
                            now[0] = B._stamp(DAY + "T" + final_time + "Z") - epsilon
                    return result

                def load():
                    return M.load_manual_preflight(store=store, approved=approved,
                               observe_current=lambda: pins, clock=lambda: now[0])

                with store.lock(), patch.object(store, "remote_ref", side_effect=observed_ref):
                    if hold:
                        with self.assertRaises(M.LoaderHeld):
                            load()
                    else:
                        result = load()
                        self.assertFalse(result.authorization_authenticated or result.production_activation)
                self.assertEqual(probes, [pins.verifier_source_sha] * 2)
                self.assertEqual(git(bare, "show-ref"), before)
                self.assertFalse(transport.pushes)
