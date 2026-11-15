# Local same-process SOXX readiness integration

This candidate is not deployed, registered, or live-tested. No provider or remote
operation was performed by its implementation tests.

The optional four CLI inputs must be present together, equal the optional closed
`readiness` object in the private immutable scope (commitment, runner_id,
runner_name, expires), and cannot accompany `preflight_only=true`. Old scopes and
empty input groups preserve the previous behavior. The execution still requires
one SOXX request / one credit / one final verification, cutoff 2026-11-13 and
BLOCKED_UNRESOLVED volume. No calendar or price-history policy changes.

The new branch validates source, current owner/run, private scope, full grant,
baseline, no prior claim, and budget before invoking the existing readiness probe.
The probe accepts only two fixed caller profiles (isolated/probe and
one-shot/soxx_gap), never an arbitrary caller path. It dispatches the fixed private
workflow once and verifies its exact receipt and actual runner. The same API
object then supplies its short-lived successful-dispatch proof to the existing
readiness gate. Original execution rechecks freshness and preclaim conditions.
The final verifier uses the same API object and must run on the same exact runner
ID/name/labels before promotion. Expiry is checked before capture, verification
and promotion. Failed/unknown probe never enters collection or retries POST.

**At most two private jobs per invocation:** one zero-provider readiness delivery,
then (only after successful collection) one final delivery. This is not two
provider requests. The runner must remain available for both jobs; an ephemeral
single-job runner cannot satisfy this protocol. Runner registration/lifecycle,
paid-runtime authorization and cleanup belong to the separate authorized control
plane; this code neither provisions nor relaxes runner checks. No admin permission
is requested by the hosted job. A readiness success is not a guarantee of the
later provider/verification outcome and does not authorize automatic retry.

Tests mock platform/transport and retain existing production validators. They do
not establish live token capability, caller fields or runner availability.
