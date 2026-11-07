# Guarded historical recovery

Task: `yuatom/stock-dairy#166`; acceptance: Issue166 comment6250427645.
Owners remain `config/market-data-store.yaml`, `config/collection-universe.json`
and `config/store-publication-durability.yaml`. The existing maintenance mode is
`daily_baseline_rebase`, not a new scheduled mode or another provider.

## Native route and execution preconditions

The supported source entry point is
`.github/workflows/market-data-collector.yml` on the current `main` branch,
with mode `daily_baseline_rebase`. The guarded recovery job excludes the generic
writer and validates the exact Compute and Store commit inputs before it calls
the recovery wrapper. Use the fixed or dynamic selector only when its target
proof is valid for the pinned Store commit; omitted selectors preserve their
documented default behavior.

Before an authorized run, verify that the workflow and recovery contract exist
at the exact Compute SHA being supplied, that the Store SHA is the intended
current Store main, and that the ET trade date has passed the 07:00 ET maturity
gate. Queued drift stops; the operation never silently repins. This runbook
documents an entry point but does not authorize a dispatch. Source merge,
successful CI, and admission PASS are not trigger authorization.

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
Before the first request predebit, recovery validates the current-day ledger and
requires enough remaining daily credits for every resolved target (one credit
per target). An unfundable batch stops with `budget_exhausted`, without spending
credits or calling the provider. Initial and terminal diagnostics may still be
published, preserving credit/minute counters. This is a preflight, not a credit
reservation: existing per-request time, minute-budget, source/Store identity and
durability guards remain mandatory. It applies to default, fixed and dynamic
recovery selections; it does not change admission or ordinary collectors.
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
the original remote histories remain intact and pre-debited ledger entries are retained.
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
not land, preserve the already pre-debited ledger and obtain a new explicit target
before another attempt. Any later reversal is a separately authorized forward
recovery based on the immutable old archives, never a force-reset of Store main.

### Safe terminal failure evidence

The CLI's nonzero `BLOCKED` JSON is the terminal execution diagnostic. A retained
Store report may still be an older `IN_PROGRESS` snapshot with zero counters.
That snapshot does not establish zero requests or zero predebits for the run.

For failed budget, diagnostic, or fact publication, the CLI preserves:

- `publication_kind`: `budget_predebit`, `diagnostic`, or `market_facts`.
- `publication_phase`: `main_push`, `main_readback`, or `durability_verification`.
- The candidate commit, expected retention ref, and last fully verified commit.
  The verified writer head is never advanced solely to permit another report.
- `main_publication_outcome=CONFIRMED` only after an exact matching main readback.
  This is a past observation, not a claim that main cannot subsequently move.
  `UNCONFIRMED` does not mean rejected or absent. A successful push acknowledgement
  alone is not an exact readback. Failed verification leaves durability unconfirmed.
- The original fixed publication/durability failure code and, when emitted by
  the verifier, its allowlisted `verifier_reason`. Unknown/malformed verifier
  messages become `STORE_PUBLICATION_VALIDATION_FAILED`; URLs, arbitrary stderr,
  provider payloads and exception text are never copied into the public summary.

Counters are run-local evidence, not provider billing or receipt evidence:

- `budget_debits_confirmed` (also `budget_debits`) counts predebits for which
  publication and durability verification completed, including a debit whose
  later blob/time gate failed. `budget_debits_unconfirmed` records a locally
  validated debit whose publication did not return a durable receipt.
- `budget_debits_attempted` is those two counts combined, not the number of
  calls to the debit helper or a replacement for the persisted day ledger.
  `predebit_credits_before/after`, when present, are the validated local ledger
  values around the failing debit. Reconcile the exact candidate on Store to
  establish whether that debit landed; never refund an uncertain debit.
- `requests_attempted` counts successful local pre-HTTP guards. It does not
  prove a network request was transmitted, received, completed or billed.

After an uncertain metadata or fact publication, `terminal_report_outcome` is
`NOT_ATTEMPTED`: no follow-on diagnostic publication or automatic retry is made.
If publication was already confirmed and a later provider/guard/blob operation
fails, the existing guarded `BLOCKED` report path remains. Its outcome is
`CONFIRMED` or `UNCONFIRMED`; a failed report retains separate safe failure
evidence without replacing the original failure. No additional Store writes,
provider requests, retention-tag repair, refunds or weaker integrity checks are
introduced to obtain these diagnostics.

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


## Exact fixed-universe subset selection

The guarded recovery wrapper accepts an optional `fixed_selection` workflow
input (or `--fixed-selection` in the wrapper CLI). Its JSON shape is:

```json
{
  "schema_version": 1,
  "targets": [
    {"symbol": "SYMBOL", "index_blob_sha": "40-lowercase-hex-git-blob-sha"}
  ]
}
```

The target array must be nonempty and unique. Every symbol must belong to the
pinned Compute fixed daily universe; every index SHA must identify that
symbol's `_index.json` in the exact `expected_store_sha`. The resolver also
checks provider, asset class, session, interval, semantics, and identity start
date before any predebit or Provider request. Fixed and dynamic selectors
cannot be combined. A malformed or stale explicit selector fails closed and
never falls back to the full fixed universe. Omitting both selectors preserves
the existing all-fixed-universe default. Existing dynamic-selection provenance
rules remain unchanged.

The selector scopes the work only. It does not approve changed historical bars.
The existing revision limits, full-history archive, identity checks, durable
predebits, atomic Store publication, immutable pin, manifest and remote
readback requirements still decide whether an attempted recovery can proceed.
The selector does not confer persistent or future execution authority.

For a one-time selected recovery, after the source is merged and after the user
authorizes the exact run, use the workflow above with mode
`daily_baseline_rebase`, today's ET trade date after 07:00 ET, current exact
Compute main SHA, current exact Store main SHA, and a selector containing the
approved symbols plus their index blob SHAs from that pinned Store commit.
Leave `dynamic_selection` empty. A selection of 21 targets means 21 provider
requests and uses the existing 200/day, 8/minute limits; no symbol outside the
provided list is rewritten. The other fixed symbols remain on normal
append-only EOD. A recovery PASS still requires all current quality and
publication checks and is not a claim that research outputs are complete.
