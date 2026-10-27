from pathlib import Path
import pytest
from test_deterministic_metric_proof import store, proofmod


def _capture(root: Path, *, capture_id, provider, timestamp, field, price, contract, date='2026-10-26', session='regular'):
    fact = {'symbol': 'AAA', 'asset_class': 'stocks', 'session': session,
            'event_time': timestamp, 'source_timestamp': timestamp, field: price, 'source_contract': contract}
    path, blob = store.write_capture(root, trade_date=date, session=session, provider=provider,
        capture_id=capture_id, generated_at=timestamp, actual_data_cutoff=timestamp,
        window={'start': '15:45', 'end': '16:00'}, feed_scope=contract, qualified_facts=[fact])
    return {'path': path, 'blob_sha': blob, 'kind': 'regular_intraday_capture'}


def test_close_selects_twelve_minute_close_over_inherited_morning_last_sale(tmp_path):
    root = tmp_path / 'data' / 'market-data'
    early = _capture(root, capture_id='morning', provider='nasdaq_public_intraday',
        timestamp='2026-10-26T10:29:00-04:00', field='last_sale', price=100,
        contract='nasdaq_public_chart_last_sale_volume_v1')
    tail = _capture(root, capture_id='tail', provider='twelve_data_basic',
        timestamp='2026-10-26T15:59:00-04:00', field='close', price=105,
        contract='twelve_data_basic_regular_1min_v1')
    snapshot = {'stage':'close', 'trade_date':'2026-10-26',
                'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[early,tail]}
    target = proofmod._target_observations(root, snapshot)['AAA']
    assert target['event_time'] == '2026-10-26T15:59:00-04:00'
    assert target['capture_path'] == tail['path']
    assert target['close'] == 105
    assert target['price_field'] == 'close'
    assert target['price_semantics'] == 'twelve_data_basic_regular_1min_close'

@pytest.mark.parametrize('timestamp,date,session', [
    ('2026-10-26T10:29:00-04:00','2026-10-26','regular'),
    ('2026-10-25T15:59:00-04:00','2026-10-25','regular'),
    ('2026-10-26T15:59:00-04:00','2026-10-26','premarket'),
    ('2026-10-26T16:00:00-04:00','2026-10-26','regular'),
])
def test_close_excludes_wrong_day_session_and_outside_window(tmp_path, timestamp, date, session):
    root = tmp_path / 'data' / 'market-data'
    ref = _capture(root, capture_id='candidate', provider='nasdaq_public_intraday', timestamp=timestamp,
        field='last_sale', price=100, contract='nasdaq_public_chart_last_sale_volume_v1', date=date, session=session)
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[ref]}
    assert 'AAA' not in proofmod._target_observations(root, snapshot)


def test_close_twelve_only_is_qualified(tmp_path):
    root = tmp_path / 'data' / 'market-data'
    ref = _capture(root, capture_id='tail', provider='twelve_data_basic', timestamp='2026-10-26T15:59:00-04:00',
        field='close', price=105, contract='twelve_data_basic_regular_1min_v1')
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[ref]}
    assert proofmod._target_observations(root, snapshot)['AAA']['close'] == 105


def test_proof_schema_accepts_minute_close_without_relabeling(tmp_path):
    import json
    from jsonschema import Draft202012Validator
    root = tmp_path / 'data' / 'market-data'
    ref = _capture(root, capture_id='tail', provider='twelve_data_basic', timestamp='2026-10-26T15:59:00-04:00',
        field='close', price=105, contract='twelve_data_basic_regular_1min_v1')
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[ref]}
    target = proofmod._target_observations(root, snapshot)['AAA']
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/deterministic-metric-proof.schema.json').read_text())
    target_schema = schema['properties']['subjects']['items']['properties']['target']
    Draft202012Validator(target_schema).validate(target)


def test_close_rejects_conflicting_price_fields_and_nonfinite(tmp_path):
    import json
    root = tmp_path / 'data' / 'market-data'
    ref = _capture(root, capture_id='tail', provider='twelve_data_basic', timestamp='2026-10-26T15:59:00-04:00',
        field='close', price=105, contract='twelve_data_basic_regular_1min_v1')
    path = root / ref['path']
    obj = json.loads(path.read_text())
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[ref]}
    for mutation, message in [({'last_sale': 105}, 'ambiguous price fields'), ({'close': float('nan')}, 'finite')]:
        obj['qualified_facts'][0].update(mutation)
        raw = (json.dumps(obj) + '\n').encode(); path.write_bytes(raw); ref['blob_sha'] = proofmod.git_blob_sha_bytes(raw)
        with pytest.raises(proofmod.MetricProofError, match=message):
            proofmod._target_observations(root, snapshot)
        obj['qualified_facts'][0].pop('last_sale', None)


