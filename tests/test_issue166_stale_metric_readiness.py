import copy
import pytest
from test_deterministic_metric_proof import _seed,store,proofmod


def test_lagging_history_is_not_emitted_as_one_session_metric(tmp_path):
    root,_=_seed(tmp_path)
    for symbol in ('SPY','QQQ'):
        store.append_daily_bars(root,provider='twelve_data_basic',symbol=symbol,asset_class='etf',bars=[{'trade_date':'2026-07-31','close':600.0}],series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported')
    proof,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='open_30m',data_plane_commit_sha='a'*40)
    assert 'AAA' not in {x['symbol'] for x in proof['subjects']}
    assert {'symbol':'AAA','reason':'provider_affine_daily_series_behind_observed_peer'} in proof['missing']
    proofmod.verify_proof_source_blobs(root,proof)


def test_standalone_verifier_rejects_proof_that_hides_stale_peer(tmp_path):
    root,_=_seed(tmp_path); original,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='open_30m',data_plane_commit_sha='a'*40)
    for symbol in ('SPY','QQQ'):
        store.append_daily_bars(root,provider='twelve_data_basic',symbol=symbol,asset_class='etf',bars=[{'trade_date':'2026-07-31','close':600.0}],series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported')
    # A fresh generator proof retains valid fresh source refs; insert AAA's old
    # subject from the independently constructed equal-date baseline.
    current,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='open_30m',data_plane_commit_sha='a'*40)
    stale=next(x for x in original['subjects'] if x['symbol']=='AAA'); forged=copy.deepcopy(current);forged['subjects']=[x for x in forged['subjects'] if x['symbol']!='AAA']+[stale];forged['missing']=[x for x in forged['missing'] if x['symbol']!='AAA']
    with pytest.raises(proofmod.MetricProofError):proofmod.verify_proof_source_blobs(root,forged)
