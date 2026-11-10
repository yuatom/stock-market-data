# Local private capture boundary

The opt-in `capture_with_private_retention` function adds a local retention
implementation and request binding to the previously reviewed capture API. No
workflow, CLI, schedule, current recovery handler or default collector calls it.
It is not a production runner and does not grant permission to collect anything.

## Runtime-owned prerequisites

The official caller must authenticate a fresh exact user permission, retain its
independent scope/digest anchor, hold the existing global collector lock and
obtain a confirmed durable one-credit debit. This module does not do those jobs.
Its closed scope is caller-supplied data, not an authorization token. The wrapper
does not add credentials, issue a debit/refund, publish data or retry a failure.

The scope contains exactly:

- `schema_version`: integer 1; `mode`: `capture_only`.
- `request`: the existing closed raw-evidence request, with explicit dp/adjust.
- `destination`: the exact absolute private local directory to create.
- `request_id`: the existing writer's numeric run-attempt identity.
- `compute_sha` and `execution_day`: the expected current source and ET day.
- `valid_from` and `valid_until`: explicit UTC ISO timestamps, lower inclusive and
  upper exclusive. The request end date cannot exceed the execution day.
- `request_limit` and `credit_limit`: integer 1, never booleans.
- `debit_commit_sha`, `budget_blob_sha` and `budget_minute`: the exact confirmed
  `BudgetDebit` values from the existing official debit path.

The wrapper also receives the actual request/destination, writer/debit,
Compute path/pin, execution day, monotonic deadline, key, access config and clock.
It freezes the scope/request, compares their bindings, and checks the validity
window before creating local output. The transport's existing key/access checks
still run before HTTP; a failure there may leave a new private request directory.
An existing or partial destination is never reused automatically.

Immediately before the single request it checks time, writes and synchronizes a
create-only attempt marker, calls the unchanged
`admit_market_data_baseline.guard_provider_request`, and rechecks scope expiry.
The effective deadline is no later than the scope expiry. The guard's durable
budget/source/current-main/ET-day/minute checks are not weakened. A rejected guard
leaves the attempt conservatively consumed locally. No credit is refunded.

## Local storage guarantees and limits

`PrivateCaptureRetention` creates a missing destination beneath an existing
owner-only directory. It rejects overlap with Compute/Store, symlink traversal,
an existing destination, non-private paths, non-regular files and hardlinks.
Noncanonical double-slash POSIX roots are rejected rather than compared as a
different lexical anchor from the same underlying Linux directory.
Directories are created with mode 0700 and files with mode 0600. It uses fixed
filenames, exclusive create, file and directory fsync, bounded read-back and exact
byte checks before acknowledgment. It reopens the original path and checks inode
identity so a renamed/replaced destination stops instead of following it.

The file sequence is `request.json`, `attempt.json`, `response.raw`,
`envelope.json`, then `completion.json`. Raw bytes are retained before parsing.
The envelope must reproduce the same raw bytes and exact request. A malformed
accepted-transport body remains available as raw evidence, without a completion
receipt. Rejected HTTP/header bodies and oversized prefixes remain outside the
original transport retention contract.

No overwrite, resume, deletion, refund or fallback occurs. A filesystem failure
may leave partial files. Unknown outcomes require inspection, not retrying the
same or a different directory automatically. A completion file on disk alone is
not a successful callback acknowledgment: a crash may occur before the final
directory sync/read-back. The runtime must retain the returned independent hashes
and any failure state, rather than interpreting file presence as success.
Terminal close errors are value-free and never retried. If capture has already
failed, cleanup must not replace that original failure. An unknown close outcome
can leave an unusable descriptor until process exit; it cannot turn into success.

Successful return reports local fsync/read-back only. Hosted durability, owner
authorization, global-lock authentication, provider truth, rounding/volume
semantics, market qualification and production recovery remain unproved/false.
This is not WORM storage or defense against a hostile same-user process, root,
filesystem, backup service or machine loss. No hosted private destination is
configured, and no raw payload is sent to GitHub or a public artifact store.

## Validation

Filesystem tests use real temporary files, permissions, hardlinks, competing
creators and failure injection. Preflight tests use the existing synthetic
Compute/Store Git fixture and actual budget/source guard, replacing only HTTP.
They prove local behavior without actual market data, credentials, provider calls
or production writes. The full repository suite remains required. Python network
blocking and file-only Git are test precautions, not OS sandbox attestation.