def test_close_rejects_same_time_different_capture_identity(tmp_path):
    root = tmp_path / 'data' / 'market-data'
    refs = [_capture(root, capture_id=name, provider='twelve_data_basic', timestamp='2026-10-26T15:59:00-04:00',
        field='close', price=105, contract='twelve_data_basic_regular_1min_v1') for name in ('one','two')]
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':refs}
    with pytest.raises(proofmod.MetricProofError, match='ambiguous selected target'):
        proofmod._target_observations(root, snapshot)


def test_close_detects_capture_blob_tamper(tmp_path):
    root = tmp_path / 'data' / 'market-data'
    ref = _capture(root, capture_id='tail', provider='twelve_data_basic', timestamp='2026-10-26T15:59:00-04:00',
        field='close', price=105, contract='twelve_data_basic_regular_1min_v1')
    (root / ref['path']).write_text('{}')
    snapshot = {'stage':'close', 'trade_date':'2026-10-26', 'target_window':{'start':'15:45','end':'16:00'}, 'data_refs':[ref]}
    with pytest.raises(proofmod.MetricProofError, match='blob mismatch'):
        proofmod._target_observations(root, snapshot)


def test_close_same_instant_with_different_offsets_is_ambiguous(tmp_path):
    root=tmp_path/'data'/'market-data'
    a=_capture(root,capture_id='et',provider='twelve_data_basic',timestamp='2026-10-26T15:59:00-04:00',field='close',price=105,contract='twelve_data_basic_regular_1min_v1')
    b=_capture(root,capture_id='utc',provider='twelve_data_basic',timestamp='2026-10-26T19:59:00+00:00',field='close',price=106,contract='twelve_data_basic_regular_1min_v1')
    snapshot={'stage':'close','trade_date':'2026-10-26','target_window':{'start':'15:45','end':'16:00'},'data_refs':[a,b]}
    with pytest.raises(proofmod.MetricProofError,match='ambiguous selected target'):
        proofmod._target_observations(root,snapshot)


def test_close_proof_selects_tail_and_verifier_rejects_inherited_morning(tmp_path):
    import copy
    from test_deterministic_metric_proof import _seed
    root,_ = _seed(tmp_path)
    refs=[]
    for symbol,price in [('AAA',170.0),('SPY',470.0),('QQQ',580.0)]:
        ts='2026-09-05T15:59:00-04:00'
        fact={'symbol':symbol,'session':'regular','event_time':ts,'source_timestamp':ts,'close':price,'source_contract':'twelve_data_basic_regular_1min_v1'}
        path,blob=store.write_capture(root,trade_date='2026-09-05',session='regular',provider='twelve_data_basic',capture_id='tail-'+symbol,generated_at=ts,actual_data_cutoff=ts,window={'start':'15:45','end':'16:00'},feed_scope='fixture',qualified_facts=[fact])
        refs.append({'path':path,'blob_sha':blob,'kind':'regular_intraday_capture'})
    store.write_snapshot(root,stage='close',trade_date='2026-09-05',snapshot_id='close-fixture',generated_at='2026-09-05T16:01:00-04:00',data_refs=refs,coverage={'available':3},missing=[],target_window={'start':'15:45','end':'16:00'},actual_data_cutoff='2026-09-05T15:59:00-04:00')
    proof,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='close',data_plane_commit_sha='a'*40)
    aaa=next(s for s in proof['subjects'] if s['symbol']=='AAA')
    assert aaa['target']['close']==170.0
    assert aaa['target']['event_time']=='2026-09-05T15:59:00-04:00'
    assert aaa['benchmark_metrics']['SPY']['status']=='available'
    assert aaa['metrics']['returns_pct']['1session']==round((170/159-1)*100,6)
    omitted=copy.deepcopy(proof); omitted['subjects'].pop()
    with pytest.raises(proofmod.MetricProofError):
        proofmod.verify_proof_source_blobs(root,omitted)
    altered=copy.deepcopy(proof);next(s for s in altered['subjects'] if s['symbol']=='AAA')['target']['event_time']='2026-09-05T10:29:00-04:00'
    with pytest.raises(proofmod.MetricProofError,match='not the snapshot-selected'):
        proofmod.verify_proof_source_blobs(root,altered)


def test_close_accepts_partial_symbol_with_earlier_in_window_observation(tmp_path):
    root=tmp_path/'data'/'market-data'
    ref=_capture(root,capture_id='tail',provider='twelve_data_basic',timestamp='2026-10-26T15:58:00-04:00',field='close',price=105,contract='twelve_data_basic_regular_1min_v1')
    snapshot={'stage':'close','trade_date':'2026-10-26','target_window':{'start':'15:45','end':'16:00'},'data_refs':[ref]}
    assert proofmod._target_observations(root,snapshot)['AAA']['event_time']=='2026-10-26T15:58:00-04:00'


