# Daily baseline revision admission

Owner: `config/data-plane.yaml`; executable mode contract:
`config/daily-baseline-admission.yaml`. Task: `yuatom/stock-dairy#166`.

This is a manual branch of the existing Market Data Collector workflow. It does
not run the ordinary collector or rebaser and never writes market facts. It
uses the existing Actions secrets, private Store, provider-affine parser,
last-three/max-three revision rule, and shared `market-data-collector` queue.
Neither PR creation/merge nor a normal schedule starts this mode.

## Manual operation after reviewed source is available

Choose the existing workflow, `main`, mode `daily_baseline_admission`. Supply:

- `expected_compute_sha`: the complete current Compute main SHA;
- `expected_store_sha`: the complete current private Store main SHA;
- `trade_date`: current America/New_York date, after the contract's 07:00 ET
  previous-session maturity boundary. No historical backdating or automatic wait.

These fields are identities, not secrets. Never put provider keys in workflow
inputs. The key is provided only through the existing Actions secret. The job
validates the event SHA before checkout and the actual clean checkouts before
work. Queue-time Store drift stops the invocation; obtain a fresh target rather
than reusing old evidence. This mode does not claim to fix invalid credentials.

## Read-only boundary and budget accounting

Read-only means **no market-fact mutation**, not no operational writes. The only
permitted paths are the current day's Twelve Data budget and the run/attempt's
private diagnostic JSON. Every push checks the complete Store change set,
including deletions and ignored/untracked paths, against those two exact paths.
Symlinks and existing report identity reuse fail closed. Series, captures,
snapshots, recovery archives, inventory and source never change.

All existing series are read and qualified before spending any credits. Before
each bounded single-symbol provider request, the existing budget helper debits
one credit. That budget-only commit must be pushed fast-forward to private
Store main and pass exact remote/durable-reference readback **before** HTTP.
No pull/rebase/force retry is used. An unexpected concurrent writer stops work.
The debit's UTC minute must still be current immediately before provider access.
Expired/failed/unused debits are retained conservatively, not refunded. At most
one provider request is attempted per symbol; malformed rows are not silently
skipped and API retries are not hidden.

This costs one small metadata commit and durability verification per request,
plus initial/final reports. It deliberately prefers auditability and bounded
wasted quota over an unpersisted local ledger. A timeout after predebit cannot
silently restore that credit; interruption before final report leaves
`IN_PROGRESS`, never a terminal PASS. If publication acknowledgement is lost,
stop and reconcile the actual remote commit; do not rerun the request blindly.

Serialization coordinates **this repository's cooperating workflows**, not all
possible users of a provider account. The operator must ensure other uses of the
same account are included or excluded during the operation. An absent day ledger
may initialize at zero only in this coordinated fresh context, not in an
unrelated snapshot. The existing 200/day and 8/minute limits are not raised.

## Results and evidence

Public output contains fixed status/count/reason fields and exact Compute,
starting Store, final Store and diagnostic blob identities. Raw payloads,
credential values, exception details and field-level market values are never
printed or uploaded as public artifacts. Detailed admitted/rejected overlap
changes and hashes remain in the private diagnostic file under `collector-state`.

A PASS means all selected symbols satisfied the **existing bounded replacement
admission** on that invocation's inputs. It does not prove exchange-calendar
completeness for rows absent from the provider response, that every latest date
advanced, or that data was repaired. Inspect per-symbol `new_records`,
`latest_date_advanced`, `last_trade_date`, and coverage/revision evidence.
A FAIL, BLOCKED or missing final report is not successful admission. CLI exit is
0 only for terminal PASS with confirmed publication; otherwise nonzero.

Return safe run/job/command/exit and exact-result refs to Issue166. Detailed
private evidence must not be copied into public PR comments. A later rebase must
freshly verify source, retained history and provider replacement again; this
report is not a stored authorization to write different inputs later.

## Validation and remaining boundaries

Tests use synthetic data and temporary Git remotes. Required candidate validation
is the existing full repository suite `python -m pytest tests -v` on the exact PR
HEAD. Native manual credentials/provider/budget/runtime execution are a separate
post-merge acceptance, never implied by CI. PR44 remains merged; its two P2 issues
(non-ok rebase exit semantics and nontransactional historical replacement)
remain open for real rebase and are not repaired by this read-only mode.

## Request-boundary and required-output proof

The predebit proof binds the actual minute increment in the helper-written
ledger, its blob and the durably published Store commit. After URL/config
preparation, the strict replacement reader invokes the admission-only callback
before its HTTP call. The callback checks the current clean Store/debit object
and current clean Compute/source identity; it checks the clock last, after
potentially slow Git reads. Rejection leaves the debit charged and does not
increment `requests_attempted`. Store drift or a dirty ledger also forbids a
final report push. These checks are not an atomic distributed lock against
unrelated writers or provider-account consumers.

The budget JSON also carries the retained publication contract's manifest
fields (`schema_version`, `request_id`, `trade_date`, `stage`, `required_outputs`).
It declares the invocation diagnostic's path and blob without adding a third
Store path. Initial/final diagnostic and declaration are committed together;
credit and minute counters are preserved. Per-request budget commits preserve
that declaration. The existing real verifier receives `--required-manifest`
for diagnostic publication, and its receipt must contain exactly that output.
The producer also reads the same verified immutable commit to check the
manifest and diagnostic request/date/stage identities. Empty closure, an
incorrect path/blob/identity, malformed receipt or failed verification is not
terminal success, even when a push or local `rev-parse` succeeded. A remotely
written diagnostic alone is not a successful producer receipt.

## Explicit existing dynamic histories

The guarded admission and recovery jobs accept an optional `dynamic_selection`
JSON object. Omission keeps the fixed universe. An explicitly supplied value
must contain `request_commit_sha`, `request_blob_sha`, `result_blob_sha`, and
`targets`, each target containing only `symbol` and `index_blob_sha`. The
request commit must be an ancestor of the fetched `discovery-requests` branch.
The request commit must be the first introduction of that request ID on the
branch; later edits cannot replace its original bytes. The request and exact
pinned Store result must agree on identity and candidate
list; each selected existing stock series must have its own recorded history
conflict and matching index, shard, provider and security semantics. The index and each shard
blob must also match the result publication commit, so later series changes
invalidate that evidence. The Store
SHA is the ordinary starting pin, not a selector-provided alternative. An empty,
invalid or stale selection stops before debit/provider access; it never falls
back to the fixed universe. No collection or personal membership changes.

This is maintenance capability, not a grant to run recovery. Admission never
writes market facts. A separate authorized recovery still fetches and qualifies
all selected replacements, keeps all retained dates and the existing tail
revision limits, and publishes one complete archived candidate with the same
manifest and durability checks. Unconfirmed publication is never retried.
