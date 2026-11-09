# Explicit mature historical tail append

The manual-only mode `daily_history_tail_backfill` separates **execution_date**
(today in America/New_York) from **historical_cutoff** (inclusive historical
provider session). It does not modify the existing 07:00 recovery gate, existing
scheduled jobs, ordinary EOD, admission, rebase, or any pending authorized run.
Source publication is not permission to dispatch this mode.

## When it can run

`execution_date` always owns the current 200/day, 8/minute project ledger and
operational reports. `historical_cutoff` owns data scope. The existing conservative
settlement interval is retained: the cutoff's next **calendar** day at configured
07:00 ET must already have passed. For example, Monday 04:00 ET may append through
the previous Friday, but Monday 06:59 cannot append Sunday's still-immature bar.
Dates use ET calendar boundaries, not a fixed 24-hour UTC duration.

Inputs are exact Compute main SHA, starting private Store SHA, current
`execution_date`, `historical_cutoff`, and a nonempty pinned **fixed_selection**.
`trade_date`, dynamic selection and omitted/default full-universe selection are
rejected for this new mode. The shared `market-data-collector` lock and existing
12-minute job/600-second internal limits remain.

## What is and is not repaired

The strict reader still obtains the full retained history, at most one request
per selected target and the existing 5000-row cap. Every retained date must be
present and canonically identical. Even one changed old field blocks the batch,
including a change within the final three old sessions that explicit rebase
might otherwise allow. This is zero-revision append, not rebase.

Only rows after the old final date and on/before the cutoff are appended. The
cutoff must be a returned provider session. Older returned prefix rows are not
inserted. A returned date missing inside the retained interval blocks rather
than inserting out of order. A cutoff before existing history ends is invalid.
Historical old rows are preserved, as are sealed shard blobs. The active last
shard and index may change to append records and lineage. No rebase archives or
rebase qualification claims are created.

This cannot bypass a known SOXX history conflict by requesting only recent days.
It must fetch and compare all retained dates again; any continuing difference
still blocks. An old rejected candidate is not a reusable qualification token.
It does not solve the root cause of provider numerical revisions.

There is no independent exchange-session calendar in this route. Success proves
the **qualified provider-returned tail** reached the cutoff, not that the provider
and Store could not both omit another session. The report explicitly sets
`exchange_calendar_completeness_established=false`. It does not manufacture
missing sessions, research inputs, stage snapshots, reports or publication receipts
for stock-dairy.

## Accounting and failure

Require enough current-day headroom for every selected target before the first
request. This is not a reservation. Keep all per-request minute/day, source/Store
identity, durable predebit, last-moment clock and deadline checks. Failures after
predebit consume project credits and are not automatically refunded. All-NOOP
still compares full histories, consumes one credit per target and writes a NOOP
diagnostic; it produces no market-fact commit.

Qualify every target before any fact staging. Build only in an isolated worktree,
verify complete histories and exact lineage after all appends, then publish one
no-force fast-forward fact commit. The original budget checkout never receives
staged histories. Inventory, indexes, all declared shards and the diagnostic form
an exact manifest verified by the existing durable-publication verifier.

Late target failure or staging failure publishes no partial market facts. After
any uncertain budget, diagnostic or fact push, stop: no request retry, metadata
follow-up, refund or rollback. Reconcile the exact commit, main, retention ref
and closure first. A new invocation requires its own authorization and fresh pins.

## Validation

Run the full native `python -m pytest tests -v`, including new time, strict append,
workflow, synthetic HTTP and real temporary Git/durability tests. Synthetic tests
are not evidence that a particular production candidate or missing date is valid.
