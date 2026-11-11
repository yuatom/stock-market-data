"""Synthetic values only; no HTTP or external Git endpoints."""
import copy
import json
from pathlib import Path

import pytest

from scripts import eleven_symbol_append as a
from scripts.simulate_eleven_symbol_append import LocalSyntheticAdapter, fixture_inputs


def setup(tmp_path):
    base, profiles, responses, pins = fixture_inputs()
    adapter = LocalSyntheticAdapter(tmp_path / 'run', base, responses, pins)
    return adapter, profiles


def test_exact_eleven_symbol_local_acceptance(tmp_path):
    adapter, profiles = setup(tmp_path)
    original = dict(adapter.files)
    result = a.run_local_acceptance(adapter, profiles)
    assert result['production_ready'] is False
    assert result['status'] == 'LOCAL_SYNTHETIC_COMPLETE'
    assert result['requests'] == result['credits'] == 11
    assert sum(x['new_rows'] for x in result['outcomes']) == 54
    assert all(x['status'] == 'APPENDED' for x in result['outcomes'])
    for symbol in a.SYMBOLS:
        before = a.read_series(original, symbol)
        after = a.read_series(adapter.files, symbol)
        assert after[:len(before)] == before
        assert len(after) == 265
        assert [x['trade_date'] for x in after[len(before):]] == list(a.dates_for(symbol))
        prefix = a.series_prefix(symbol)
        for name in ('000000.json', '000128.json'):
            assert adapter.files[prefix + name] == original[prefix + name]
        assert (adapter.root / symbol / 'response.raw').read_bytes() == adapter.responses[symbol]
        terminal = json.loads((adapter.root / symbol / 'terminal.json').read_bytes())
        assert terminal['retention']['raw_sha256'] == a.sha(adapter.responses[symbol])
        assert terminal['publication']['snapshot']
    assert adapter.files['unrelated.txt'] == original['unrelated.txt']


def mutate_response(adapter, symbol, action):
    doc = json.loads(adapter.responses[symbol])
    action(doc)
    adapter.responses[symbol] = a.encode(doc)


def test_volume_overlap_preserved_and_audited(tmp_path):
    adapter, profiles = setup(tmp_path)
    original = a.read_series(adapter.files, 'IBIT')
    mutate_response(adapter, 'IBIT', lambda d: d['values'][0].update(volume='999999'))
    result = a.run_local_acceptance(adapter, profiles)
    assert a.read_series(adapter.files, 'IBIT')[:260] == original
    assert result['outcomes'][0]['volume_conflicts'] == [original[0]['trade_date']]


@pytest.mark.parametrize('mutation,reason', [
    ('price', 'BASIS_MISMATCH'), ('precision_price', 'BASIS_MISMATCH'),
    ('missing', 'DATE_SET_INVALID'), ('duplicate', 'DATE_SET_INVALID'),
    ('future', 'DATE_SET_INVALID'), ('identity', 'IDENTITY_INVALID'),
    ('fractional_volume', 'QUALITY_INVALID'), ('ohlc', 'QUALITY_INVALID'),
    ('ambiguous', 'RAW_INVALID'), ('nonfinite', 'RAW_INVALID'),
])
def test_certain_validation_failures_isolate_only_after_terminal(tmp_path, mutation, reason):
    adapter, profiles = setup(tmp_path)
    if mutation == 'ambiguous': adapter.responses['IBIT'] = b'{"meta":{},"meta":{}}'
    elif mutation == 'nonfinite': adapter.responses['IBIT'] = b'{"values":NaN}'
    else:
        def edit(doc):
            if mutation == 'price': doc['values'][0]['open'] = '100.01'
            if mutation == 'precision_price': doc['values'][0]['open'] = '100.000000000000000001'
            if mutation == 'missing': doc['values'].pop(0)
            if mutation == 'duplicate': doc['values'][1] = doc['values'][0]
            if mutation == 'future': doc['values'][-1]['datetime'] = '2026-11-10'
            if mutation == 'identity': doc['meta']['symbol'] = 'SOXX'
            if mutation == 'fractional_volume': doc['values'][-1]['volume'] = '3.5'
            if mutation == 'ohlc': doc['values'][-1]['low'] = '999'
        mutate_response(adapter, 'IBIT', edit)
    original = a.read_series(adapter.files, 'IBIT')
    result = a.run_local_acceptance(adapter, profiles)
    assert result['outcomes'][0]['status'] == 'REJECTED'
    assert result['outcomes'][0]['reason'] == reason
    assert a.read_series(adapter.files, 'IBIT') == original
    assert len(a.read_series(adapter.files, 'IWM')) == 265
    assert result['requests'] == 11
    events = adapter.events
    assert events.index(('retain', 'IBIT')) < events.index(('terminal', 'IBIT')) < events.index(('claim', 'IWM'))


