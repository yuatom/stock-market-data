# Private authority selection channel (unbound)

This local integration design does not activate either workflow. Both public workflow deployment gates remain unchanged. No credential, permission, branch protection, or secret configuration is created by this design.

## Existing transport, separate authority

The existing collector schema already names the private Store repository and `MARKET_DATA_STORE_TOKEN`. A future reviewed binding can reuse that credential to read a private Git reference. A token's existence and repository write access are not user consent to capture or append data.

Public dispatch supplies the exact reviewed Compute SHA and an opaque salted private-receipt commitment. The route comes from the specific workflow, not user-controlled private JSON. The deterministic private reference is `refs/heads/capture-authority/<route>/<compute-sha>`. Route names are closed constants. No private commit, contract hash, approval JSON, or Store pin belongs in public inputs, outputs, summaries, artifacts, command echoes, or error messages.

## Immutable descriptor

The reference points to a commit with exactly one parent: the exact Store main authority head expected by the selected runtime. Its sole change is adding a normal-file `capture-authority/selection.json`. The descriptor is canonical, bounded JSON with a closed schema, binding route, reviewed Compute SHA, Store authority head, and exact approval digest. SOXX points to its existing approval object. Eleven-symbol append carries the canonical approval object privately and points to its existing contract head. The existing runtime remains responsible for all exact scope, freshness, identity, accounting and append gates.

The descriptor is on an isolated side reference, never added atop Store main. This preserves each runtime's existing single-parent, single-addition authority shape. The reference must be created once only under exact authorization, with its commit independently read back. An occupied reference must not be moved or deleted to retry. Its creation receipt and exact descriptor identity are private approval evidence, not public output.

The reader checks literal blob identity, mode, size, canonical bytes, closed fields, exact parent, one added path, reference binding, and unchanged runtime head. Git replacement and graft views are rejected. Commands disable hooks and fsmonitor. The reader does not fetch, modify refs, checkout, publish, or call the provider. A valid selection is data, not an authorization decision.

## Future hosted binding requirements

Before replacing a deployment gate, review a wrapper that:

1. Verifies owner manual-main first-attempt identity and exact reviewed source under the existing same-repository collector concurrency lock.
2. Confirms the existing Store destination is private using the existing credential without redirecting Authorization headers.
3. Retrieves only the deterministic private reference and required Store objects through a quiet, trusted transport. It does not use checkout actions whose output may expose private pins. It captures all subprocess output privately and emits only constant status codes.
4. Cross-checks the observed reference commit against the private creation/authorization receipt, then invokes the strict local reader. It does not infer consent from an arbitrary reference or file.
5. Keeps the runtime checkout on the exact approved main authority head, not the descriptor. Immediately rechecks remote main and authority reference, then hands private values in memory to the existing coordinator. It does not put them on a public command line or step output.
6. Preserves create-only claims and the existing durable debit/provider guard sequence. Any changed head, consumed claim, expired approval, uncertain publication, or unknown request outcome stops without a retry or automatic repair.

A fresh invocation that sees an existing claim is not a new permission. A different descriptor or request ID must never revive a spent grant. Private retention and main advancement remain the runtime's replay barrier.

## Exact permissions still required

Local code and synthetic verification require no production action. Actual activation requires approval of the reviewed binding/source change and its merge; approval to place the exact canonical runtime authority and descriptor in the named private Store and create the exact isolated reference; and approval of the specific one-shot execution scope, including provider request/credit limits, validity window, retention destination and whether market facts may change. These can be presented as one concrete operation after current private pins are verified. No new secret or broader account access is proposed.

No private production content or credentials were read to implement this design. Public configuration establishes that a credential name exists in the workflow contract, not that it is currently configured or has sufficient permissions. The public workflows remain deployment-gated until the complete executing binding is independently reviewed and specifically authorized.

## Quiet metadata transport seam

