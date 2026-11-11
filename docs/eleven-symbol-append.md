# Bounded conditional append design

## Publication boundary
Public source contains no actual private Store commit/blob pins, record counts,
coverage dates or per-symbol file inventories. The supplied contract template is
intentionally non-executable. Public tests generate fabricated Store rows, dates,
counts and Git objects; these are not claims about any real Store.

## Private scope authority
The closed canonical approval identifies current Compute, current Store head,
private contract SHA256, public identity-registry digest, execution ET day,
validity window, historical cutoff, request/credit caps and private destination.
Its independently reviewed digest is stable across workflow attempts.

The exact Store head must contain one added object only:
`capture-contracts/<private-contract-sha256>.json`. Its sole parent must equal the
contract's pinned baseline Store commit. Local Git replacement refs and legacy
graft files are rejected; immutable authority reads disable replacement objects. The object must be a regular Git blob,
within the byte bound, with exact SHA256 and blob identity. This avoids embedding
a commit's identity inside its own contents and forbids arbitrary advancement.

The private object holds exact per-symbol baseline counts, first/last dates,
addition dates and index/shard blob pins. All pins are checked against the
immutable baseline before claims, debits or HTTP. Baseline index closure and all
existing records are verified again against the exact approved scope. No current
inventory, synthetic default, automatic descendant or caller boolean substitutes
for this private contract. Execution requires explicit approval of its digest.

Hard implementation ceilings are eleven fixed eligible symbols, one request and
one credit per symbol, at most sixteen additions per symbol, bounded provider
output size, and the existing daily/minute credit limits. These are ceilings,
not granted scope: the private contract determines the exact request and rows.
GLD and SOXX are excluded from this route.

## Capture and qualification
Verified public provider profiles admit proposed request identity only. The
retained response must match exact symbol, exchange, MIC, currency, ETF type and
timezone. No operating/segment MIC alias is silently applied. Explicit splits
and automatic-precision parameters are pinned. Current provider documentation
is not a historical rounding or adjustment certificate.

One response covers every approved existing date plus the exact new dates.
All overlapping OHLC values must match as decimals before float normalization.
Historical volume differences are audited without changing old values. Missing,
extra, duplicate or unordered dates, identity mismatch and invalid values reject
that symbol before fact publication. This establishes observed compatibility for
the bounded append, not universal semantics or split migration qualification.

## Serialized transaction
The manual-main workflow holds the existing market-data-collector concurrency
lock. Source, origin, exact Store and private-repository visibility are checked.
Authenticated GitHub metadata redirects are forbidden.

Create-only claim, request and attempt objects precede the existing durable debit.
No commit occurs between debit and the unchanged provider guard. Exact raw bytes
are retained privately before parsing. The shared retention adapter independently
verifies literal blob bytes, parent, single added path, remote main and retention
tag. Evidence remains outside market-data inventory under an approval-specific
capture-evidence prefix. No raw payload or credential is sent to public logs or
artifacts.

The pure builder preserves every old logical row, sealed shard, index metadata
and prior lineage. Only the active tail and index change; the fact transaction
also carries inventory, report and exact required-output manifest. Literal-byte
Git plumbing validates parent and global changed-path/blob closure before one
push. The unchanged verifier runs in a child with hooks/fsmonitor disabled.
Receipt checks require exact main, tag and independently derived fact closure.

Only certain pure validation failures may be isolated, after confirmed private
terminal retention. Unknown debit, HTTP, capture, publication or terminal outcomes
stop globally without retries, refunds, repair writes or another provider request.
Previously confirmed publications remain real. COMPLETE requires all eligible
symbols appended; PARTIAL/BLOCKED return nonzero with retained certain outcomes.

## Runtime bounds and execution status
There is one initial immutable market-data snapshot. Exact confirmed budget/fact
changes update that cache; evidence commits must preserve its tree identity.
The request deadline is unchanged. Local synthetic measurements are not a hosted
latency guarantee.

Implementation and tests are local only. Deployment, fresh exact private contract,
reviewed approval, available credits and authorized manual execution remain
separate actions. No live provider capture or production publication is claimed.


## Deployment gate
The checked-in workflow accepts only a public Compute SHA and requires a manual,
owner, main, first-attempt invocation. It then exits with
PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED before any checkout, credential access
or provider operation. It accepts no private dispatch inputs and emits no private
outputs. An approved private authority-input integration is unknown and must be
reviewed separately; no secret provisioning is performed or implied.

The runtime API is fully implemented for a future authorized private caller. Its
CLI emits only a coarse status and terminal-confirmation flag, never Store hashes,
per-symbol counts, dates, private receipt paths or detailed outcomes. Detailed
results remain in the private caller/Store context. Deployment-gated is not
activated or production-ready.
