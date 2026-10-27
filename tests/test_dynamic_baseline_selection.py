"""Synthetic identity-bound dynamic history targets; no production or HTTP."""
import copy
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import admit_market_data_baseline as admission
import market_data_store as store


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def init(repo):
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'test')
    git(repo, 'config', 'user.email', 'test@example.invalid')


@pytest.fixture
def source(tmp_path):
    discovery, data = tmp_path / 'discovery', tmp_path / 'store'
    init(discovery); init(data)
    request={'schema_version':1, 'request_id':'synthetic-request', 'requested_at':'2026-10-17T12:00:00-04:00',
             'trade_date':'2026-10-17','stage':'close','request_purpose':'opportunity_discovery',
             'research_repository':'yuatom/stock-dairy','research_repository_commit_sha':'a'*40,
             'market_data_contract_sha':'b'*40,'candidate_symbols':['AAA','BBB','CCC'],
             'transaction_id':'synthetic-close','research_universe_resolution_status':'resolved',
             'personal_state_proof':{'dynamic_state_read_sha':'c'*40,'blob_sha':'d'*40,'state_version':1,
                 'authority_state':'canonical','content_hash_status':'canonical_sha256','content_sha256':'e'*64}}
    req=discovery/'requests/dynamic-candidate-request.json'; req.parent.mkdir();req.write_text(json.dumps(request))
    git(discovery,'add','.');git(discovery,'commit','-qm','original request')
    commit=git(discovery,'rev-parse','HEAD');git(discovery,'update-ref','refs/remotes/origin/discovery-requests',commit)
    bars=[{'trade_date':f'2026-10-{day:02}','open':10,'high':12,'low':9,'close':11,'volume':100} for day in (13,14,15)]
    for symbol in ('AAA','BBB','CCC'):
        store.append_daily_bars(data/'data/market-data',provider='twelve_data_basic',symbol=symbol,
            asset_class='stocks',bars=bars,series_semantics='daily_regular_ohlcv')
    result={'schema_version':1,'request_id':request['request_id'],'request_purpose':request['request_purpose'],
        'research_repository_commit_sha':request['research_repository_commit_sha'],
        'market_data_contract_sha':request['market_data_contract_sha'],'candidate_symbols':request['candidate_symbols'],
        'mode':'discovery_close','snapshot_stage':'discovery_close','persistent_collection_membership':False,
        'daily_history_available':['CCC'],'daily_history_failures':[{'symbol':x,'reason':f'MarketDataConflict:conflicting bar for {x} 2026-10-15'} for x in ('AAA','BBB')]}
    path=data/'data/market-data/collector-state/2026-10/2026-10-17-dynamic-candidate-close.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(result))
    git(data,'add','.');git(data,'commit','-qm','stored source and histories')
    data_sha=git(data,'rev-parse','HEAD');h=admission.load_helpers()
    selector={'request_commit_sha':commit,'request_blob_sha':git(discovery,'rev-parse',f'{commit}:requests/dynamic-candidate-request.json'),
              'result_blob_sha':git(data,'rev-parse',f'{data_sha}:{path.relative_to(data)}'),
              'targets':[{'symbol':x,'index_blob_sha':git(data,'rev-parse',f'{data_sha}:data/market-data/series/daily/twelve_data_basic/{x}/_index.json')} for x in ('AAA','BBB')]}
    universe={'daily_series':[{'symbol':'FIX','asset_class':'etf'}]}
    return h,data,data_sha,discovery,selector,universe


def resolve(source, selector=None, **kwargs):
    h,repo,sha,discovery,selection,universe=source
    m=importlib.import_module('dynamic_baseline_targets')
    return m.resolve_dynamic_targets(h,store_repo=repo,store_sha=kwargs.get('store_sha',sha),
        discovery_repo=discovery,selector=selection if selector is None else selector,universe=universe,day="2026-10-18")


def test_two_failed_dynamic_histories_resolve_without_membership_mutation(source):
    h,repo,sha,discovery,selection,universe=source
    before=git(repo,'status','--porcelain')
    result=resolve(source)
    assert [x['symbol'] for x in result]==['AAA','BBB']
    assert all(x['asset_class']=='stocks' and x['effective'] is None for x in result)
    assert universe=={'daily_series':[{'symbol':'FIX','asset_class':'etf'}]}
    assert git(repo,'status','--porcelain')==before


