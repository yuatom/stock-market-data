"""Closed append builder and LOCAL SYNTHETIC acceptance coordinator.

No HTTP, credentials, Git publisher, hosted lock or production entrypoint. The
reviewed pins are metadata, not permission. Local receipts do not prove hosted
retention or provider origin. Never route a real provider through this simulator.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from datetime import date, datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from scripts import market_data_store as store

SYMBOLS = ('IBIT', 'IWM', 'KWEB', 'QQQ', 'SMH', 'SPY', 'TLT', 'VIXY', 'XLE', 'XLU', 'XLK')
# Public simulator constants are fabricated and are never official runtime scope.
DATES = ('2024-09-02', '2024-09-03', '2024-09-04', '2024-09-05', '2024-09-06')
START = '2023-08-01'
SYNTHETIC_STORE = 'a' * 40
SYNTHETIC_COMPUTE = 'c' * 40
PREFIX = 'data/market-data/'
FILENAMES = ('000000.json', '000128.json', '000256.json', '_index.json')
FIELDS = ('open', 'high', 'low', 'close', 'volume')
MAX_RAW_BYTES = 4 * 1024 * 1024
ISOLATABLE = frozenset({'RAW_INVALID', 'IDENTITY_INVALID', 'DATE_SET_INVALID', 'QUALITY_INVALID', 'BASIS_MISMATCH'})
REQUEST_KEYS = set('provider symbol interval exchange mic_code currency type adjust dp start_date end_date outputsize order'.split())


class ValidationRejected(ValueError):
    """Only deterministic prepublication raw validation failures are isolatable."""


class RunStopped(RuntimeError):
    """Uncertain or invalid state. No automated retry, repair, refund or resume."""


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition):
    if not condition:
        raise RunStopped('LOCAL_RUN_STOPPED')


def reject(condition, reason):
    if not condition:
        raise ValidationRejected(reason)


def strict_load(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_):
        raise ValueError()
    try:
        reject(type(raw) is bytes and 0 < len(raw) <= MAX_RAW_BYTES, 'RAW_INVALID')
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationRejected('RAW_INVALID') from None


def dates_for(symbol):
    require(symbol in SYMBOLS)
    return DATES[1:] if symbol == 'XLK' else DATES


def series_prefix(symbol, *, symbols=SYMBOLS):
    require(symbol in symbols)
    return PREFIX + 'series/daily/twelve_data_basic/' + symbol + '/'


def validate_profile(symbol, profile):
    """Profiles accepted here are explicitly synthetic, never production authority."""
    require(type(profile) is dict and set(profile) == {'request', 'basis'})
    require(profile['basis'] == 'SYNTHETIC_SPLITS_COMPATIBLE')
    request = profile['request']
    require(type(request) is dict and set(request) == REQUEST_KEYS)
    expected = dict(provider='twelve_data', symbol=symbol, interval='1day', exchange='SYNTHETIC',
                    mic_code='TEST', currency='USD', type='ETF', adjust='splits', dp=-1,
                    start_date=START, end_date=DATES[-1], outputsize=265, order='asc')
    require(encode(request) == encode(expected))
    return copy.deepcopy(request)


def synthetic_scope(symbol, files):
    return dict(symbol=symbol,old_count=261 if symbol=='XLK' else 260,
        first_date=START,last_date=DATES[0] if symbol=='XLK' else '2024-08-30',
        addition_dates=list(dates_for(symbol)),files=copy.deepcopy(files))


def validate_scope(scope, symbol):
    require(type(scope) is dict and set(scope)=={'symbol','old_count','first_date','last_date','addition_dates','files'})
    require(scope['symbol']==symbol and type(scope['old_count']) is int and 1<=scope['old_count']<=5000)
    dates=scope['addition_dates']
    require(type(dates) is list and 1<=len(dates)<=16 and dates==sorted(set(dates)))
    for value in [scope['first_date'],scope['last_date'],*dates]:
        require(type(value) is str and date.fromisoformat(value).isoformat()==value)
    require(scope['first_date']<=scope['last_date']<dates[0] and scope['old_count']+len(dates)<=5000)
    files=scope['files']
    require(type(files) is dict and '_index.json' in files and 2<=len(files)<=41)
    require(all((name=='_index.json' or re.fullmatch(r'[0-9]{6}\.json',name)) and
                type(blob) is str and re.fullmatch('[0-9a-f]{40}',blob) for name,blob in files.items()))
    return scope


def series_names(files, symbol, *, symbols=SYMBOLS):
    prefix=series_prefix(symbol, symbols=symbols)
    try:
        index=strict_load(files[prefix+'_index.json'])
        metas=index['shards']
        require(type(metas) is list and 1<=len(metas)<=40)
        require(all(type(m) is dict and all(type(m.get(k)) is int for k in ('start','end','count')) and
                    1<=m['count']<=128 and type(m.get('sealed')) is bool and
                    m.get('path')==f"{m['start']:06d}.json" for m in metas))
        names=[m['path'] for m in metas]
        require(all(type(n) is str and re.fullmatch(r'[0-9]{6}\.json',n) for n in names) and len(names)==len(set(names)))
        require([m['start'] for m in metas]==[sum(metas[j]['count'] for j in range(i)) for i in range(len(metas))])
        require([m['sealed'] for m in metas]==[True]*(len(metas)-1)+[False])
        require({p for p in files if p.startswith(prefix)}=={prefix+n for n in names+['_index.json']})
        return names+['_index.json']
    except Exception:
        raise RunStopped('LOCAL_RUN_STOPPED') from None


def read_series(files, symbol, *, symbols=SYMBOLS):
    """Read only the immutable index-declared closure, with no public inventory pins."""
    prefix=series_prefix(symbol, symbols=symbols);names=series_names(files,symbol, symbols=symbols)
    index=strict_load(files[prefix+'_index.json'])
    require(type(index) is dict and index.get('symbol')==symbol and index.get('provider')=='twelve_data_basic' and
            index.get('asset_class')=='etf' and index.get('series_id')==f'twelve_data_basic:{symbol}:regular:1d' and
            index.get('session')=='regular' and index.get('interval')=='1d' and
            index.get('series_semantics')=='daily_regular_ohlcv' and index.get('adjustment_semantics')=='provider_reported' and
            index.get('sort_order')=='trade_date_asc' and index.get('shard_record_limit')==128 and
            index.get('shard_byte_limit')==24576 and type(index.get('lineage_segments')) is list and
            type(index.get('baseline_initialization')) is dict)
    with tempfile.TemporaryDirectory(prefix='eleven-read-') as directory:
        root=Path(directory)
        for name in names:
            target=root/prefix/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(files[prefix+name])
        rows=store.read_daily_series(root/PREFIX,provider='twelve_data_basic',symbol=symbol)
    require(rows and index.get('first_trade_date')==rows[0]['trade_date'] and index.get('last_trade_date')==rows[-1]['trade_date'])
    return rows


def validate_base(files, symbol, blob_pins, scope=None, *, symbols=SYMBOLS):
    scope=validate_scope(scope if scope is not None else synthetic_scope(symbol,blob_pins),symbol)
    require(scope['files']==blob_pins and set(blob_pins)==set(series_names(files,symbol, symbols=symbols)))
    prefix=series_prefix(symbol, symbols=symbols)
    require(all(store.git_blob_sha_bytes(files[prefix+name])==digest for name,digest in blob_pins.items()))
    old=read_series(files,symbol, symbols=symbols)
    require(len(old)==scope['old_count'] and old[0]['trade_date']==scope['first_date'] and old[-1]['trade_date']==scope['last_date'])
    return old


def inspect_raw(raw, request, old, symbol, scope):
    """Validate exact decimal values and basis BEFORE lossy normalization."""
    doc = strict_load(raw)
    reject(type(doc) is dict and set(doc) == {'status', 'meta', 'values'} and doc['status'] == 'ok', 'RAW_INVALID')
    expected = {k: request[k] for k in ('symbol', 'interval', 'currency', 'exchange', 'mic_code', 'type')}
    expected['exchange_timezone'] = 'America/New_York'
    reject(doc['meta'] == expected, 'IDENTITY_INVALID')
    rows = doc['values']
    reject(type(rows) is list and len(rows) == request['outputsize'], 'DATE_SET_INVALID')
    reject(all(type(r) is dict and set(r) == {'datetime', *FIELDS} for r in rows), 'RAW_INVALID')
    reject([r['datetime'] for r in rows] == [r['trade_date'] for r in old] + scope['addition_dates'], 'DATE_SET_INVALID')
    values = []
    for row in rows:
        numbers = {}
        for field in FIELDS:
            value = row[field]
            reject(type(value) is str and len(value) <= 64 and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value), 'QUALITY_INVALID')
            number = Decimal(value)
            reject(number >= 0 if field == 'volume' else number > 0, 'QUALITY_INVALID')
            reject(math.isfinite(float(number)), 'QUALITY_INVALID')
            if field == 'volume':
                reject(number == number.to_integral_value() and number <= 2**53, 'QUALITY_INVALID')
            numbers[field] = number
        reject(numbers['low'] <= min(numbers['open'], numbers['close']) <=
               max(numbers['open'], numbers['close']) <= numbers['high'], 'QUALITY_INVALID')
        values.append(numbers)
    conflicts = []
    for prior, current in zip(old, values):
        reject(all(type(prior.get(f)) in (int, float) and math.isfinite(prior[f]) for f in FIELDS), 'BASIS_MISMATCH')
        reject(all(Decimal(str(prior[f])) == current[f] for f in FIELDS[:-1]), 'BASIS_MISMATCH')
        if Decimal(str(prior['volume'])) != current['volume']:
            conflicts.append(prior['trade_date'])
    additions = []
    for raw_row, numbers in zip(rows[len(old):], values[len(old):]):
        additions.append(dict(trade_date=raw_row['datetime'],
                              **{f: int(numbers[f]) if f == 'volume' else float(numbers[f]) for f in FIELDS}))
    return additions, conflicts


def prepare_candidate(base, symbol, raw, profile, lineage, blob_pins):
    request = validate_profile(symbol, profile)
    return prepare_qualified_candidate(base, symbol, raw, request, lineage, blob_pins, synthetic_scope(symbol,blob_pins))


def prepare_qualified_candidate(base, symbol, raw, request, lineage, blob_pins, scope, *, symbols=SYMBOLS):
    """Pure construction only. Official caller MUST first qualify pinned profile.

    This function grants no provider identity, adjusted-basis or execution authority.
    Its request must already be independently approved by the official runner.
    """
    require(symbol in symbols and type(request) is dict and set(request) == REQUEST_KEYS)
    scope=validate_scope(scope,symbol)
    require(request['symbol'] == symbol and request['start_date'] == scope['first_date'] and
            request['end_date'] == scope['addition_dates'][-1] and type(request['outputsize']) is int and
            request['outputsize'] == scope['old_count']+len(scope['addition_dates']) and request['interval'] == '1day' and
            request['order'] == 'asc' and request['adjust'] == 'splits' and
            type(request['dp']) is int and request['dp'] == -1 and
            request['provider'] == 'twelve_data' and request['currency'] == 'USD' and
            request['type'] == 'ETF')
    old = validate_base(base, symbol, blob_pins, scope, symbols=symbols)
    additions, conflicts = inspect_raw(raw, request, old, symbol, scope)
    prefix = series_prefix(symbol, symbols=symbols)
    original_index = strict_load(base[prefix + '_index.json'])
    names=series_names(base,symbol, symbols=symbols)
    sealed=[m['path'] for m in original_index['shards'][:-1]]
    tail=original_index['shards'][-1]['path']
    with tempfile.TemporaryDirectory(prefix='eleven-candidate-') as directory:
        root = Path(directory)
        for name in names:
            target = root / prefix / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base[prefix + name])
        store.append_daily_bars(root / PREFIX, provider='twelve_data_basic', symbol=symbol,
                                asset_class='etf', series_semantics='daily_regular_ohlcv',
                                bars=additions, lineage=lineage)
        after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    require(set(after) == {prefix + n for n in names})
    require(encode(read_series(after, symbol, symbols=symbols)) == encode(old + additions))
    index = strict_load(after[prefix + '_index.json'])
    mutable = {'record_count', 'last_trade_date', 'shards', 'lineage_segments'}
    require({k: v for k, v in index.items() if k not in mutable} ==
            {k: v for k, v in original_index.items() if k not in mutable})
    require(index['lineage_segments'] == original_index['lineage_segments'] +
            [dict(lineage, start=len(old), end=len(old)+len(additions)-1)])
    require(index['shards'][:-1] == original_index['shards'][:-1])
    require(all(after[prefix + n] == base[prefix + n] for n in sealed))
    changes = {p: b for p, b in after.items() if b != base[p]}
    require(set(changes) == {prefix + '_index.json', prefix + tail})
    return changes, conflicts


def snapshot(files):
    return sha(encode({p: sha(raw) for p, raw in sorted(files.items())}))


def next_budget(raw, execution_day, minute):
    """Pure synthetic accounting model, NOT the official remote debit guard."""
    try:
        before = strict_load(raw)
        require(type(before) is dict and before.get('execution_day') == execution_day)
        credits = before.get('credits')
        minutes = before.get('minutes')
        require(type(credits) is int and 0 <= credits < 200 and type(minutes) is dict)
        require(type(minute) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z', minute))
        for key, value in minutes.items():
            require(type(key) is str and type(value) is int and 0 <= value <= 8)
            stamp = datetime.strptime(key, '%Y-%m-%dT%H:%MZ').replace(tzinfo=ZoneInfo('UTC'))
            require(stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat() == execution_day)
        stamp = datetime.strptime(minute, '%Y-%m-%dT%H:%MZ').replace(tzinfo=ZoneInfo('UTC'))
        require(stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat() == execution_day)
        require(sum(minutes.values()) <= credits and minutes.get(minute, 0) < 8)
        result = copy.deepcopy(before)
        result['credits'] += 1
        result['minutes'][minute] = minutes.get(minute, 0) + 1
        return encode(result)
    except Exception:
        raise RunStopped('LOCAL_RUN_STOPPED') from None


def run_local_acceptance(adapter, profiles):
    """One local synthetic simulation; never reusable as a production dispatcher.

