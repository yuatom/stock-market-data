# Hosted evidence and conditional append routes

These manual workflows are inert until separately authorized and dispatched on reviewed main. Opening a pull request or merging source is not permission to request provider data or publish private evidence.

## Shared boundaries

Both routes run in the same repository's `market-data-collector` concurrency group, with cancellation disabled. Neither creates a schedule or uploads raw data to public Actions artifacts. Existing provider and private Store credentials are reused; no credentials are embedded in source.

The private Store evidence adapter writes only root `capture-evidence/<stable-approval-digest>/<symbol>/<closed-name>` objects. This prefix is outside market history and inventory. Literal-byte Git plumbing bypasses attributes and filters. All Git operations disable hooks and fsmonitor per command. Each immutable candidate must contain exactly one added normal file with the exact parent and independently computed blob identity before a push. Remote commit and retention references are read back before acknowledgment. No unknown write is blindly retried.

Deployments must independently verify the approved repository remains private, the current source and Store pins match, the approval is fresh and unused, and the environment has trusted Git transport/configuration. Synthetic local tests establish implementation behavior, not actual production approval or scheduler ownership.

## SOXX evidence-only capture

The dedicated `soxx-capture-only.yml` deployment guard accepts a public reviewed Compute SHA and opaque salted receipt commitment. It permits owner manual dispatch on main and the first attempt only, then stops before private input, checkout or credential access because executing integration is not activated. Private approval commits and digests must never be supplied as public workflow inputs. The complete runtime is tested locally; activating a private input binding requires a separate reviewed change.

The canonical approval object is supplied privately under `capture-approvals/<digest>.json`. Its commit must add only that normal file and have exactly the approved baseline parent. Its scope supplies exact request parameters, accounting day, UTC validity window, repository identity and one-request/one-credit limits. Run identity is excluded from the stable approval digest and added to the create-only execution claim. File presence does not establish user consent; creating the approval and dispatching its digest require explicit authorization.

The sequence is approval verification, claim/request/attempt retention, one official durable budget debit, unchanged immediate provider guard, one response, raw retention, envelope retention and private terminal receipt. No Store commit is permitted between debit and provider guard. All failures stop without refund, automatic rerun or recovery fallback. Malformed responses may have retained raw bytes without a valid envelope. Public output is value-free.

SOXX's explicitly selected XNMS profile is supported without equating it with XNAS. Returned identity must match the exact selected request. Captured bytes, hashes or metadata never establish adjustment-basis equivalence, market qualification or restored history.

## Eleven-symbol conditional append

The separate eleven-symbol workflow keeps its own explicitly approved scope and qualification policy. It shares only the immutable private retention implementation and official collector concurrency. Runtime Store pins and authority inputs remain private; public examples and tests are synthetic.

Capture and append are distinct gates. Raw bytes must be durably retained before parsing. Identity and complete approved overlap comparisons must pass before any allowed append. Old rows and sealed data remain unchanged. A rejected symbol cannot be silently normalized into acceptance, and an uncertain retention/publication outcome stops the operation. Existing SOXX and GLD paths are excluded from this route.

## Activation

Review source and tests first. Prepare exact private authority and current immutable pins only for an explicitly approved one-shot operation. Independently verify freshness, privacy, budget and no earlier uncertain execution immediately before dispatch. Code publication does not activate either route.

## Current deployment guard

No authorized private authority-delivery channel is currently bound. Publishing this source does not create one. The public SOXX workflow fails closed with `PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED` before any private checkout, secret use or provider request. The runtime implementation and tests remain available for an explicitly reviewed private integration. Do not remove this gate merely to accept authority through public inputs or logs.
