"""Offline protocol acceptance candidate. Not wired to runtime or authority provisioning.

Expectations, workflow approval, dispatch times and stored receipt identity must
come from independent locked authority/immutable authenticated readback, never
from the jobs/logs being checked. A runner label alone is not source authority.
"""
from datetime import timedelta
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_delivery import DeliveryExpectation, WORKFLOW, _verify_private_delivery


def verify_hosted_pair(*, profile, repository, ready, final, anchored_workflow_blob,
                       source_readback, ready_dispatched_at, provider_gate_at,
                       final_dispatched_at, now):
    B._closed(profile, ('kind', 'workflow_blob', 'expires'))
    B._need(profile['kind'] == 'private_github_hosted_v1')
    blob = B._hex(anchored_workflow_blob)
    B._need(profile['workflow_blob'] == blob)
    expires = B._stamp(profile['expires'])
    before, debit, after, instant = map(B._stamp, (ready_dispatched_at, provider_gate_at,
                                                   final_dispatched_at, now))
    B._need(before <= debit <= after <= instant < expires
            and debit + timedelta(seconds=120) < expires <= before + timedelta(minutes=10))
    pre, post = ready[0], final[0]
    B._need(type(pre) is type(post) is DeliveryExpectation
            and pre.workflow_id == post.workflow_id == 415302427
            and all(getattr(pre, k) == getattr(post, k) for k in
                    ('repository_id', 'owner_id', 'workflow_source_sha', 'compute_sha'))
            and pre.run_id != post.run_id and pre.receipt_commitment != post.receipt_commitment)
    actual_blob, actual_bytes = source_readback(B._hex(pre.workflow_source_sha), WORKFLOW)
    B._need(actual_blob == blob and B.blob_sha(actual_bytes) == blob)
    proofs = []
    job_ids = set()
    for phase, lower, upper in ((ready, before, debit), (final, after, instant)):
        expected, run, jobs, log, identity = phase
        proof = _verify_private_delivery(expected, repository, run, jobs, log,
            now=instant.isoformat(), calendar_authority={'finite_hosted_verifier_candidate': True})
        job = jobs['jobs'][0]
        B._need(job['labels'] == ['ubuntu-latest'] and job['id'] not in job_ids)
        B._need(lower.replace(microsecond=0) <= B._stamp(run['created_at'])
            <= B._stamp(run['run_started_at']) <= B._stamp(job['completed_at']) <= upper)
        B._need((proof['receipt_commit'], proof['receipt_blob_sha'], proof['selector']) == identity
                and expires <= B._stamp(proof['valid_until']))
        job_ids.add(job['id'])
        proofs.append(proof)
    B._need(proofs[0]['receipt_commit'] != proofs[1]['receipt_commit']
            and proofs[0]['receipt_blob_sha'] != proofs[1]['receipt_blob_sha'])
    return proofs[1]
