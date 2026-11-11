#!/usr/bin/env python3
"""Executable SYNTHETIC-ONLY acceptance flow; never reads production/private data.

Usage: python -m scripts.simulate_eleven_symbol_append --output /new/local/path
Creates a local fixture, evidence and snapshots. No network or Git commands.
Local fsync/readback is not hosted durability, authorization or an official lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, timedelta
import fcntl
import json
import os
from pathlib import Path
import tempfile

from scripts import eleven_symbol_append as a
from scripts import market_data_store as store


def create_file(path, raw):
    """Create once, sync, and read back; exceptions are never retried."""
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    with path.open('rb') as stream:
        a.require(stream.read() == raw)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fixture_inputs():
    """Produce synthetic OHLCV, not a US exchange-calendar or provider assertion."""
    days = []
    cursor = date.fromisoformat(a.START)
    end = date(2024, 8, 30)
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    # Keep exactly 260 synthetic sessions and both endpoints. This fixture is
    # deliberately not evidence of actual market holidays/calendar completeness.
    days = [days[0]] + days[-259:]
    profiles, responses, pins = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix='eleven-fixture-') as directory:
        root = Path(directory)
        for symbol in a.SYMBOLS:
            old_dates = days + (['2024-09-02'] if symbol == 'XLK' else [])
            rows = [dict(trade_date=day, open=100, high=102, low=99, close=101, volume=1000)
                    for day in old_dates]
            store.append_daily_bars(root / a.PREFIX, provider='twelve_data_basic', symbol=symbol,
                                    asset_class='etf', series_semantics='daily_regular_ohlcv', bars=rows,
                                    lineage={'ingest_kind': 'synthetic_fixture'})
            index_path = root / a.series_prefix(symbol) / '_index.json'
            index = json.loads(index_path.read_bytes())
            index['baseline_initialization'] = {'status': 'accepted', 'synthetic_only': True}
            index_path.write_bytes(a.encode(index))
            request = dict(provider='twelve_data', symbol=symbol, interval='1day', exchange='SYNTHETIC',
                           mic_code='TEST', currency='USD', type='ETF', adjust='splits', dp=-1,
                           start_date=a.START, end_date=a.DATES[-1], outputsize=265, order='asc')
            profiles[symbol] = dict(request=request, basis='SYNTHETIC_SPLITS_COMPATIBLE')
            meta = {k: request[k] for k in ('symbol', 'interval', 'currency', 'exchange', 'mic_code', 'type')}
            meta['exchange_timezone'] = 'America/New_York'
            values = [dict(datetime=day, open='100', high='102', low='99', close='101', volume='1000')
                      for day in old_dates + list(a.dates_for(symbol))]
            responses[symbol] = a.encode(dict(meta=meta, status='ok', values=values))
            pins[symbol] = {n: store.git_blob_sha_bytes((root / a.series_prefix(symbol) / n).read_bytes())
                            for n in a.FILENAMES}
        files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    files['unrelated.txt'] = b'Synthetic unrelated object: must remain unchanged.\n'
    files[LocalSyntheticAdapter.BUDGET] = a.encode({'execution_day': '2026-11-11', 'credits': 0, 'minutes': {}})
    return files, profiles, responses, pins


class LocalSyntheticAdapter:
    """Real local file IO around an intentionally synthetic request/Store model.