@pytest.mark.parametrize('stage', ['claim', 'debit', 'request', 'retain', 'publish', 'terminal'])
def test_unknown_boundary_stops_without_retry_or_next_symbol(tmp_path, stage):
    adapter, profiles = setup(tmp_path)
    adapter.fail_at = (stage, 'IBIT')
    with pytest.raises(a.RunStopped) as caught:
        a.run_local_acceptance(adapter, profiles)
    assert str(caught.value) == 'LOCAL_RUN_STOPPED'
    assert not any(symbol == 'IWM' for _, symbol in adapter.events)
    assert sum(event == (stage, 'IBIT') for event in adapter.events) == 1
    assert not (adapter.root / 'IWM').exists()


def test_retention_readback_mismatch_stops_before_parse_or_publish(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.corrupt_raw_readback = True
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert ('publish', 'IBIT') not in adapter.events
    assert ('terminal', 'IBIT') not in adapter.events


def test_debit_mismatch_stops_before_provider(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.corrupt_debit = True
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert ('request', 'IBIT') not in adapter.events


def test_publication_mismatch_stops_without_terminal_or_retry(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.corrupt_publication = True
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert ('terminal', 'IBIT') not in adapter.events
    assert adapter.events.count(('publish', 'IBIT')) == 1


def test_rejection_terminal_failure_stops_run(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.responses['IBIT'] = b'{}'
    adapter.fail_at = ('terminal', 'IBIT')
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert ('claim', 'IWM') not in adapter.events


def test_base_pin_mismatch_stops_before_claim(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.pins['IBIT']['000256.json'] = '0' * 40
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert adapter.events == []


@pytest.mark.parametrize('mutation', ['symbol', 'scope', 'basis', 'source', 'request_size'])
def test_profile_is_closed_and_preflighted_before_any_attempt(tmp_path, mutation):
    adapter, profiles = setup(tmp_path)
    if mutation == 'symbol': profiles['SOXX'] = profiles.pop('XLK')
    elif mutation == 'scope': profiles['XLK']['extra'] = True
    elif mutation == 'basis': profiles['XLK']['basis'] = 'unverified'
    elif mutation == 'source': profiles['XLK']['request']['provider'] = 'other'
    else: profiles['XLK']['request']['outputsize'] = 5000
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert adapter.events == []


def test_cannot_rerun_same_adapter_or_destination(tmp_path):
    adapter, profiles = setup(tmp_path)
    a.run_local_acceptance(adapter, profiles)
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    with pytest.raises(FileExistsError):
        LocalSyntheticAdapter(adapter.root, adapter.files, adapter.responses, adapter.pins)


def test_real_store_pins_are_not_a_public_config():
    assert not (Path(__file__).resolve().parents[1] / 'config/eleven-symbol-append-pins.json').exists()



@pytest.mark.parametrize('boundary', ['lock', 'between_symbols', 'retention', 'source'])
def test_context_drift_stops_before_request_or_publication(tmp_path, boundary):
    from contextlib import contextmanager
    adapter, profiles = setup(tmp_path)
    def corrupt_pending():
        path = a.series_prefix('IWM') + '000256.json'
        adapter.files[path] += b' '
    if boundary == 'lock':
        original = adapter.lock
        @contextmanager
        def lock(group):
            with original(group):
                corrupt_pending()
                yield
        adapter.lock = lock
    elif boundary == 'between_symbols':
        original = adapter.terminal
        def terminal(symbol, raw):
            result = original(symbol, raw)
            if symbol == 'IBIT': corrupt_pending()
            return result
        adapter.terminal = terminal
    else:
        original = adapter.retain
        def retain(*args):
            result = original(*args)
            if boundary == 'retention': adapter.files['unrelated.txt'] = b'drift'
            else: adapter.compute_sha = '0' * 40
            return result
        adapter.retain = retain
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    if boundary == 'lock': assert adapter.events == []
    elif boundary == 'between_symbols': assert ('claim', 'IWM') not in adapter.events
    else: assert ('publish', 'IBIT') not in adapter.events


def test_bad_base_is_never_isolatable_raw_rejection(tmp_path):
    adapter, profiles = setup(tmp_path)
    path = a.series_prefix('IBIT') + '_index.json'
    adapter.files[path] = b'{}'
    adapter.pins['IBIT']['_index.json'] = a.store.git_blob_sha_bytes(b'{}')
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert adapter.events == []


def test_module_cli_is_executable_and_never_claims_production(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'scripts.simulate_eleven_symbol_append',
                             '--output', str(tmp_path / 'cli')], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary['requests'] == 11
    assert summary['production_ready'] is False
    assert summary['hosted_durability'] is False


def test_existing_daily_usage_and_other_fields_are_preserved(tmp_path):
    base, profiles, responses, pins = fixture_inputs()
    budget = dict(execution_day='2026-11-11', credits=180,
                  minutes={'2026-11-11T12:00Z': 8}, unrelated={'keep': True})
    base[LocalSyntheticAdapter.BUDGET] = a.encode(budget)
    adapter = LocalSyntheticAdapter(tmp_path / 'run', base, responses, pins)
    result = a.run_local_acceptance(adapter, profiles)
    after = json.loads(adapter.files[adapter.BUDGET])
    assert after['credits'] == 191
    assert after['minutes']['2026-11-11T12:00Z'] == 8
    assert after['unrelated'] == {'keep': True}
    assert max(after['minutes'].values()) <= 8
    assert result['credits'] == 11
    assert result['execution_day'] == '2026-11-11'
    assert result['historical_cutoff'] == '2024-09-06'


@pytest.mark.parametrize('credits,minutes', [(200, {}), (190, {}), (0, {'2026-11-11T13:00Z': 8})])
def test_exhausted_headroom_stops_before_claim(tmp_path, credits, minutes):
    base, profiles, responses, pins = fixture_inputs()
    base[LocalSyntheticAdapter.BUDGET] = a.encode(dict(execution_day='2026-11-11', credits=credits, minutes=minutes))
    adapter = LocalSyntheticAdapter(tmp_path / 'run', base, responses, pins)
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert adapter.events == []


def test_same_minute_schedule_never_exceeds_eight_requests(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.minutes = ('2026-11-11T13:00Z',) * 11
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert len(adapter.requested) == 8
    assert ('claim', 'XLE') not in adapter.events
    assert json.loads(adapter.files[adapter.BUDGET])['credits'] == 8


def test_terminal_readback_corruption_stops_before_next_symbol(tmp_path):
    adapter, profiles = setup(tmp_path)
    original = adapter.read_terminal
    adapter.read_terminal = lambda symbol: original(symbol) + b'corrupt'
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert ('claim', 'IWM') not in adapter.events
    assert len(a.read_series(adapter.files, 'IBIT')) == 265


def test_later_unknown_publication_keeps_prior_success_and_never_continues(tmp_path):
    adapter, profiles = setup(tmp_path)
    adapter.fail_at = ('publish', 'IWM')
    with pytest.raises(a.RunStopped): a.run_local_acceptance(adapter, profiles)
    assert len(a.read_series(adapter.files, 'IBIT')) == 265
    assert (adapter.root / 'IBIT' / 'terminal.json').exists()
    assert not (adapter.root / 'IWM' / 'terminal.json').exists()
    assert ('claim', 'KWEB') not in adapter.events
