# Retained GLD Append Reconciliation Implementation Plan

> For agentic workers: implement in isolated local source only. No hosted writes, provider requests, dispatch or production execution.

**Goal:** Add a formal, manual-only retained-candidate GLD append reconciliation route with exact scope, preserved history/accounting, real shared writer concurrency, and verified terminal receipt.

**Architecture:** A standalone Python reconciliation module consumes an exact request contract plus pinned current/candidate Store checkouts. It validates every input before staging, creates one complete forward candidate, uses the existing no-force publication/verifier contract, and never calls a provider or debit routine. A dedicated workflow uses the existing market-data-collector concurrency group with cancellation disabled and no schedules.

**Tech Stack:** Existing Python 3.12, Git, JSON schema, GitHub Actions conventions. No new service or dependency family.

**Spec:** docs/retained-daily-reconciliation.md, implementing the approved retained-only append design in an isolated local project.

## Global Constraints

- No production action or hosted writes during development/test.
- Preserve every existing logical record, sealed shard, prior lineage, original receipt/archive, unrelated asset, and entire budget file.
- Only GLD additions 2026-11-02, 03, 04, 05, 06, 09 from candidate e2b6227de5d026e1d01216c3c5ed6a63c9d670ee.
- Provider requests/debits/refunds remain zero for this operation. Existing retained request accounting is not reset.
- Immutable identities, explicit conflict preservation and independent terminal closure are mandatory; no retries or silent repins.
- Production execution permission remains separate from source implementation and tests.

## Review Focus

- Duplicate-key/malformed or extra-scope requests fail before staging and push.
- Rehashed history/additions/provenance cannot bypass independent checks.
- Existing current-day and historical ledgers stay byte-identical.
- Main drift before/after push and lost acknowledgement cannot yield a false PASS or retry.
- Dedicated workflow cannot accidentally enter ordinary provider/debit routes; real shared concurrency must match all writers.

## Task 1: Formal reconciler and schema

Files: scripts/reconcile_retained_daily.py, schemas/retained-daily-reconciliation.schema.json, tests/test_retained_daily_reconciliation.py.

- [ ] Write failing request, no-overwrite, provenance, accounting and terminal-closure tests using synthetic fixtures.
- [ ] Implement strict request validation, exact pinned source/current reads, private isolated staging, real append helper, separate report/manifest, inventory regeneration and independently validated allowed changes.
- [ ] Implement single no-force publication with exact current main guards and real retention/output verifier. Require official workflow execution surface for production CLI; library methods support only explicit injected local fixtures in tests.
- [ ] Run focused tests, including refusal cases and no-retry behavior.

## Task 2: Manual workflow and contract documentation

Files: .github/workflows/reconcile-retained-daily.yml, docs/retained-daily-reconciliation.md, tests/test_retained_reconciliation_workflow.py.

- [ ] Test manual-only trigger, exact pins, no provider secret, shared concurrency, no generic collector invocation and argv injection safety.
- [ ] Implement dedicated workflow with current Compute/event validation, pinned current/candidate Store checkout and no-force publisher invocation under market-data-collector lock.
- [ ] Document explicit authorization and write/output boundaries, current target limitations and uncertain-outcome handling.

## Task 3: Final verification and review

- [ ] Run all native contract tests and compile changed Python.
- [ ] Independently review complete diff and address findings with regression tests.
- [ ] Record exact local patch identity, test results and remaining source-publication/production authorization boundaries.
