# Dynamic intraday accounting: offline evidence and smallest proposed repair

Base: published Compute bc6a85b71ead7f4eebdf90dd193c59b7b63c34b4.
Initial observation stage changed no production code; local implementation now follows below. No API/provider request,
SOXX replay, production Store mutation or publication was made. Seven targeted
observations execute real existing functions with synthetic transport and sockets
forbidden. A passing test documents the current gap, NOT a repaired production path.

## Actual call graph
`run_dynamic_guarded.main` -> `collect_request` -> daily durable per-symbol closures
-> shared `market_data_collection.collect_regular_window`:
1. Nasdaq requests run concurrently, bounded by max_workers=4. Both fixed and dynamic
   use this same function. A Nasdaq exception is recorded and does not stop others.
2. Missing or sparse primary windows trigger serial Twelve fallback if contract permits.
   Full alternate replaces whole symbol/window only, never splices providers. Complete
   primary skips fallback. Preserve this selection policy.
3. `fetch_twelve_regular`: secret check -> `wait_for_twelve_budget` -> `_http_json`
   -> local `consume_twelve_credit(1)` -> provider status/row parsing. It does not use
   bounded `_http_raw`, before_request callback, remote predebit or raw retention.
4. Broad exception handler records failure and continues the next symbol. After the
   entire provider phase, captures and snapshot/latest are written. Guarded dynamic CLI
   finalizes result/inventory/manifest using its current verified Store head.

## Observed failure/accounting boundaries
- At successful HTTP invocation the local credits are still 0; afterward 1.
- Parsed provider error JSON consumes 1 locally even though rejected as market facts.
- Timeout and JSON decode failure occur before consume. Repeated same request makes
  another HTTP attempt, with no claim and credits still 0. This is UNKNOWN provider
  charge, never evidence of free calls.
- Real shared loop continues AAA -> BBB after Twelve timeout, local credits stay 0.
- Minute budget can become exhausted after HTTP and before consume, causing a post-call
  rejection. Test injects ledger contention during transport; it demonstrates order,
  not observed real production concurrency. Existing shared writer lock limits parallel
  workflows but cannot eliminate process crashes or provider-time accounting changes.
- Existing daily limit 200 blocks HTTP when already exhausted; minute limit is 8.
  Minute keys use current UTC time; daily filename uses request trade_date. Intraday
  wait receives no deadline, so minute exhaustion can wait until workflow cancellation.
  Guarded outer owner enforces current NY request date, but shared fixed callers have
  separate policies. Do not change these globally in a dynamic patch.
- `_http_json` uses urlopen/read/JSON decoding directly, no strict raw transport bounds.
  Existing parsers skip malformed rows; missing volume may become zero. These quality
  gaps are separate from predebit and must not be silently called solved.

## Original minimum implementation plan (superseded by local candidate below)
An explicit optional dynamic intraday owner argument, default None, at the shared
collection/fetch seams; fixed callers remain byte-for-byte behaviorally unchanged.
Only the dynamic invocation opts in. Avoid monkeypatch/global state in production.

Reservation identity: immutable authorized business request ID + symbol + Twelve
provider + stage + trade date + exact start/end window + interval. Dedicated intraday
claim namespace, distinct from existing daily request+symbol claim. No run ID/time
in business key. Reuse exact source owner provenance, StorePublisher CAS/durability,
ledger+claim same commit, and tight guard immediately before bounded transport.
A separate owner phase or scoped reservation extension is needed: simply calling
existing daily `reserve` will reject the same symbol already claimed for daily.

Twelve flow: durable claim+conservative credit -> guard -> single bounded raw HTTP ->
raw durable -> parse -> known terminal/capture closure. Remove post-fetch consume only
from guarded branch to avoid double debit. Unknown remote/local I/O/publish must use
DynamicPublicationUnknown and explicitly escape both shared broad catches and dynamic
outer catch. No fallback next symbol after unknown; no retry/refund/claim deletion.
Known deterministic quality rejection may close evidence, then continue only if the
budget/transport/publication status is known. No claim means a Nasdaq-only result is
not charged to Twelve. Nasdaq provider selection and concurrency need no rewrite.

Raw response/terminal can close before next symbol; selected normalized capture must
also close durably before next request. Current shared code materializes captures only
AFTER all fallbacks, so a real guarded callback needs a narrow capture-materialization
seam (same existing write_capture semantics) rather than pretending raw-only closure
is a complete market fact. Final snapshot uses those exact refs and current owner head;
existing final inventory closure remains single CAS/no outer re-push.

Future fault matrix: duplicate across runs, daily vs intraday claim noncollision,
unknown predebit -> zero HTTP, real guard minute/day expiration -> reservation retained
but zero HTTP, raw/capture publication unknown -> no later symbol, hard exit after HTTP
-> durable claim prevents replay, known provider error retained, no post-fetch double
debit, Nasdaq full-window -> zero Twelve credits, sparse-primary fallback selection
unchanged, fixed default regression. No production readiness claim until these pass.