A separate read-only HTTPS resolver can use the existing Store credential to retrieve repository visibility and the deterministic authority reference without printing either. Its endpoint host, repository and reference grammar are fixed. Redirects, oversized or malformed JSON, a public or mismatched repository, a tag instead of a commit, or a reference differing from an independently approved descriptor commit must reject with a constant error. The token is attached only to the exact GitHub API requests, never query strings or diagnostics.

The trusted descriptor pin is supplied privately by the caller from the exact user-approved creation receipt. It is not learned by accepting whatever the reference currently points to. A metadata result is not an execution grant, does not materialize Git objects, and does not replace the immutable local reader. No standalone transport endpoint or new secret is needed; the remaining hosted integration must keep the receipt in the trusted private caller context and prove quiet object materialization before activation.

The local resolver is implemented in `scripts/private_authority_transport.py` and remains unbound. It uses standard-library HTTPS with normal certificate verification, no ambient proxy, no redirects and no retries. It reads at most 65537 bytes per response, rejects duplicate JSON keys and non-finite constants, requires a private exact-name repository, and accepts only the exact commit from the caller's independently approved receipt. Result representations and propagated errors are value-free. Forty synthetic tests include real opener redirect processing without network access. No live credential or production request was used to verify it.

This implementation does not deliver or authenticate the receipt, fetch Git objects, bind workflow identity/concurrency, or invoke either runtime. Those are still required before activation. In particular, loading the expected pin from the same mutable authority reference would defeat the independent-receipt requirement and is not a supported integration.

## Quiet object materialization and identity preflight (local, unbound)

`scripts/private_authority_materialize.py` now implements fresh-directory Store acquisition. It verifies private metadata first, fetches only main and the deterministic authority ref from the fixed HTTPS origin without redirects, submodules, helpers or hooks, verifies exact fetched pins, checks out the approved main head, runs the strict local selector, and rechecks remote main/ref and metadata before returning. It captures all Git output and returns a constant error on failure without retrying or deleting/reusing the failed directory. The new directory is created with mode 0700. Git receives only a small environment allowlist; its temporary Authorization header is confined to authenticated child environments and never persisted in repository config or passed in argv. The transport does not promise bounded repository history size; it has a per-command timeout. Production storage sizing remains part of hosted integration review.

`scripts/private_authority_preflight.py` rejects unsupported platform identities before private access, including non-owner, non-main, repeat-attempt, wrong-source or wrong-workflow invocations. This is a local preflight wrapper, not a platform attestation or executing workflow. It does not authenticate a receipt, establish a concurrency lock, verify source checkout/remote main, or invoke a coordinator. Those duties remain with the reviewed hosted caller. The current public workflow gates remain untouched.

## Receipt binding and deployment boundary

An opaque salted receipt commitment can be supplied independently through an owner manual dispatch, while exact receipt pins and nonce stay private. See `private-authority-receipt-candidate.md` for the canonical receipt, exact public disclosure and threat model. A create-once ref convention alone is not immutable authority; the independent commitment binds receipt bytes even if a sidecar pointer changes. Missing or substituted data fails closed.

Both workflow interfaces now accept the opaque commitment, but their deployment gates still stop before private access, checkout, credentials or provider calls. Source review and deployment authorization remain separate. This interface does not authorize any private receipt creation, collection or append.


The quiet sidecar reader is implemented in `scripts/private_authority_receipt_transport.py`. It validates private repository metadata, the fixed deterministic receipt ref and path, bounded JSON/base64, returned path/type/size and literal Git blob SHA, then verifies the independently supplied receipt commitment and route/source context. It returns canonical private bytes without writing or logging. Explicit symlink/submodule indicators reject, but Contents API metadata alone does not prove literal Git mode; trusted creation/readback must establish any required sidecar normal-file invariant. Exact descriptor/tree mode remains independently checked by the strict selector.

`prepare_hosted_authority` composes platform identity validation before any sidecar read, receipt commitment verification, quiet materialization and final authority checks. It returns only a private selection, not an execution grant. No workflow currently invokes this pipeline; the deployment gates still prevent all private access. The existing runtime's source, locking, approval-window, budget, claim and provider guards must be connected in a separately reviewed executing binding.