Snapshots are SHA256-addressed local directories, NOT Git commits or remote
publications. Fault hooks model unknown completion; they do not recover/resume.
"""
    BUDGET = a.PREFIX + 'collector-state/synthetic-budget.json'

    def __init__(self, root, files, responses, pins):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.files = dict(files)
        self.responses = dict(responses)
        self.pins = {s: dict(p) for s, p in pins.items()}
        self.events = []
        self.compute_sha = a.SYNTHETIC_COMPUTE
        self.execution_day = '2026-11-11'
        self.minutes = tuple('2026-11-11T13:%02dZ' % (i // 8) for i in range(11))
        self.used = False
        self.fail_at = None
        self.corrupt_raw_readback = False
        self.corrupt_debit = False
        self.corrupt_publication = False
        self.claimed = set()
        self.requested = set()
        self.locked = False
        self._snapshot_dir = None
        (self.root / 'snapshots').mkdir(mode=0o700)
        self._save_snapshot()
        create_file(self.root / 'SYNTHETIC_ONLY.json', a.encode(dict(
            synthetic_only=True, production_ready=False, hosted_durability=False,
            reviewed_source_is_not_runtime_attestation=True)))

    @contextmanager
    def lock(self, group):
        a.require(group == 'market-data-collector' and not self.locked)
        with (self.root / 'local-synthetic.lock').open('xb') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
            try:
                yield
            finally:
                self.locked = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def event(self, stage, symbol):
        a.require(self.locked)
        self.events.append((stage, symbol))

    def fault(self, stage, symbol):
        if self.fail_at == (stage, symbol):
            raise OSError('synthetic unknown completion')

    def _save_snapshot(self):
        digest = a.snapshot(self.files)
        target = self.root / 'snapshots' / digest
        target.mkdir(mode=0o700, exist_ok=False)
        for path, raw in self.files.items():
            # No arbitrary escape paths even in the local fixture adapter.
            a.require(not Path(path).is_absolute() and all(x not in ('', '.', '..') for x in path.split('/')))
            dest = target / path
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            create_file(dest, raw)
        create_file(target / 'snapshot-manifest.json', a.encode({p: a.sha(b) for p, b in sorted(self.files.items())}))
        self._snapshot_dir = target

    def claim(self, symbol, raw):
        self.event('claim', symbol)
        a.require(symbol not in self.claimed)
        target = self.root / symbol
        target.mkdir(mode=0o700, exist_ok=False)
        create_file(target / 'request.json', raw)
        create_file(target / 'attempt.json', a.encode(dict(attempt=1, request_sha256=a.sha(raw))))
        self.claimed.add(symbol)
        self.fault('claim', symbol)

    def debit(self, symbol, minute):
        self.event('debit', symbol)
        prior = a.snapshot(self.files)
        a.require(symbol in self.claimed)
        self.files[self.BUDGET] = a.next_budget(self.files[self.BUDGET], self.execution_day, minute)
        credits = json.loads(self.files[self.BUDGET])['credits']
        self._save_snapshot()
        receipt = dict(symbol=symbol, credits=credits, prior=prior, snapshot=a.snapshot(self.files),
                       execution_day=self.execution_day, minute=minute, charged=1,
                       budget_sha256=a.sha(self.files[self.BUDGET]))
        create_file(self.root / symbol / 'debit.json', a.encode(receipt))
        self.fault('debit', symbol)
        if self.corrupt_debit:
            receipt['credits'] += 1
        return receipt

    def request_once(self, symbol):
        self.event('request', symbol)
        a.require(symbol in self.claimed and symbol not in self.requested and len(self.requested) < 11)
        self.requested.add(symbol)
        self.fault('request', symbol)
        return self.responses[symbol]  # In-memory synthetic bytes, never HTTP.

    def retain(self, symbol, raw, claim, debit):
        self.event('retain', symbol)
        target = self.root / symbol / 'response.raw'
        create_file(target, raw)
        receipt = dict(symbol=symbol, raw_sha256=a.sha(raw), request_sha256=a.sha(a.encode(claim['request'])),
                       debit_sha256=a.sha(a.encode(debit)), object=str(target), local_only=True)
        create_file(self.root / symbol / 'retention.json', a.encode(receipt))
        self.fault('retain', symbol)
        return receipt

    def read_raw(self, symbol):
        raw = (self.root / symbol / 'response.raw').read_bytes()
        return raw + b'corrupt' if self.corrupt_raw_readback else raw

    def publish(self, symbol, changes):
        self.event('publish', symbol)
        parent = a.snapshot(self.files)
        self.files.update(changes)
        self._save_snapshot()
        receipt = dict(parent=parent, snapshot=a.snapshot(self.files),
                       outputs={p: a.sha(b) for p, b in sorted(changes.items())})
        create_file(self.root / symbol / 'publication.json', a.encode(receipt))
        self.fault('publish', symbol)
        if self.corrupt_publication:
            receipt['snapshot'] = '0' * 64
        return receipt

    def read_snapshot(self):
        return {str(p.relative_to(self._snapshot_dir)): p.read_bytes() for p in self._snapshot_dir.rglob('*')
                if p.is_file() and p.name != 'snapshot-manifest.json'}

    def terminal(self, symbol, raw):
        self.event('terminal', symbol)
        create_file(self.root / symbol / 'terminal.json', raw)
        self.fault('terminal', symbol)
        return a.sha(raw)

    def read_terminal(self, symbol):
        return (self.root / symbol / 'terminal.json').read_bytes()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New local SYNTHETIC output directory')
    args = parser.parse_args(argv)
    try:
        files, profiles, responses, pins = fixture_inputs()
        adapter = LocalSyntheticAdapter(args.output, files, responses, pins)
        result = a.run_local_acceptance(adapter, profiles)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'LOCAL_RUN_STOPPED', 'production_ready': False}))
        return 1


if __name__ == '__main__':
    # Keep the deliberately exact adapter type canonical under python -m.
    from scripts.simulate_eleven_symbol_append import main as canonical_main
    raise SystemExit(canonical_main())
