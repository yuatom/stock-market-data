"""Synthetic, local-only contract/candidate acceptance, never provider execution."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest
from scripts import eleven_symbol_append as old
from scripts import market_data_store as store
from scripts.simulate_eleven_symbol_append import fixture_inputs

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ('GLD',) + old.SYMBOLS

def module():
    spec = importlib.util.find_spec('scripts.twelve_symbol_append')
    assert spec is not None, 'closed twelve-symbol adapter is missing'
    from scripts import twelve_symbol_append
    return twelve_symbol_append

@pytest.fixture
def data(tmp_path):
    base, profiles, responses, pins = fixture_inputs()
    rows = old.read_series(base, 'IBIT')
    store.append_daily_bars(tmp_path/old.PREFIX,provider='twelve_data_basic',symbol='GLD',
        asset_class='etf',series_semantics='daily_regular_ohlcv',bars=rows,
        lineage={'ingest_kind':'synthetic_fixture'})
    prefix=old.PREFIX+'series/daily/twelve_data_basic/GLD/'
    index_path=tmp_path/prefix/'_index.json'
    index=json.loads(index_path.read_bytes())
    index['baseline_initialization']={'status':'accepted','synthetic_only':True}
    index_path.write_bytes(old.encode(index))
    for path in tmp_path.rglob('*.json'):
        base[str(path.relative_to(tmp_path))]=path.read_bytes()
    pins['GLD']={name:store.git_blob_sha_bytes(base[prefix+name]) for name in old.FILENAMES}
    profiles['GLD']=copy.deepcopy(profiles['IBIT'])
    profiles['GLD']['request']['symbol']='GLD'
    response=json.loads(responses['IBIT'])
    response['meta']['symbol']='GLD'; response['values'][0]['volume']='9999'
    responses['GLD']=old.encode(response)
    scopes=[dict(symbol=s,old_count=261 if s=='XLK' else 260,first_date=old.START,
        last_date=old.DATES[0] if s=='XLK' else '2024-08-30',
        addition_dates=list(old.DATES[1:] if s=='XLK' else old.DATES),files=pins[s]) for s in SYMBOLS]
    contract=dict(schema_version=1,kind='twelve_symbol_append_contract',baseline_store_sha='a'*40,
        historical_cutoff=old.DATES[-1],targets=scopes,request_limit=12,credit_limit=12,per_symbol_request_limit=1)
    return base,profiles,responses,pins,contract

def test_closed_contract_and_no_production_claim(data):
    a=module()
    contract=data[-1]
    assert a.validate_contract(old.encode(contract),old.sha(old.encode(contract)))==contract
    assert a.SYMBOLS==SYMBOLS and a.PRODUCTION_READY is False

@pytest.mark.parametrize('mutation',['eleven','extra','duplicate','order','kind','budget','per_symbol','boolean','cutoff','digest'])
def test_contract_rejects_scope_or_limits_drift(data,mutation):
    a=module(); c=copy.deepcopy(data[-1])
    if mutation=='eleven':c['targets'].pop(0)
    elif mutation=='extra':c['targets'].append(copy.deepcopy(c['targets'][0]))
    elif mutation=='duplicate':c['targets'][1]=copy.deepcopy(c['targets'][0])
    elif mutation=='order':c['targets'].reverse()
    elif mutation=='kind':c['kind']='eleven_symbol_append_contract'
    elif mutation=='budget':c['request_limit']=13
    elif mutation=='per_symbol':c['per_symbol_request_limit']=2
    elif mutation=='boolean':c['per_symbol_request_limit']=True
    elif mutation=='cutoff':c['historical_cutoff']='2024-09-09'
    raw=old.encode(c);digest='0'*64 if mutation=='digest' else old.sha(raw)
    with pytest.raises(old.RunStopped):a.validate_contract(raw,digest)

def test_gld_candidate_preserves_full_old_rows_and_records_volume_conflict(data):
    a=module();base,profiles,responses,pins,c=data; before=copy.deepcopy(base)
    scope=c['targets'][0]
    changes,conflicts=a.prepare_candidate(base,'GLD',responses['GLD'],profiles['GLD']['request'],
        {'ingest_kind':a.INGEST_KIND},pins['GLD'],scope)
    assert base==before
    after=dict(base,**changes)
    assert a.read_series(after,'GLD')[:260]==a.read_series(base,'GLD')
    assert len(a.read_series(after,'GLD'))==265
    assert conflicts==[a.read_series(base,'GLD')[0]['trade_date']]
    assert len(changes)==2
    with pytest.raises(old.RunStopped):old.series_prefix('GLD')

@pytest.mark.parametrize('field',['open','high','low','close'])
def test_gld_any_overlap_price_change_rejects(data,field):
    a=module();base,profiles,responses,pins,c=data
    raw=json.loads(responses['GLD']);raw['values'][0][field]=str(float(raw['values'][0][field])+0.01)
    with pytest.raises(old.ValidationRejected):
        a.prepare_candidate(base,'GLD',old.encode(raw),profiles['GLD']['request'],
            {'ingest_kind':a.INGEST_KIND},pins['GLD'],c['targets'][0])

def test_wrong_lineage_or_unapproved_symbol_stops(data):
    a=module();base,profiles,responses,pins,c=data
    with pytest.raises(old.RunStopped):
        a.prepare_candidate(base,'GLD',responses['GLD'],profiles['GLD']['request'],
            {'ingest_kind':'eleven_symbol_conditional_append'},pins['GLD'],c['targets'][0])
    with pytest.raises(old.RunStopped):a.read_series(base,'SOXX')

@pytest.mark.parametrize('key,value',[('credit_limit',13),('credit_limit',True),('request_limit',12.0),
    ('extra_key',False),('baseline_store_sha','bad'),('schema_version',True)])
def test_contract_other_exact_fields(data,key,value):
    a=module();c=dict(data[-1],**{key:value});raw=old.encode(c)
    with pytest.raises(old.RunStopped):a.validate_contract(raw,old.sha(raw))

def test_plan_is_exact_once_per_symbol_and_shared_budget_not_a_debit(data):
    a=module();c=data[-1];raw=old.encode(c)
    budget=old.encode({'execution_day':'2026-11-11','credits':180,'minutes':{'2026-11-11T12:00Z':8}})
    before=bytes(budget)
    assert hasattr(a,'synthetic_execution_plan'), 'closed twelve execution plan is missing'
    plan=a.synthetic_execution_plan(raw,old.sha(raw),budget,'2026-11-11',
        ['2026-11-11T13:00Z']*8+['2026-11-11T13:01Z']*4)
    assert tuple(x['symbol'] for x in plan['steps'])==SYMBOLS
    assert all(x['attempt']==1 for x in plan['steps'])
    assert plan['projected_credits']==192 and plan['production_ready'] is False
    assert plan['required_lock_group']=='market-data-collector' and budget==before

@pytest.mark.parametrize('credits,minutes',[(189,['2026-11-11T13:00Z']*8+['2026-11-11T13:01Z']*4),
    (0,['2026-11-11T13:00Z']*12),(0,['2026-11-11T13:00Z']*11),
    (0,['2026-11-11T13:01Z']*8+['2026-11-11T13:00Z']*4)])
def test_plan_rejects_budget_or_schedule_before_any_side_effect(data,credits,minutes):
    a=module();raw=old.encode(data[-1])
    assert hasattr(a,'synthetic_execution_plan'), 'closed twelve execution plan is missing'
    budget=old.encode({'execution_day':'2026-11-11','credits':credits,'minutes':{}})
    with pytest.raises(old.RunStopped):a.synthetic_execution_plan(raw,old.sha(raw),budget,'2026-11-11',minutes)
