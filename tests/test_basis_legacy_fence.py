import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from scripts import market_data_store as store
import build_deterministic_metric_proof as proof
import market_data_collectors as collector


def marker(root, raw=b'{bad'):
    path=root/'series-active/daily/twelve_data_basic/AAA.json'
    path.parent.mkdir(parents=True);path.write_bytes(raw)


def seed(root):
    return store.append_daily_bars(root,provider='twelve_data_basic',symbol='AAA',asset_class='etf',
        bars=[dict(trade_date='2026-01-01',open=30,high=30,low=30,close=30,volume=100)],
        series_semantics='daily_regular_ohlcv')


def test_legacy_defaults_unchanged_but_catalog_fences_read_and_write(tmp_path):
    seed(tmp_path);assert len(store.read_daily_series(tmp_path,provider='twelve_data_basic',symbol='AAA'))==1
    old={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    marker(tmp_path)
    with pytest.raises(store.MarketDataError,match='explicit_versioned_basis_required'):
        store.read_daily_series(tmp_path,provider='twelve_data_basic',symbol='AAA')
    with pytest.raises(store.MarketDataError,match='explicit_versioned_basis_required'):seed(tmp_path)
    assert all((tmp_path/p).read_bytes()==raw for p,raw in old.items())


def test_legacy_proof_rejects_active_basis_before_loading_values(tmp_path):
    seed(tmp_path);marker(tmp_path)
    with pytest.raises(proof.MetricProofError,match='explicit_versioned_basis_required'):
        proof._load_daily_series(tmp_path,'AAA','2026-01-02')


def test_collector_fences_before_budget_and_provider(tmp_path):
    marker(tmp_path)
    config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                         'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':256}}}
    with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'synthetic-test-key'}), \
         patch.object(collector,'_daily_universe',return_value=[('AAA','etf')]), \
         patch.object(collector,'wait_for_twelve_budget') as wait, \
         patch.object(collector,'consume_twelve_credit') as debit, \
         patch.object(collector,'fetch_twelve_daily') as fetch:
        result=collector.collect_previous_session_eod(trade_date='2026-01-07',store_root=tmp_path,
            watchlist={},completeness={},config=config,access={})
        assert result['changed_series']==0 and len(result['failures'])==1
        wait.assert_not_called();debit.assert_not_called();fetch.assert_not_called()
