# Private fixed daily tail: 2026-11-20

This is a one-date, fixed-scope owner-only manual workflow. It is not the daily
schedule or a generic replay API. An independently reviewed and merged Compute
main tree plus a separately authorized private grant are necessary. Creating,
publishing, or checking in this source does not create the grant or dispatch.

## Public boundary

The only workflow inputs are exact reviewed public Compute main SHA and opaque
salted commitment in the existing private receipt domain. No private ref,
Store SHA, selector, per-symbol receipts or provider payload is a workflow
input, output, environment field, action input, or log. Existing credential
secrets are available at execution only. The literal Compute repository
`market-data-collector` queue is shared with all existing scheduled writers:
`cancel-in-progress: false`, `queue: max`. The similarly named group in
another repository has no bearing on exclusion. Keep ordinary EOD unchanged.

## Private contract

Follow the existing independent creation/readback protocol for a private
`capture_authority_receipt` with route `fixed_daily_tail_20261120`. The receipt
commits to the reviewed Compute and Store preimage, exact descriptor commit
and a fresh private salt. The off-main descriptor is one normal added file
over the exact Store main preimage, kept off main by the runtime. Its approval
is canonical JSON with a bounded schema: source and Store identities, ET
accounting date and bounded validity, retained date 2026-11-19, cutoff
2026-11-20, exactly ten approved fixed identities with exact index blob
identities, request and credit ceilings of ten, and no other keys. PLTR and
TSLA are not admitted. Do not infer authority from a found ref, credentials,
or working code. Operators must approve the exact private creation/readback
and independently approve the opaque commitment before any dispatch.

The workflow rechecks owner, manual main, first attempt, exact source and
native maturity. After entering the Compute queue, it checks exact Store
main, unchanged retained history with no target date, and current ledger
headroom. EOD first or any drift prevents a write. No historical price/volume
replacement or broad selection is allowed. Original native per-request
maturity, debit, quota and publication checks stay in force.

## Handoff and outputs

The private one-shot batch claim uses one stable key for the independently
approved commitment, with a fresh physical attempt only in the payload.
A non-force publish, exact single-parent/add-only market-unchanged new head
and durable tag readback must all succeed. Native starts with the verified
operational head, not the original approved preimage. Ambiguous claim or
publication means stop, inspect private durable evidence, and never
automatically repeat (including a fresh workflow run). A pre-main failure
leaves no Store fact; do not create a re-salted grant to bypass uncertainty.

Independent plan, ten child manifests and original-series closure, exact
budget, ten checkpoints and retention tags are re-read privately before
declaring completion. Public statuses contain no private fields:

- `PRIVATE_FIXED_TAIL_BLOCKED`: stopped before the claim boundary.
- `PRIVATE_FIXED_TAIL_OUTCOME_UNKNOWN_NO_RETRY`: a claim may already have been
  spent or verified append outcome is unavailable. Do not infer zero effects.
- `PRIVATE_FIXED_TAIL_PRICE_ONLY_VOLUME_UNRESOLVED_NO_RETRY`: ten durable
  price rows proven; volume remains isolated, so this is not full completion.
- `PRIVATE_FIXED_TAIL_COMPLETE`: ten complete durable appends and normal
  volume policy verified. Actual cancellation stays cancellation.

Local regression tests use only fabricated data and real file-only Git.
Hosted private transport, actual permissions, and the exact reviewed,
authorized private receipt must be checked independently before activation.
