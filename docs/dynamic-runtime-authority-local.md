# Dynamic runtime authority and final closure (local candidate only)

No network access, real API/provider request, production Store write, runner registration,
commit or publication was performed for this candidate. Existing guarded owner baseline:
60 offline tests and independent review. This increment is not production acceptance.

## Authority
The known local dispatcher at discovery-requests b2b006e70350b9bf0a1099fcfcdcc4d826b1e322
(blob c587fcdf2fcfe124e4a0bb5d7040d91a9f563a50) calls this repository's reusable runtime
at main. It is historical evidence, not an assertion of current remote state.

Runtime reads fixed GitHub API repository, run and workflow endpoints directly. Fixed
anchors are yuatom/stock-market-data, owner 10399225, private Store
 yuatom/stock-market-data-store ID 1335101677. Compute numeric ID is derived from
trusted repo endpoint and independently matched to run.repository/head_repository
and environment, not invented. Caller head is the immutable control commit; reusable
resolved SHA is separately required to equal the checked-out current Compute main.
Only explicit main path spellings @main/@refs/heads/main are equivalent; ref and SHA
remain exact. Missing/ambiguous references, reruns, forks and read failures stop.
The local candidate is owner-only, not permission for arbitrary repository collaborators.

All repositories require expected origin, full history, clean checkout. Control exact
blob/ancestry/first introduction uses the reviewed existing owner logic. No historical
result is required for a new business request. API evidence is never accepted from a
caller file/CLI flag. No OIDC, admin scope, PAT scope expansion, or runtime fallback.

## Runtime and final closure
Workflow uses python -B to avoid creating untracked pycache before clean gates.
Existing shared market-data-collector lock and limits remain. Contents read is retained;
actions:read is explicitly added for run/workflow attestation. Control and Compute use
full checkout. CLI performs already-reviewed per-symbol daily predebit, raw retention,
market closure, then original intraday collector. Original intraday provider/fallback,
accounting and crash gaps are NOT fixed by this increment.

The final writer starts at the latest verified daily head, not the initial Store head.
It allows only this request's result/manifest/ledger, snapshot, changed captures referenced
by that snapshot, matching latest pointer, close-state and rebuilt inventory. Old daily
objects are verified through the manifest but not republished as changed files. Extra
local writes (including post-daily price mutation) fail before push. Final push is single,
verified by existing durability helper. Unknown failure latches; no outer pull/rebase/push,
no diagnostic retry, no automatic refund. Already durable daily commits remain if final
intraday/inventory closure fails. STOPPED becomes nonzero only after successful retention.

## Activation gates still unproven
- Live API referenced_workflows shape/availability and resolved workflow ref evidence.
- Actual token read access with minimal actions:read; caller may cap reusable permissions.
- Actual automatic push actor and triggering_actor: owner-only candidate fails closed
  until real run evidence establishes compatibility, no invented bot allowlist.
- Full-history Actions checkout/ref availability and actual caller context environment.
- No assertion that a synthetic authority fixture is production authorization.

Tests use local Git and synthetic API/transport. Final closure exercises actual capture,
snapshot/latest writer and inventory/manifest verification. A subprocess exercises real
CLI imports with -B and rejects a deliberately wrong Store before reaching any network.

## Confirmed caller permission gap
The locally retained discovery-requests dispatcher grants only contents:read. A
reusable workflow cannot elevate this. Publishing only main runtime is insufficient.
The accompanying dynamic-dispatcher-actions-read.patch targets ONLY the control
branch dispatcher job; do not apply it to the different main collector workflow.
It adds contents:read/actions:read to dispatch-discovery only, preserving standard
dispatch permissions. Not applied to any branch or remote; activation needs this
caller-side update and a real new immutable request commit.

## 2026-11-15 observed authority delta
Coordinator retrieved real run 43516938981 (completed). Its selected public fields,
with original source SHA256 and completed status, are retained in the test fixture.
API lists BOTH the dynamic reusable and the skipped standard reusable. Gate now
requires exactly one dynamic and permits at most one known standard, both same
repository/main ref/resolved Compute SHA; duplicates and all other paths fail.
The completed run is tested as rejected. Only a separately labeled in-progress
copy is simulated as passing; it is not a live runtime attestation. Actual actors
were both yuatom/10399225, so owner-only is evidenced for that historical run,
not all future runs. No network or publication performed by this worker.
