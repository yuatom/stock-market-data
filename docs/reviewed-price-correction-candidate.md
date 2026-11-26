# Reviewed historical daily price correction candidate

This is a local preparation and verification primitive for an already
approved field-level correction, not a scheduled or production writer. The
operator must authenticate the approval and both provider receipts out of band
before calling it. A Git hash or a provider label is not proof that a provider
made the observation. The callback is required, receives the exact immutable
scope (including both immutable Git evidence locators, before-image SHAs and
canonical field approvals), and must return exactly true. Do not expose it
through a caller-controlled input, CLI flag, or environment variable.

Only existing OHLC values on up to three recent rows in the active regular
daily shard of a legacy USD series may change. Confirm each field at cent
precision in original retained and independent normalized evidence; keep
the retained number and old values of every other field. The whole retained
overlap is compared, including unaffected sealed shards. No date is added,
no volume is changed, and explicitly versioned corporate-action series use a
different route. This does not change ordinary EOD behavior.

`prepare_candidate` uses an isolated Git index to create a single-parent
candidate, leaving all refs and the working tree untouched. It archives the
exact prior series index and active shard bytes, adds the bound audit,
materializes an exact-output manifest and independently re-derives all output
bytes. The prior snapshot's immutable evidence must remain available; scopes
and approvals stay in the private owner surface.

`install_local_candidate` independently revalidates the candidate, then
optionally installs it in an exact, pre-created local review ref using atomic
compare-and-swap against the original Store base. It cannot write to a remote,
update a branch, or modify the production ref. An existing different value or
absent review ref fails without overwriting it.

A future production writer must separately bind the authenticated deployment approval,
current production ref and verified candidate; do not publish this candidate
using special-case operators, cleartext provider labels, or direct file
uploads. Publication requires exact CAS semantics at the original base,
manifest and durable Store readback, and runtime consumer verification.
