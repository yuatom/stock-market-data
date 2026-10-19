# Guarded historical recovery

Task: `yuatom/stock-dairy#166`; acceptance: Issue166 comment6250427645.
Owners remain `config/market-data-store.yaml`, `config/collection-universe.json`
and `config/store-publication-durability.yaml`. The existing maintenance mode is
`daily_baseline_rebase`, not a new scheduled mode or another provider.

## Candidate status and intended native route

**PR CANDIDATE WIRED; NOT ACTIVE ON MAIN:** PR #46 contains the recovery script,
tests and workflow integration, but remains Draft and unmerged. The route is not
available from `main` until this candidate passes validation and review and is
merged. Do not use the current legacy rebase route as a substitute. The workflow
tests require the ordinary publisher to exclude this mode and the guarded job to
call the recovery wrapper.

After the candidate is accepted and merged, use the existing
`.github/workflows/market-data-collector.yml` on `main` with the existing
`daily_baseline_rebase` mode: the guarded recovery job excludes the generic
writer. Supply current ET `trade_date`, full current `expected_compute_sha` and
full current `expected_store_sha` from their respective repositories. The
current-day 07:00 ET maturity gate applies. Queued drift stops, never silently
repins.

This is an explicit historical-write operation, not the read-only admission.
Ordinary continuation, admission PASS, source merge and successful CI do not
constitute its controlled trigger authorization. Do not dispatch merely to
validate source. Secrets are injected by Actions; no local provider key, workflow
input key or public payload artifact is required. The existing shared
`market-data-collector` concurrency group and 200/day, 8/minute limits remain.
Unrelated consumers of the same provider account still require coordination.

## Acquisition and publication boundary

The wrapper reuses the strict provider reader, replacement comparison, durable
predebits and request-boundary guards exercised by admission. It qualifies the
whole current collection universe, preserving last-three-existing-sessions /
max-three revisions and all retained dates; it does not reuse an old PASS to
permit different inputs. Original shard paths and archive collisions are checked
before spending credits. All replacement rows are acquired before rebuilding.

Only the current-day budget and unique recovery diagnostic can be written during
acquisition. The budget-writing checkout never receives rebuilt market facts.
An isolated disposable Git worktree starts from its last durable budget commit.
In that worktree the existing rebase helpers archive the entire old logical
history and index, rebuild shards, then re-read every target history, verify
hashes, old archives, identity, row counts and allowed paths. Inventory is derived
only after full history validation; inventory alone is not that validation.

Only one complete fact commit is published, by a no-force fast-forward push with
no pull/rebase retry. All resulting target indexes/shards, new old-history
archives, inventory and diagnostic are declared by exact path/blob in the
required manifest carried by the existing day-budget JSON. The real verifier
must return the exact nonempty closure and retained read reference before the
CLI reports PASS. Credit/minute counters are preserved when declarations change.
Neither sessions nor snapshots nor older recovery archives may change.

## Failure and reconciliation

Failure during qualification/staging publishes no partial market-fact history;
the original remote histories remain intact and consumed budgets remain charged.
An interruption can leave an IN_PROGRESS operational diagnostic; it is not PASS.
Cleanup removes only the disposable local worktree, never production refs.

The immutable pre-push diagnostic says `CANDIDATE_VALIDATED`, not self-asserted
publication success. Its `publication_requires_terminal_receipt=true` means that
only the matching successful producer receipt and independently read-back Store
objects establish publication. The budget is also its required-output manifest.

After any attempted fact push, failed acknowledgement or closure verification
returns nonzero with `publication_outcome=UNCONFIRMED` and the exact candidate
commit. No retry, second metadata push, automatic refund or automatic rollback
is performed. Read actual Store main, candidate, retention ref and required
manifest first. If the complete candidate landed, reconcile that exact commit;
do not overwrite later writers or repeat provider requests blindly. If it did
not land, preserve the already charged ledger and obtain a new explicit target
before another attempt. Any later reversal is a separately authorized forward
recovery based on the immutable old archives, never a force-reset of Store main.

These protections apply to the supported primary native route. The low-level
`rebase_market_data_baseline.py` remains a helper with non-transactional local
filesystem operations and legacy standalone result semantics. This change does
not claim universal transactionality or repair every standalone caller.

## Validation and acceptance boundaries

Required source validation is the full native `python -m pytest tests -v` on the
exact source candidate, with no skipped required cases. Recovery tests combine
synthetic HTTP/clock/failure injection with real Store helpers, temporary Git
remotes and the actual required-output verifier. They include late-symbol
rejection, archive/shard corruption, staging interruption, unexpected paths,
source drift, post-push closure failure and one complete fact commit.

A source PASS is not real recovery. A successful recovery publishes qualified
history only; verify resulting dates, archive integrity and ordinary append-only
EOD operation separately. It does not create a research Snapshot, Frozen Input,
Research result, Publication or Receipt and does not close the five-stage task.
