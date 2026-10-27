"""Resolve bounded dynamic-history repair targets from immutable request and Store proof."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

HEX40 = re.compile(r'^[0-9a-f]{40}$')
SYMBOL = re.compile(r'^[A-Z][A-Z0-9.-]{0,9}$')
REQUEST_PATH = 'requests/dynamic-candidate-request.json'
STORE_PREFIX = 'data/market-data/'


class DynamicTargetError(ValueError):
    pass


def parse_dynamic_selection(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        def unique_object(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError
                out[key] = value
            return out
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise DynamicTargetError('dynamic target proof invalid') from None


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.pop('TWELVE_DATA_API_KEY', None)
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, env=env, timeout=90)
    if result.returncode:
        raise DynamicTargetError('dynamic source identity unavailable')
    return result.stdout.strip()


def _exact_file(repo: Path, commit: str, path: str, blob: str) -> bytes:
    if not HEX40.fullmatch(blob) or _git(repo, 'rev-parse', f'{commit}:{path}') != blob:
        raise DynamicTargetError('dynamic object identity mismatch')
    p = repo / path
    if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(repo.resolve()):
        raise DynamicTargetError('dynamic object path invalid')
    for part in (p, *p.parents):
        if part == repo:
            break
        if part.is_symlink():
            raise DynamicTargetError('dynamic object path invalid')
    raw = p.read_bytes()
    import hashlib
    if hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != blob:
        raise DynamicTargetError('dynamic object bytes mismatch')
    return raw


def resolve_dynamic_targets(h, *, store_repo: Path, store_sha: str, discovery_repo: Path,
                            selector: dict[str, Any], universe: dict, day: str) -> list[dict]:
    """Require exact request/result/index provenance before any budget or provider operation."""
    try:
        if not isinstance(selector, dict) or set(selector) != {'request_commit_sha', 'request_blob_sha', 'result_blob_sha', 'targets'}:
            raise ValueError
        request_commit = selector['request_commit_sha']
        if not isinstance(request_commit, str) or not HEX40.fullmatch(request_commit) or not HEX40.fullmatch(store_sha):
            raise ValueError
        if _git(store_repo, 'rev-parse', 'HEAD') != store_sha:
            raise ValueError
        _git(discovery_repo, 'merge-base', '--is-ancestor', request_commit, 'refs/remotes/origin/discovery-requests')
        request_blob = selector['request_blob_sha']
        if not isinstance(request_blob, str) or not HEX40.fullmatch(request_blob) or _git(discovery_repo, 'rev-parse', f'{request_commit}:{REQUEST_PATH}') != request_blob:
            raise ValueError
        env = dict(os.environ); env.pop('TWELVE_DATA_API_KEY', None)
        raw = subprocess.check_output(['git', '-C', str(discovery_repo), 'show', f'{request_commit}:{REQUEST_PATH}'], env=env, stderr=subprocess.PIPE, timeout=90)
        import hashlib
        if hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != request_blob:
            raise ValueError
        with tempfile.TemporaryDirectory(prefix='dynamic-request-proof-') as temp:
            path = Path(temp) / 'request.json'; path.write_bytes(raw)
            import collect_dynamic_candidates
            request = collect_dynamic_candidates._load_request(path)
        request_id = request.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', request_id):
            raise ValueError
        introductions = _git(discovery_repo, 'log', '--first-parent', '--format=%H',
                             '-S', request_id, 'refs/remotes/origin/discovery-requests', '--', REQUEST_PATH).splitlines()
        if not introductions or introductions[-1] != request_commit:
            raise ValueError
        recovery_day = day
        day = request['trade_date']; stage = request['stage']
        if date.fromisoformat(day).isoformat() != day or date.fromisoformat(day) >= date.fromisoformat(recovery_day):
            raise ValueError
        result_path = f'{STORE_PREFIX}collector-state/{day[:7]}/{day}-dynamic-candidate-{stage}.json'
        result = json.loads(_exact_file(store_repo, store_sha, result_path, selector['result_blob_sha']))
        result_commit = _git(store_repo, 'log', '--first-parent', '-n', '1', '--format=%H', store_sha, '--', result_path)
        if not HEX40.fullmatch(result_commit) or _git(store_repo, 'rev-parse', f'{result_commit}:{result_path}') != selector['result_blob_sha']:
            raise ValueError
        for key in ('request_id', 'request_purpose', 'research_repository_commit_sha', 'market_data_contract_sha', 'candidate_symbols'):
            if result.get(key) != request.get(key):
                raise ValueError
        if (result.get('mode') != f'discovery_{stage}' or result.get('snapshot_stage') != f'discovery_{stage}'
                or result.get('persistent_collection_membership') is not False):
            raise ValueError
        targets = selector['targets']
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise ValueError
        fixed = {s for s, _ in h.collection.daily_universe(universe)}
        failures = result.get('daily_history_failures')
        available = result.get('daily_history_available')
        if (not isinstance(available, list) or any(not isinstance(x, str) for x in available)
                or len(set(available)) != len(available)):
            raise ValueError
        if not isinstance(failures, list):
            raise ValueError
        resolved = []
        for target in targets:
            if not isinstance(target, dict) or set(target) != {'symbol', 'index_blob_sha'}:
                raise ValueError
            symbol = target['symbol']
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol in fixed or symbol not in request['candidate_symbols']:
                raise ValueError
            matching = [x for x in failures if isinstance(x, dict) and x.get('symbol') == symbol]
            if len(matching) != 1 or symbol in available:
                raise ValueError
            match = re.fullmatch(rf'MarketDataConflict:conflicting bar for {re.escape(symbol)} (\d{{4}}-\d{{2}}-\d{{2}})', str(matching[0].get('reason') or ''))
            if not match:
                raise ValueError
            idx_path = f'{STORE_PREFIX}series/daily/twelve_data_basic/{symbol}/_index.json'
            index = json.loads(_exact_file(store_repo, store_sha, idx_path, target['index_blob_sha']))
            if _git(store_repo, 'rev-parse', f"{result_commit}:{idx_path}") != target['index_blob_sha']:
                raise ValueError
            expected = {'provider':'twelve_data_basic', 'symbol':symbol, 'asset_class':'stocks',
                        'session':'regular', 'interval':'1d', 'series_semantics':'daily_regular_ohlcv',
                        'adjustment_semantics':'provider_reported', 'series_id':f'twelve_data_basic:{symbol}:regular:1d'}
            if any(index.get(k) != v for k, v in expected.items()):
                raise ValueError
            effective = index.get('identity_effective_from')
            if effective is not None and (not isinstance(effective, str) or date.fromisoformat(effective).isoformat() != effective):
                raise ValueError
            shards = index.get('shards')
            if not isinstance(shards, list) or not shards:
                raise ValueError
            names, cursor = set(), 0
            for shard in shards:
                name = shard.get('path')
                if not isinstance(name, str) or not re.fullmatch(r'\d{6}\.json', name) or name in names:
                    raise ValueError
                if shard.get('start') != cursor or type(shard.get('count')) is not int or shard['count'] <= 0 or shard.get('end') != cursor + shard['count'] - 1:
                    raise ValueError
                names.add(name); cursor += shard['count']
                shard_path = f'{STORE_PREFIX}series/daily/twelve_data_basic/{symbol}/{name}'
                _exact_file(store_repo, store_sha, shard_path, shard['blob_sha'])
                if _git(store_repo, 'rev-parse', f"{result_commit}:{shard_path}") != shard['blob_sha']:
                    raise ValueError
            if cursor != index.get('record_count'):
                raise ValueError
            directory = store_repo / f'{STORE_PREFIX}series/daily/twelve_data_basic/{symbol}'
            children = list(directory.rglob('*'))
            if any(p.is_symlink() for p in children) or {p.name for p in children if p.is_file()} != names | {'_index.json'} or any(p.is_dir() for p in children):
                raise ValueError
            rows = h.store.read_daily_series(store_repo / STORE_PREFIX, provider='twelve_data_basic', symbol=symbol)
            if (not rows or index.get('first_trade_date') != rows[0]['trade_date'] or index.get('last_trade_date') != rows[-1]['trade_date']
                    or match.group(1) not in {row['trade_date'] for row in rows[-3:]}
                    or (effective is not None and rows[0]['trade_date'] < effective)):
                raise ValueError
            resolved.append({'symbol':symbol, 'asset_class':'stocks', 'effective':effective,
                             'source_result_commit_sha':result_commit})
        if len({x['symbol'] for x in resolved}) != len(resolved):
            raise ValueError
        return resolved
    except Exception:
        raise DynamicTargetError('dynamic target proof invalid') from None
