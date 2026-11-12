# Twelve private route: local, non-activated lock surface

This directory is a design/test artifact only. Its .yml.template is outside .github/workflows. No hosted workflow, dispatch, receipt, authority, budget debit or provider request has been created.

Chosen approach: reuse the existing Eleven wrapper workflow shape with the exact Twelve route. A direct runner or separate per-symbol jobs would bypass private selection or release the transaction lock, so neither is suitable. This is a design choice, not approval to deploy.

One manual-only job holds the existing same-repository market-data-collector concurrency group, cancel-in-progress false, queue max. The first secret-free inline step checks source SHA, owner/actor/triggering actor, main, manual event and first attempt. Only then checkout the exact SHA with credentials disabled; only the final wrapper step sees secret references. Inputs contain only source SHA and opaque receipt commitment. No direct approval JSON, artifact, retry, scheduled trigger or automatic follow-main.

The template alone cannot attest that GitHub actually acquired the lock, nor authorize acquisition. Before installing: finish independent route-chain review and old-route regressions, identify an effective consumer version recognizing Twelve lineage, bind exact new source and Store pins, obtain finite execution approval and verify current maturity/budget. Existing production pin remains untouched. Queueing never extends approval: wrapper/source checks and runtime admission must recheck source/Store/window before claim/HTTP. Unknown outcomes stop, never retry.

Local verification: 13 tests passed (2026-11-12, Python 3.12), including ten rejected inline identity mutations and a successful synthetic identity. These execute only the inline Python guard in a cleared fake environment; no credentials or network. The structural tests check one job, exact shared lock, manual-only trigger, wrapper-only entry, private data separation, exact checkout and no artifact/retry. They are static/synthetic tests, not hosted-lock proof.
