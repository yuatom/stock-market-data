"""Offline-only fixed-window adapter experiment. No CLI or workflow activation.

This is NOT a runtime authorization adapter. It accepts only local bare Git
remotes and has no production request attestation or final proof publisher.
The existing attempt owner implements reservation, raw retention and terminals.
"""
import hashlib
import json
import re
from pathlib import Path
import admit_market_data_baseline as publisher
from dynamic_daily_owner import DynamicDailyOwner, encode

WINDOWS = {'open_15m': ('09:30', '09:45'), 'open_30m': ('09:45', '10:00'),
           'open_60m': ('10:00', '10:30')}


def require_local_bare(repo):
    remotes = publisher.git(repo, 'remote', 'get-url', '--all', 'origin').splitlines()
    remotes += publisher.git(repo, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()
    for remote in remotes:
        path = Path(remote)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('offline candidate requires local bare fetch and push remotes')
        if publisher.git(path, 'rev-parse', '--is-bare-repository') != 'true':
            raise ValueError('offline candidate requires local bare fetch and push remotes')
    if len(set(str(Path(p).resolve()) for p in remotes)) != 1:
        raise ValueError('offline candidate requires identical local bare fetch and push remotes')



class OfflineFixedIntradayOwner(DynamicDailyOwner):
    """Local simulation only; ambiguous triggers and retries fail closed."""
    def __init__(self, repo, expected, day, run_id, *, compute, compute_sha,
                 business_request_id, transaction_id, stage, trigger, fixed_symbols,
                 clock, deadline):
        if trigger != 'push' or stage not in WINDOWS:
            raise ValueError('trigger/stage identity not supported by offline candidate')
        for value in (business_request_id, transaction_id):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', value):
                raise ValueError('invalid explicit identity')
        if (not isinstance(fixed_symbols, list) or not fixed_symbols or
            any(not isinstance(s, str) or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,15}', s)
                for s in fixed_symbols) or len(set(fixed_symbols)) != len(fixed_symbols)):
            raise ValueError('invalid fixed symbols')
        require_local_bare(repo)
        require_local_bare(compute)
        # Transaction identity remains stable across invocation/request aliases.
        # This conservative simulation blocks all repeat attempts for that scope.
        self.request = dict(request_id=transaction_id, control_request_id=business_request_id,
                            trade_date=day, stage=stage, candidate_symbols=list(fixed_symbols),
                            offline_simulation=True)
        self.request_raw = encode(self.request)
        self.compute = compute
        publisher.StorePublisher.__init__(self, repo, expected, day, run_id,
            verify=self._verify, report_namespace='dynamic-daily-attempt')
        self.compute = compute
        self.compute_sha = compute_sha
        self.clock = clock
        self.deadline = deadline
        # No invented control commit/blob: a production adapter is not implemented.
        self.request_commit = None
        self.request_blob = None
        self.request_sha = hashlib.sha256(self.request_raw).hexdigest()
        self.intraday_scope = None
        self.allowed = {self.budget_relative}
        self.active = None
        self.reservation = 'NOT_STARTED'
        self.failed = False
        self.predebit_publish_started = False

    @property
    def mode(self):
        return 'fixed_intraday_attempt_offline'

    def attempt_namespace(self):
        return 'fixed-intraday-attempt'

    def raw_prefix(self, symbol):
        return f'collector-state/{self.day[:7]}/fixed-intraday-{symbol}-'

    def remote_head(self):
        require_local_bare(self.repo)
        require_local_bare(self.compute)
        return super().remote_head()

    def publish(self, paths, **kwargs):
        require_local_bare(self.repo)
        require_local_bare(self.compute)
        return super().publish(paths, **kwargs)

    def begin_intraday(self, *args, **kwargs):
        raise ValueError('offline owner cannot enter live collector; use simulate')

    def before_request(self):
        raise ValueError('offline owner forbids provider transport; use simulate')

    def _begin_simulation(self, trade_date, stage, start, end):
        if (self.active or trade_date != self.day or stage != self.request['stage']
                or (start, end) != WINDOWS[self.request['stage']]):
            self.abort()
        self.assert_current()
        self.intraday_outcomes = []
        self.intraday_scope = dict(provider='twelve_data_basic', stage=stage,
            trade_date=trade_date, start=start, end=end, interval='1min')

    def simulate(self, responses, *, config):
        """Consume only in-memory synthetic byte responses, never a transport.

        None models an unknown transport outcome; it does NOT perform HTTP.
        This validates attempt persistence, not live collector integration.
        """
        from dynamic_intraday_response import KnownIntradayRejection
        from market_data_collection import parse_twelve_regular, _write_regular_capture
        if (type(responses) is not dict or set(responses) != set(self.request['candidate_symbols'])
                or any(value is not None and type(value) is not bytes for value in responses.values())):
            raise ValueError('simulate requires exact symbol to bytes/None mapping')
        start, end = WINDOWS[self.request['stage']]
        self._begin_simulation(self.day, self.request['stage'], start, end)
        for symbol in self.request['candidate_symbols']:
            self.reserve(symbol, publisher.load_helpers(), config['collector']['budgets']['twelve_data_basic'])
            super().before_request()  # Existing claim checks only; no provider I/O.
            raw = responses[symbol]
            if raw is None:
                self.abort()
            relative = self.raw_prefix(symbol) + hashlib.sha256(raw).hexdigest() + '.raw'
            full = 'data/market-data/' + relative
            self.allowed.add(full)
            path = self.safe_path(full)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != raw:
                self.abort()
            path.write_bytes(raw)
            self.retain_raw(relative)
            try:
                data = json.loads(raw)
                if (not isinstance(data, dict) or data.get('status') == 'error'
                        or data.get('meta', {}).get('symbol') != symbol
                        or data.get('meta', {}).get('interval') != '1min'):
                    raise ValueError('synthetic response rejected')
                facts, _ = parse_twelve_regular(data, symbol, 'stocks', self.day, start, end)
            except (ValueError, TypeError, AttributeError):
                self.close_intraday(None, 'REJECTED')
                raise KnownIntradayRejection('retained synthetic response rejected') from None
            capture = None
            if facts:
                capture = _write_regular_capture(self.repo/'data/market-data', self.day,
                    self.request['stage'], symbol, 'twelve_data_basic', facts,
                    self.clock().isoformat(), start, end, durable_owner=self)
            self.close_intraday(capture[0] if capture else None,
                                'SELECTED_TWELVE' if capture else 'REJECTED')
        return dict(offline_simulation=True, snapshot_written=False,
                    attempt_count=len(self.intraday_outcomes))
