"""Pure read-only v2 price proof adapter. No fetch, persistence or activation.

Execute only from an independently reviewed immutable Compute source pin. The
caller owns calendar verification, private receipt admission and deployment.
The containing Store commit cannot be embedded in its own proof. Every source
blob and selector must instead be reverified at the final Frozen Store boundary.
"""
from scripts import provider_basis_snapshot as basis
from scripts.build_deterministic_metric_proof import compute_metrics


def build_price_basis_proof(repo, selector, authority, *, required_sessions,
                            knowledge_cutoff, compute_sha, scope):
    basis._hex(compute_sha)
    basis._need(scope in {'prior_session_core_context', 'current_target'})
    view = basis.read_price_view(repo, selector, authority, fields=('close',),
        required_sessions=required_sessions, knowledge_cutoff=knowledge_cutoff)
    rows = [{'trade_date':r['trade_date'], 'close':float(r['close'])} for r in view['records']]
    basis._need(len(rows) >= 2)
    result = compute_metrics(rows[:-1], target_trade_date=rows[-1]['trade_date'],
                             target_close=rows[-1]['close'])
    result['volume'] = {'status':'blocked','reason':'BLOCKED_UNRESOLVED'}
    return dict(schema_version=1, kind='price_basis_metric_proof',
        formula_contract='stock_dairy_calculation_policy_v2', compute_sha=compute_sha,
        basis_id=selector['basis_id'], scope=scope,
        required_sessions=[r['trade_date'] for r in rows], volume_policy='BLOCKED_UNRESOLVED',
        selector={k:v for k,v in selector.items() if k != 'read_sha'}, result=result)