@pytest.mark.parametrize('mutation',['request_blob','request_commit','result_blob','index_blob','duplicate','missing','available','extra_field'])
def test_unproven_dynamic_selection_fails_closed(source,mutation):
    selector=copy.deepcopy(source[4])
    if mutation=='request_blob':selector['request_blob_sha']='0'*40
    if mutation=='request_commit':selector['request_commit_sha']='0'*40
    if mutation=='result_blob':selector['result_blob_sha']='0'*40
    if mutation=='index_blob':selector['targets'][0]['index_blob_sha']='0'*40
    if mutation=='duplicate':selector['targets'].append(copy.deepcopy(selector['targets'][0]))
    if mutation=='missing':selector['targets']=[]
    if mutation=='available':selector['targets'][0]['symbol']='CCC'
    if mutation=='extra_field':selector['arbitrary']='not authority'
    with pytest.raises(Exception): resolve(source,selector)
    assert git(source[1],'status','--porcelain')==''


def test_wrong_store_commit_fails_before_any_selection(source):
    with pytest.raises(Exception):resolve(source,store_sha='0'*40)


def test_later_rewritten_request_with_same_id_cannot_replace_original(source):
    h,repo,sha,discovery,selector,universe=source
    path=discovery/'requests/dynamic-candidate-request.json';request=json.loads(path.read_text())
    request['personal_state_proof']['state_version']+=1;path.write_text(json.dumps(request))
    git(discovery,'add','.');git(discovery,'commit','-qm','later request rewrite')
    commit=git(discovery,'rev-parse','HEAD');git(discovery,'update-ref','refs/remotes/origin/discovery-requests',commit)
    altered=copy.deepcopy(selector);altered['request_commit_sha']=commit;altered['request_blob_sha']=git(discovery,'rev-parse',f'{commit}:requests/dynamic-candidate-request.json')
    with pytest.raises(Exception):resolve(source,altered)


@pytest.mark.parametrize('mutation',['wrong_request','wrong_purpose','wrong_contract','wrong_candidate_set','bad_availability'])
def test_result_must_match_original_request_even_at_new_store_pin(source,mutation):
    h,repo,sha,discovery,selector,universe=source
    path=repo/'data/market-data/collector-state/2026-10/2026-10-17-dynamic-candidate-close.json'
    result=json.loads(path.read_text())
    if mutation=='wrong_request':result['request_id']='another-request'
    if mutation=='wrong_purpose':result['request_purpose']='personal_membership'
    if mutation=='wrong_contract':result['market_data_contract_sha']='0'*40
    if mutation=='wrong_candidate_set':result['candidate_symbols']=['AAA','BBB']
    if mutation=='bad_availability':result['daily_history_available']='CCC'
    path.write_text(json.dumps(result));git(repo,'add','.');git(repo,'commit','-qm','tampered result')
    new_sha=git(repo,'rev-parse','HEAD');selection=copy.deepcopy(selector)
    selection['result_blob_sha']=git(repo,'rev-parse',f'{new_sha}:{path.relative_to(repo)}')
    with pytest.raises(Exception):resolve(source,selection,store_sha=new_sha)


def test_index_referencing_corrupt_or_untracked_shard_fails_selection(source):
    repo=source[1];shard=repo/'data/market-data/series/daily/twelve_data_basic/AAA/000000.json'
    shard.write_text('{}')
    with pytest.raises(Exception):resolve(source)


def test_workflow_uses_same_selection_in_both_guarded_jobs():
    import yaml
    workflow=yaml.safe_load((ROOT/'.github/workflows/market-data-collector.yml').read_text())
    assert 'dynamic_selection' in workflow[True]['workflow_dispatch']['inputs']
    assert "inputs.dynamic_selection == ''" in workflow['jobs']['run']['if']
    for name in ('admission','recovery'):
        job=workflow['jobs'][name]
        assert job['concurrency']=={'group':'market-data-collector','cancel-in-progress':False,'queue':'max'}
        text=json.dumps(job)
        assert 'discovery-requests' in text and '--dynamic-selection' in text and '--discovery-repo' in text


@pytest.mark.parametrize('raw',['null','[]','""','false','0','{'])
def test_explicit_invalid_selection_never_becomes_default(raw):
    m=importlib.import_module('dynamic_baseline_targets')
    with pytest.raises(m.DynamicTargetError):m.parse_dynamic_selection(raw)


