"""Selected versioned symbols must stop before wait, debit or provider I/O."""
import json
from pathlib import Path
import pytest
import scripts.collect_dynamic_candidates as dynamic
import scripts.initialize_market_data_baseline as initialize


@pytest.mark.parametrize('mode',['catalog','malformed','symlink'])
def test_dynamic_selected_basis_never_waits_debits_or_fetches(tmp_path,monkeypatch,mode):
    root=tmp_path/'data';p=root/'series-active/daily/twelve_data_basic/AAA.json';p.parent.mkdir(parents=True)
    if mode=='symlink':p.symlink_to(tmp_path/'absent')
    else:p.write_text('{}' if mode=='catalog' else 'broken')
    monkeypatch.setenv('TWELVE_DATA_API_KEY','synthetic-unused')
    called=[]
    for name in ['wait_for_twelve_budget','consume_twelve_credit','fetch_twelve_daily']:
        monkeypatch.setattr(dynamic.collectors,name,lambda *a,_name=name,**k:called.append(_name) or [])
    available,failed=dynamic._ensure_daily_history(symbols=['AAA'],trade_date='2026-01-07',store_root=root,config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':800,'hard_credits_per_minute':8}}}},access={},target_records=50)
    assert called==[]
    assert available==[] and failed and 'basis' in failed[0]['reason'].lower()


def test_initializer_selected_basis_never_waits_debits_or_fetches(tmp_path,monkeypatch):
    root=tmp_path/'data';p=root/'series-active/daily/twelve_data_basic/AAA.json';p.parent.mkdir(parents=True);p.write_text('{}')
    monkeypatch.setenv('TWELVE_DATA_API_KEY','synthetic-unused')
    monkeypatch.setattr(initialize.collection,'load_collection_universe',lambda p:{})
    monkeypatch.setattr(initialize.collection,'daily_universe',lambda c:[('AAA','ETF')])
    config={'series':{'analysis_windows_may_read_last_sessions':[50]},'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':800,'hard_credits_per_minute':8}}}}
    monkeypatch.setattr(initialize.collectors,'load_yaml',lambda p:config)
    called=[]
    for name in ['wait_for_twelve_budget','consume_twelve_credit','fetch_twelve_daily']:
        monkeypatch.setattr(initialize.collectors,name,lambda *a,_name=name,**k:called.append(_name) or [])
    result=initialize.initialize(store_root=root,trade_date='2026-01-07',universe_path=tmp_path/'u.json',store_config_path=tmp_path/'config.yaml',access_path=tmp_path/'access.yaml')
    assert called==[]
    assert result['failures'] and result['failures'][0]['detail']=='explicit_versioned_basis_required'
