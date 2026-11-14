# Daily per-symbol volume conflict isolation

This change does not enable the scheduled `overlap_revision_policy`. Deployment
and the production policy switch require separate authorization. The existing
bounded manual recovery policy is unchanged.

With the scheduled price-only classifier explicitly enabled:

- Equivalent overlap, including numeric representation differences, retains
  ordinary daily lineage and the existing volume semantics.
- A confirmed numeric volume difference quarantines only that symbol.
  A simultaneous price/metadata change still rejects price append.
  No market row, price, volume value, sealed shard or existing lineage is rewritten.
- `volume_conflict_quarantine_v1` is a policy annotation in `lineage_segments`,
  not an ingestion or a new price interval. Its overlapping 0..last bounds state
  the full-series volume scope. It binds the retained normalized response by path
  and SHA256. The current consumer already honors `BLOCKED_UNRESOLVED` annotations.
- The policy annotation is persisted even without a new price date, or when
  another overlap date has a price conflict and price append is rejected.
- An existing blocked marker is never released; repeated quarantine is a no-op.
- Invalid prices and metadata revisions still reject that symbol. Independent
  symbols retain the collector's existing continue-after-symbol-failure behavior.

The result reports `volume_blocked_symbols` and `volume_quarantined_series`,
not a misleading global volume ban. `changed_series` still counts price appends;
metadata-only quarantine is counted separately. Evidence is diagnostic, not
permission to activate a provider basis or overwrite historical prices.

Rollback removes the scheduled opt-in policy, not persisted quarantine evidence
or the consumer's volume fence. It cannot requalify previously conflicted volume.
