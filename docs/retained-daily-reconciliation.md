# Retained daily append reconciliation

This is a distinct, explicit maintenance route, not a retry of `daily_baseline_rebase` or `daily_symbol_maintenance`. Its owners are `config/market-data-store.yaml`, `config/store-publication-durability.yaml`, and `schemas/retained-daily-reconciliation.schema.json`.

Source implementation, tests, review, merge, or a validated request do not authorize a production dispatch. An operator must separately approve the exact transaction before using the manual workflow. No schedule, automatic resume, provider request, debit or refund exists in this route.

## Deliberately fixed scope

The initial schema permits only the explicitly reviewed GLD case:

- Current Store commit: `4b511e8c8d211adfaff0e1ddbcce75c070a32781`.
- Retained candidate commit: `e2b6227de5d026e1d01216c3c5ed6a63c9d670ee`.
- Candidate tail blob: `e63ca19a19ae8b0f948a6b8c38eabda03127fb6a`.
- Six additions: November 2, 3, 4, 5, 6, and 9, 2026.
- All 310 existing records remain unchanged. The known October 30 volume disagreement is reported without values and resolved only by preserving the existing record; it is not silently accepted as a revision.

Every current shard/index pin, addition digest and conflict policy must match the schema. This is not permission for arbitrary retained candidates, symbols, dates or altered history. A changed production head is a blocker, not permission to silently repin. A later target needs a separately reviewed contract and authorization.

The retained candidate is readable as an exact immutable object. This route does not claim that candidate was successfully published or that the failed original run succeeded. Existing request counters and historical receipts remain evidence of their original transactions.

## Manual execution surface

`.github/workflows/reconcile-retained-daily.yml` accepts only `workflow_dispatch` on Compute `main`. Its job uses the same `market-data-collector` concurrency group as existing writers, with cancellation disabled. The exact reviewed Compute SHA must match the event and checkout; both current Store and retained source candidate are checked out by validated exact SHA. No provider secret is exposed.

The workflow first validates the value-free request, then materializes private Store checkouts. Request input is passed through environment variables and quoted arguments, never interpolated into executable shell source. The retained candidate checkout does not persist write credentials. The Store checkout is the sole potential publication origin.

`--validate-request-only` validates input without Store writes and returns only the two exact checkout identities as JSON. The executing CLI separately enforces the official workflow/event/ref/Compute identity and clean pinned checkouts. Validation alone is not a successful operation or dispatch authorization.

## Write and proof boundaries

One complete forward candidate may contain only:

1. GLD's existing open shard and index, with six new rows and truthful append lineage.
2. Inventory derived from the full current Store candidate.
3. A new request-specific reconciliation report under `collector-state/retained-daily-reconciliation/`.
4. Its separate output manifest in the same namespace.

All original records, sealed shards, prior baseline/lineage, original recovery archives and receipts, other assets, sessions and snapshots remain unchanged. Every budget file remains byte-identical: no old 67-credit ledger replaces the later 200-credit state, and the retained earlier debit is neither charged again nor refunded.

The immutable report initially says `CANDIDATE_VALIDATED` and requires a terminal receipt. Independently derived required output closure includes GLD's index and every shard, the inventory, and new report. The manifest is separate from accounting. Before publication, the implementation rechecks immutable history/additions/provenance and allowed changes, rather than trusting declared hashes alone.

Only one no-force fact push is allowed. Actual Store main must still equal the pinned parent under the shared writer group. Terminal `VERIFIED` is available only after the existing verifier reads exact main, fetches the immutable candidate, verifies object closure, creates or confirms its deterministic retention tag, and reads that tag back. This proves only this append-reconciliation publication, not completeness of the original batch or downstream research.

## Uncertain outcomes

Any attempted-push failure or incomplete publication proof returns an unconfirmed outcome with the candidate identity. The producer does not retry the fact push, write a second diagnostic commit, refund, reset or roll back. A retention failure can coexist with an advanced main; neither is hidden by reporting success. Inspect actual immutable objects and refs before deciding on any separately authorized follow-up.

## Verification and deployment boundary

Repository tests use synthetic values and temporary local bare Git remotes. Private provider rows must not enter this public Compute repository, test fixtures, logs or uploaded artifacts. Local test receipts are not production receipts. The source remains local until separately authorized publication; a source publication/merge and an actual Store dispatch are separate decisions.