## Local implementation candidate (not published)
- Four production source files only: existing owner gains explicit intraday phase;
  original daily defaults retain their mode/claim bytes and paths. Same atomic ledger
  publisher and 200/day, 8/minute budgets. No separate state service.
- Claim identity includes immutable business request, symbol, provider, stage, business
  date, window and interval; does NOT include execution run/time. Different namespace
  from daily claim. Current accounting date/guard still belongs to existing owner.
- Shared collector accepts optional owner. Only dynamic caller supplies it; Nasdaq
  parallelism and whole-window election remain unchanged. Valid unselected fallback
  retains raw/terminal/reservation while selected capture still names Nasdaq.
- Bounded raw HTTP callback follows durable debit; no guarded post-consume. Raw path
  is checked before write; same existing bytes are not rewritten. Raw is durable before
  parse. Known bad JSON/provider error/metadata rejection closes REJECTED then stops
  future paid requests. It becomes business STOPPED, not publication unknown.
- Partial I/O, guard/publication uncertainty latch and escape through all collectors.
  No later terminal/push/paid request. Reserved credits remain, actual provider charge
  remains NOT_VERIFIED. No automatic refund/retry/claim cleanup.
- Narrow existing capture helper writes selected capture before next paid attempt;
  already-durable capture refs are reused in final snapshot, never regenerated with
  a new timestamp. Pure complete Nasdaq symbols keep original final capture path;
  Nasdaq raw retention and its crash recovery are NOT claimed solved.
- Capture path is checked before read/write, and closure binds actual provider,
  session, exact authorized window, stage-symbol capture ID and symbol facts. It
  does not reimplement shared per-row coverage or source election.
- `intraday_execution` distinguishes conservative reservations, selected/unselected/
  rejected outcomes and whether snapshot was produced. Earlier durable captures do
  not establish a complete final snapshot if later work stops.

Quality limits: existing intraday row parser is extracted unchanged and reused. It
still skips malformed rows and can map absent volume to zero. A dynamic-only response
symbol/interval gate is new, but only synthetic metadata fixtures have been tested;
no actual retained intraday response was used to establish provider compatibility.
This candidate does not fix all intraday data quality risks or authorize publication.

Offline tests additionally cover actual daily->intraday->final manifest closure with
four total reservations, independent daily/intraday claims, duplicate new run rejection,
retained provider rejection stopping at first symbol, full Nasdaq zero paid calls,
raw symlink/partial-write, pre-HTTP expired guard (reservation retained/zero HTTP),
unknown transport/raw publication and actual selected capture before second request.

## Retained production evidence audit (2026-11-15, offline)
At Store `095f780ea95dbeb8d55fd82089527ded146fe26a`, inspected all 6,814
Twelve session capture JSON files: 119,069 normalized rows, 203 symbols, event
 dates 2026-08-14 through 2026-11-13. None contains original provider `meta`
or `values`. These captures are not original-response fixtures: the existing
parser assigns `symbol` from its argument and hardcodes the 1min source contract,
so neither proves the provider returned matching `meta.symbol/meta.interval`.
All 14 retained `capture-evidence/**/*.raw` responses have `meta.interval=1day`.
They cannot establish intraday response compatibility. The absence of raw
intraday evidence is an explicit acceptance gap, not a reason to weaken the gate,
reconstruct fictional metadata, or issue a fresh request without authorization.
No original parser behavior or production data was changed for this audit.
A separate scan of 119 parseable candidate files outside Store (recovery17/28/30/33,
SOXX admission/basis, XLC candidate/zero-request, bounded and verification artifacts)
also found no original response with `meta.interval=1min` and `values`. This result
is limited to the inspected local retained materials; it does not assert no such
response exists elsewhere. No extra network request was made.

## Official schema contract cross-check (2026-11-15)
Read the provider's public documentation, NOT its market-data endpoint:
https://twelvedata.com/docs/market-data/time-series
The documented response includes string-valued `meta.symbol` and `meta.interval`,
with an AAPL/1min example, plus OHLCV string rows. This supports the exact metadata
gate for our 1min equity contract without spending any provider credits. It is
schema compatibility evidence, not proof of this account's live response behavior.
The timezone guidance at https://support.twelvedata.com/en/articles/5745849-timezones
supports specifying output timezone. Current config explicitly uses America/New_York;
the old parser's ET interpretation remains unchanged, not generalized to arbitrary
configured timezones.

Added three focused doc-derived offline tests (synthetic price values, no live fixture):
successful documented shape; wrong symbol/interval rejection; non-object metadata
rejection. The last case first reproduced four AttributeErrors (null/list/number/string)
incorrectly treated as publication uncertainty. A minimal dict-shape check now classifies
these already-retained payloads as known rejection. It does not relax validation,
continue paid calls, alter row parsing, or claim production activation. This small
increment still requires independent review; previous review covered the earlier code.

Final metadata increment independently reviewed PASS: four tests (three contract cases
plus real REJECTED/STOPPED final closure), 9.431s. No blocking findings. This supersedes
the increment's pending-review note above, not the live-response evidence limitation.