def test_omitted_selection_alone_uses_default():
    m=importlib.import_module('dynamic_baseline_targets')
    assert m.parse_dynamic_selection(None) is None
    assert m.parse_dynamic_selection('{}') == {}


def test_explicit_null_cannot_enter_either_guarded_executor(monkeypatch, capsys):
    from unittest.mock import Mock
    import recover_market_data_baseline as recovery
    sha='a'*40
    for key,value in {'GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'workflow_dispatch',
                      'GITHUB_REPOSITORY':'yuatom/stock-market-data','GITHUB_REF':'refs/heads/main',
                      'GITHUB_SHA':sha,'TWELVE_DATA_API_KEY':'SYNTHETIC'}.items():
        monkeypatch.setenv(key,value)
    args=['--trade-date','2026-10-18','--compute-sha',sha,'--store-sha','b'*40,
          '--store-repo','/unused','--request-id','1-1','--dynamic-selection','null']
    recover=Mock();monkeypatch.setattr(recovery,'recover',recover)
    assert recovery.main(args)==2;recover.assert_not_called()
    execute=Mock();monkeypatch.setattr(admission,'execute',execute)
    monkeypatch.setattr(admission,'git',lambda repo,*argv: sha if argv[0]=='rev-parse' else '')
    monkeypatch.setattr(admission,'validate_target',lambda *args:None)
    monkeypatch.setattr(admission,'StorePublisher',Mock())
    assert admission.main(args)==2;execute.assert_not_called()
    assert 'SYNTHETIC' not in capsys.readouterr().out


@pytest.mark.parametrize('mutation',['missing_old','truncated','outside_tail','four_revisions'])
def test_selected_dynamic_history_keeps_original_coverage_and_revision_limits(source,mutation):
    h,repo,sha,discovery,selector,universe=source
    targets=resolve(source)
    config={'series':{'analysis_windows_may_read_last_sessions':[3]}}
    candidate=admission.prepare_series(h,repo/'data/market-data',universe,config,'2026-10-18',targets=targets)[0]
    previous=[dict(candidate['existing'][0],trade_date=d) for d in ('2026-10-09','2026-10-12')]
    candidate['existing']=previous+candidate['existing'];replacement=copy.deepcopy(candidate['existing'])
    if mutation=='missing_old':replacement.pop(0)
    if mutation=='truncated':replacement=replacement[-3:]
    if mutation=='outside_tail':replacement[0]['volume']+=1
    if mutation=='four_revisions':
        for row in replacement[-4:]:row['volume']+=1
    with pytest.raises(admission.AdmissionError):
        admission.compare_replacement(h,candidate,replacement,'2026-10-18',
            {'maximum_revised_overlap_records':3,'revised_overlap_must_be_within_last_existing_sessions':3})


def test_changed_index_after_failure_is_stale_even_if_conflict_remains_in_tail(source):
    h,repo,sha,discovery,selector,universe=source
    store.append_daily_bars(repo/'data/market-data',provider='twelve_data_basic',symbol='AAA',
        asset_class='stocks',series_semantics='daily_regular_ohlcv',
        bars=[{'trade_date':'2026-10-16','open':10,'high':12,'low':9,'close':11,'volume':100}])
    git(repo,'add','.');git(repo,'commit','-qm','later history')
    new_sha=git(repo,'rev-parse','HEAD');selection=copy.deepcopy(selector)
    selection['targets'][0]['index_blob_sha']=git(repo,'rev-parse',f'{new_sha}:data/market-data/series/daily/twelve_data_basic/AAA/_index.json')
    with pytest.raises(Exception):resolve(source,selection,store_sha=new_sha)


def test_selector_json_duplicate_keys_are_rejected():
    m=importlib.import_module('dynamic_baseline_targets')
    with pytest.raises(m.DynamicTargetError):
        m.parse_dynamic_selection('{"targets":[],"targets":[]}')


def test_result_commit_must_contain_original_matching_shards(source):
    h,repo,sha,discovery,selector,universe=source
    path='data/market-data/series/daily/twelve_data_basic/AAA/000000.json'
    git(repo,'rm','--cached',path);git(repo,'commit','--amend','--no-edit','-q')
    git(repo,'add',path);git(repo,'commit','-qm','later shard restoration')
    new_sha=git(repo,'rev-parse','HEAD')
    with pytest.raises(Exception):resolve(source,copy.deepcopy(selector),store_sha=new_sha)
