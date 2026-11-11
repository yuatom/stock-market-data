# Immutable provider snapshots and explicit price-only views

This local source contract adds no hosted workflow, provider request, automatic
admission, activation or consumer release. Successful tests or a completed local
candidate are not permission to publish or activate data.

## Why a new identity

A provider can restate its historical split-adjusted prices after a corporate
action. Existing `provider:symbol:regular:1d` identity cannot express this vintage.
A mathematical split ratio does not establish exact vendor price rounding or
unrelated volume revisions. The old history is preserved, not rescaled.

`provider_basis_snapshot.prepare_snapshot` takes an externally anchored retained
envelope, baseline evidence bytes and a declared forward-split event. It retains
every literal provider OHLCV string in a content-addressed snapshot. The baseline
input is diagnostic evidence, not a claim that a derived file is the original
Store shard. All volume semantics remain blocked, including when ratios happen
to match. Differences are unresolved provider-vintage differences, not fabricated
corrections. The existing envelope and its false qualification claims never change.

`stage_snapshot` creates a new private local directory containing snapshot and
completion manifest. It refuses collisions, symlinks and changed readback. This
is local artifact completion, not hosted durability or market qualification.

## Private Store contracts, after separate authorization

Prospective separate paths, all private:

- `data/market-data/series-versions/daily/{provider}/{symbol}/{basis_id}/snapshot.json`
- `data/market-data/series-admissions/daily/{provider}/{symbol}/{admission_blob_sha}.json`
- `data/market-data/series-active/daily/{provider}/{symbol}.json`

The catalog binds a basis, snapshot and admission, not its own future commit.
Consumer selectors add the independently pinned Store read commit. A future
publisher must perform one reviewed CAS transaction, verify the exact output
closure and durable readback, and prove all legacy files remain byte-identical.
This module does not implement that publisher or mint an admission.

The independently reviewed admission permits only prices. It identifies the
retained envelope and terminal blobs, capture source/run and unresolved volume
dates. The reader verifies their exact Git bindings and consistency. This is
reviewed workflow evidence, not a cryptographic signature by the provider or a
proof of market truth. The runtime owner must separately verify the actual
workflow/source/receipt/durability evidence before admitting the dataset.

## Explicit reader

`read_price_view` requires a private explicit selector, a trusted runtime
`ReadAuthority`, requested OHLC fields, independently established expected
sessions and a knowledge cutoff. It reads immutable Git objects only. Dirty
worktree files cannot replace a pinned object. It rejects wrong/malformed
catalogs, wrong pins, unadmitted data, missing sessions, observation/admission
lookahead and every volume-dependent request. The result contains only requested
price fields. It does not synthesize missing dates or shorten the requested
window, does not claim a total-return series, and never falls back to legacy.

`ReadAuthority` is a runtime dependency, not an authorization token to accept from
a user-authored research draft. Private pins, data and exception dates must not
be copied into public workflow inputs, public source configuration or logs.

## Concrete private admission-pin delivery

The implemented local adapter reads an already-materialized normal private Git
ref `refs/heads/basis-read-receipts/{reviewed_compute_sha}/{opaque_commitment}`.
The receipt file is `basis-authority/receipt.json`. It contains the private exact
selector and admission pin, source SHA, bounded validity window and a fresh
random 32-byte nonce. Its public commitment uses a separate domain plus canonical
receipt bytes, matching the existing salted private-receipt pattern. The loader
checks exact ref type, canonical bytes, approved commitment, source and expiry.
No signing key or additional secret is introduced. Errors and object reprs are
value-free. Hash binding alone does not establish approval or nonce entropy.

The **remaining hosted adapter** must use existing Store credentials to verify
repository privacy and materialize only that exact approved receipt ref, then
call this loader under the reviewed Compute source. Public inputs may contain
only reviewed Compute SHA and the user-approved opaque commitment. The old
capture/append route enums are deliberately unchanged; this new route is not
silently accepted by existing hosted transport. Approval for a consumed capture
cannot authorize this receipt or another request.

## Compatibility and deployment order

With no catalog, current legacy APIs retain their prior behavior. Updated legacy
Store reads/writes and deterministic proof reads reject a catalog. Updated daily
collectors stop before debit or provider access. The companion stock-dairy
prewrite patch rejects a catalog at its frozen Store SHA before using legacy
roots, including malformed catalog content.

A catalog cannot compel an **older deployed binary** to implement these fences.
Before any Store activation, publish/review compatible Compute and the actual
stock-dairy runtime release, verify deployed pins, and drain relevant in-flight
old runs. Main and the runtime-release pointer are distinct authorities. Existing
historical artifacts pinned before activation remain historically readable.
The companion patch is a fence, not full price-view integration into Research
Input, deterministic metrics, frozen input sealing or the model-native runtime.
That full consumer adoption and its existing calendar/freshness gates require a
separate compatible source increment and release.

## Future coverage

Coverage ends at the final observed date. A later requested session needs new
source data under a separately authorized acquisition. Correct the inclusive
wire boundary before such a request; never retry a consumed capture grant.
Future extension must create a new immutable version with explicit predecessor,
exact overlap and admission policy; it cannot mutate this snapshot or the old
history. No scheduled collector adoption or automatic extension is added here.

## Tests

Fixtures are invented and contain no real private receipts, Store identities,
prices or exception dates. Tests exercise candidate integrity, exact decimal
strings, rejected OHLC, trusted selection, private receipt loading, malformed
catalogs, immutable Git reads, knowledge/date bounds, volume refusal, legacy
compatibility and fencing before provider debit. Full Compute-suite validation
and actual-consumer focused test results must accompany a proposed change.
