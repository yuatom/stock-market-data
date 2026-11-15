# Dynamic intraday durable accounting release candidate

Base: `bc6a85b71ead7f4eebdf90dd193c59b7b63c34b4`.
Scope: four source files, three focused test files, evidence/release notes.
No workflow, permission, secret, provider plan, fixed universe, daily collection
scope, schedule, historical data, SOXX or report binding change.

## Activation and bounded acceptance
The existing dynamic reusable runtime resolves Compute main. A normal merge makes
this code available for subsequent already-authorized business requests; this release
must NOT create a request, dispatch a workflow, replay an existing request or perform
a paid probe. The runtime is workflow_call-only. Check that main has not diverged before
publishing, and review any actual delta rather than forcing a branch over it.

Offline evidence supports release without an additional provider probe: 37 integration/
regression tests passed on the original candidate; the final metadata increment passed
3 focused tests plus known-rejection integration (4 total), and an independent reviewer
passed 4 tests including final STOPPED closure. Official time-series response schema
supports exact symbol/1min metadata. These are NOT live account or hosted runtime
acceptance results. Do not describe a code merge as live collection completion.

For the next naturally authorized dynamic request, observe the existing run only:
reservation before HTTP, retained raw, selected capture, terminal, exact final durable
Store head. Distinguish a prior symbol's durable capture from a completed batch snapshot.
Failure/unknown stops the sequence; do not redispatch to obtain a green acceptance.
No new request is authorized by this document.

## Behavior and operational limits
Only dynamic guarded fallback receives durable predebit and per-symbol closure.
Fixed/default collection and whole-provider election remain unchanged. Valid unselected
fallback is charged/reserved and retained even if Nasdaq remains selected. Known
retained response rejection stops later fallback; unknown state latches without further
publication. Reservations do not prove billing; do not refund automatically.

Existing 8-minute dynamic job limit remains unchanged. Added durable publications may
increase latency; no production latency claim is made. A timeout preserves published
claims/credits and requires state-specific diagnosis, not a blind replay. No timeout
extension, concurrency expansion or permission increase is included.

Original parser's missing-volume/malformed-row behavior remains outside this patch.
Configured timezone is currently America/New_York; parser is not generalized to other
zones. Nasdaq raw retention/crash recovery is not added. Live intraday metadata remains
unobserved in retained account evidence, although official-schema compatibility is
supported. Keep these limitations visible rather than adding a mandatory paid probe.

## Safe rollback
Do not delete claims, raw evidence, debits, captures or terminal records. After a new
intraday attempt has started, simply reverting to the old unguarded fallback is unsafe:
it would not honor the new intraday claim namespace and could spend again. First stop
new dynamic fallback dispatch through the existing approved operational control; then
fix forward or use a rollback which preserves guard/claim enforcement. Do not cancel
an in-flight writer or reset Store history. Before any actual activation reversal,
inspect whether a run/claim exists and use an explicitly scoped action. This document
itself does not change production controls.