The executable adapter is intentionally restricted to its concrete local fixture
class. Future official integration needs a separately reviewed entrypoint, not a
boolean override here. Only validation rejections are isolated, never I/O errors.
"""
    from scripts.simulate_eleven_symbol_append import LocalSyntheticAdapter
    try:
        require(type(adapter) is LocalSyntheticAdapter and not adapter.used)
        adapter.used = True  # Consume even failed preflight; no replay.
        require(set(profiles) == set(SYMBOLS) and set(adapter.pins) == set(SYMBOLS))
        frozen = copy.deepcopy(profiles)
        frozen_pins = copy.deepcopy(adapter.pins)
        initial_files = dict(adapter.files)
        destination = adapter.root
        execution_day = adapter.execution_day
        minutes = tuple(adapter.minutes)
        require(len(minutes) == 11 and execution_day > DATES[-1])
        budget_start = strict_load(initial_files[adapter.BUDGET])
        require(type(budget_start.get('credits')) is int and budget_start['credits'] + 11 <= 200)
        def guard(expected):
            require(adapter.compute_sha == SYNTHETIC_COMPUTE and adapter.root == destination and
                    adapter.execution_day == execution_day and tuple(adapter.minutes) == minutes)
            require(adapter.files == expected and adapter.read_snapshot() == expected)
        for symbol in SYMBOLS:
            validate_profile(symbol, frozen[symbol])
            validate_base(adapter.files, symbol, frozen_pins[symbol])
        outcomes = []
        with adapter.lock('market-data-collector'):
            guard(initial_files)
            expected_state = initial_files
            for symbol in SYMBOLS:
                guard(expected_state)
                validate_base(adapter.files, symbol, frozen_pins[symbol])
                minute = minutes[len(outcomes)]
                if outcomes:
                    require(minute >= minutes[len(outcomes) - 1])
                expected_budget = next_budget(adapter.files[adapter.BUDGET], execution_day, minute)
                request = frozen[symbol]['request']
                prior = dict(adapter.files)
                before = snapshot(prior)
                claim = dict(symbol=symbol, request=request, before=before,
                             compute=SYNTHETIC_COMPUTE, reviewed_store=SYNTHETIC_STORE,
                             evidence_destination=str(adapter.root / symbol), execution_day=execution_day,
                             historical_cutoff=DATES[-1], minute=minute, synthetic_only=True)
                adapter.claim(symbol, encode(claim))
                debit = adapter.debit(symbol, minute)
                expected_debit_files = dict(prior, **{adapter.BUDGET: expected_budget})
                require(debit == dict(symbol=symbol, credits=budget_start['credits'] + len(outcomes) + 1,
                                      execution_day=execution_day, minute=minute, charged=1,
                                      budget_sha256=sha(expected_budget), prior=before,
                                      snapshot=snapshot(expected_debit_files)))
                guard(expected_debit_files)
                debit_base = dict(adapter.files)
                raw = adapter.request_once(symbol)
                retention = adapter.retain(symbol, raw, claim, debit)
                require(retention == dict(symbol=symbol, raw_sha256=sha(raw), request_sha256=sha(encode(request)),
                                         debit_sha256=sha(encode(debit)), object=str(adapter.root / symbol / 'response.raw'),
                                         local_only=True))
                require(adapter.read_raw(symbol) == raw)
                guard(debit_base)
                # Catch only this pure validation step, never capture/IO/publication.
                try:
                    changes, conflicts = prepare_candidate(debit_base, symbol, raw, frozen[symbol],
                        dict(ingest_kind='synthetic_bounded_append', raw_sha256=sha(raw),
                             request_sha256=sha(encode(request)), debit_sha256=sha(encode(debit))),
                        frozen_pins[symbol])
                except ValidationRejected as error:
                    require(str(error) in ISOLATABLE)
                    outcome = dict(symbol=symbol, status='REJECTED', reason=str(error), new_rows=0)
                    expected_state = debit_base
                    terminal = dict(outcome=outcome, retention=retention, debit=debit, publication=None)
                else:
                    expected = dict(debit_base, **changes)
                    guard(debit_base)
                    require(adapter.read_raw(symbol) == raw)
                    publication = adapter.publish(symbol, changes)
                    require(publication == dict(parent=snapshot(debit_base), snapshot=snapshot(expected),
                                                outputs={p: sha(b) for p, b in sorted(changes.items())}))
                    guard(expected)
                    expected_state = expected
                    outcome = dict(symbol=symbol, status='APPENDED', new_rows=len(dates_for(symbol)),
                                   volume_conflicts=conflicts)
                    terminal = dict(outcome=outcome, retention=retention, debit=debit, publication=publication)
                guard(expected_state)
                require(adapter.terminal(symbol, encode(terminal)) == sha(encode(terminal)))
                require(adapter.read_terminal(symbol) == encode(terminal))
                guard(expected_state)
                outcomes.append(outcome)
        return dict(status='LOCAL_SYNTHETIC_COMPLETE', production_ready=False, hosted_durability=False,
                    execution_day=execution_day, historical_cutoff=DATES[-1],
                    requests=len(outcomes), credits=len(outcomes), outcomes=outcomes)
    except Exception:
        raise RunStopped('LOCAL_RUN_STOPPED') from None