def test_close_build_records_missing_if_only_inherited_morning(tmp_path):
    from test_deterministic_metric_proof import _seed
    root,_=_seed(tmp_path)
    early=_capture(root,capture_id='early',provider='nasdaq_public_intraday',timestamp='2026-09-05T10:29:00-04:00',field='last_sale',price=100,contract='nasdaq_public_chart_last_sale_volume_v1',date='2026-09-05')
    store.write_snapshot(root,stage='close',trade_date='2026-09-05',snapshot_id='missing-close',generated_at='2026-09-05T16:01:00-04:00',data_refs=[early],coverage={'available':1},missing=[],target_window={'start':'15:45','end':'16:00'},actual_data_cutoff='2026-09-05T10:29:00-04:00')
    proof,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='close',data_plane_commit_sha='a'*40)
    assert proof['subjects']==[]
    assert {'symbol':'AAA','reason':'qualified_stage_target_unavailable'} in proof['missing']
    proof['missing']=[]
    with pytest.raises(proofmod.MetricProofError,match='missing target'):
        proofmod.verify_proof_source_blobs(root,proof)


def _partition_fixture(tmp_path):
    from test_deterministic_metric_proof import _seed
    root,_ = _seed(tmp_path)
    ref=_capture(root,capture_id='partition-tail',provider='twelve_data_basic',timestamp='2026-09-05T15:59:00-04:00',field='close',price=170,contract='twelve_data_basic_regular_1min_v1',date='2026-09-05')
    store.write_snapshot(root,stage='close',trade_date='2026-09-05',snapshot_id='partition-fixture',generated_at='2026-09-05T16:01:00-04:00',data_refs=[ref],coverage={'available':1},missing=[],target_window={'start':'15:45','end':'16:00'},actual_data_cutoff='2026-09-05T15:59:00-04:00')
    proof,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='close',data_plane_commit_sha='a'*40)
    return root,proof


@pytest.mark.parametrize('mutation', ['empty','omit','duplicate','false_missing_daily','overlap','extra','invalid_missing'])
def test_reference_verifier_rejects_incomplete_or_forged_partition(tmp_path,mutation):
    import copy
    root,proof=_partition_fixture(tmp_path); altered=copy.deepcopy(proof)
    if mutation in ('empty','omit'): altered['subjects']=[]
    if mutation=='duplicate': altered['subjects'].append(copy.deepcopy(altered['subjects'][0]))
    if mutation=='false_missing_daily':
        altered['subjects']=[]; altered['missing']=[{'symbol':'AAA','reason':'provider_affine_daily_series_unavailable'}]
    if mutation=='overlap': altered['missing']=[{'symbol':'AAA','reason':'provider_affine_daily_series_unavailable'}]
    if mutation=='extra': altered['missing']=[{'symbol':'EXTRA','reason':'qualified_stage_target_unavailable'}]
    if mutation=='invalid_missing': altered['missing']=[{'symbol':'AAA','reason':'fabricated','extra':'invalid'}]
    with pytest.raises(proofmod.MetricProofError): proofmod.verify_proof_source_blobs(root,altered)


def test_reference_verifier_accepts_proven_missing_daily_series_and_rejects_corruption(tmp_path):
    import shutil
    root,proof=_partition_fixture(tmp_path)
    shutil.rmtree(root/'series/daily/twelve_data_basic/AAA')
    missing=dict(proof);missing['subjects']=[];missing['missing']=[{'symbol':'AAA','reason':'provider_affine_daily_series_unavailable'}]
    proofmod.verify_proof_source_blobs(root,missing)
    bad=root/'series/daily/twelve_data_basic/AAA/_index.json';bad.parent.mkdir(parents=True);bad.write_text('{bad json')
    with pytest.raises((proofmod.MetricProofError,ValueError)): proofmod.verify_proof_source_blobs(root,missing)


def test_missing_target_includes_observed_symbol_without_timestamp(tmp_path):
    root=tmp_path/'data'/'market-data';ts='2026-09-05T15:59:00-04:00'
    fact={'symbol':'AAA','session':'regular','close':105,'source_contract':'twelve_data_basic_regular_1min_v1'}
    path,blob=store.write_capture(root,trade_date='2026-09-05',session='regular',provider='twelve_data_basic',capture_id='no-event',generated_at=ts,actual_data_cutoff=ts,window={'start':'15:45','end':'16:00'},feed_scope='fixture',qualified_facts=[fact])
    store.write_snapshot(root,stage='close',trade_date='2026-09-05',snapshot_id='no-event',generated_at='2026-09-05T16:01:00-04:00',data_refs=[{'path':path,'blob_sha':blob}],coverage={'available':1},missing=[],target_window={'start':'15:45','end':'16:00'},actual_data_cutoff=ts)
    proof,_=proofmod.build_proof(root,trade_date='2026-09-05',stage='close',data_plane_commit_sha='a'*40)
    assert proof['subjects']==[]
    assert proof['missing']==[{'symbol':'AAA','reason':'qualified_stage_target_unavailable'}]
    proofmod.verify_proof_source_blobs(root,proof)
