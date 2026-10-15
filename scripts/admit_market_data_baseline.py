#!/usr/bin/env python3
"""Manual, serialized daily revision admission; market facts are never written.

Only a target-day budget and a run-specific private diagnostic may be published.
Every request is durably pre-debited. A rejected/expired debit never permits HTTP;
an unused debit is conservatively retained, not refunded. See the mode contract.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

MODE = 'daily_baseline_admission'
ET = ZoneInfo('America/New_York')
CODES = frozenset({
    'invalid_target', 'input_not_mature', 'credential_missing', 'wrong_execution_surface',
    'object_drift', 'dirty_checkout', 'unexpected_store_change', 'unsafe_path',
    'report_exists', 'publication_failed', 'durability_failed', 'budget_exhausted',
    'budget_slot_expired', 'budget_state_invalid', 'stored_series_invalid',
    'provider_response_rejected', 'replacement_rejected', 'contract_invalid',
    'deadline_exceeded', 'internal_error',
})


class AdmissionError(RuntimeError):
    def __init__(self, code: str, *, debited: bool = False, evidence: dict | None = None):
        self.code = code if code in CODES else 'internal_error'
        self.debited = debited
        self.evidence = evidence or {}
        super().__init__(self.code)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run(argv: list[str], *, code: str = 'publication_failed') -> str:
    env = dict(os.environ)
    env.pop('TWELVE_DATA_API_KEY', None)  # Git/verification children do not need provider credentials.
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        raise AdmissionError(code) from None
    if p.returncode:
        # Never copy remote URLs, private payloads or arbitrary stderr into public evidence.
        raise AdmissionError(code)
    return p.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return _run(['git', '-C', str(repo), *args])


def verify_durability(repo: Path, sha: str) -> None:
    _run([sys.executable, str(Path(__file__).with_name('verify_store_publication.py')),
          '--store-root', str(repo), '--expected-commit', sha,
          '--writer-status', 'store_persisted'], code='durability_failed')


def validate_target(day: str, maturity: str, now: datetime) -> None:
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError
        parsed = datetime.strptime(day, '%Y-%m-%d').date()
        hour, minute = (int(x) for x in maturity.split(':'))
        local = now.astimezone(ET)
        boundary = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if parsed != local.date():
            raise ValueError
    except (ValueError, TypeError):
        raise AdmissionError('invalid_target') from None
    if local < boundary:
        raise AdmissionError('input_not_mature')


class StorePublisher:
    """Exact fast-forward budget/diagnostic writer under the collector's job lock."""
    def __init__(self, repo: Path, expected: str, day: str, request_id: str,
                 *, verify: Callable[[Path, str], None] = verify_durability):
        self.repo = repo.resolve()
        if not re.fullmatch(r'[0-9a-f]{40}', expected):
            raise AdmissionError('object_drift')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not re.fullmatch(r'\d+-\d+', request_id):
            raise AdmissionError('invalid_target')
        self.base = self.head = expected
        self.verify = verify
        prefix = f'data/market-data/collector-state/{day[:7]}/{day}'
        self.budget_relative = f'{prefix}-twelve-data-budget.json'
        self.report_relative = f'{prefix}-daily-baseline-admission-{request_id}.json'
        self.allowed = {self.budget_relative, self.report_relative}
        if git(self.repo, 'rev-parse', 'HEAD') != expected or self.remote_head() != expected:
            raise AdmissionError('object_drift')
        if git(self.repo, 'status', '--porcelain') or git(self.repo, 'ls-files', '--others'):
            raise AdmissionError('dirty_checkout')
        if self.safe_path(self.report_relative).exists():
            raise AdmissionError('report_exists')

    def remote_head(self) -> str:
        value = git(self.repo, 'ls-remote', 'origin', 'refs/heads/main').split()
        if len(value) != 2 or value[1] != 'refs/heads/main':
            raise AdmissionError('object_drift')
        return value[0]

    def safe_path(self, relative: str) -> Path:
        if relative not in self.allowed:
            raise AdmissionError('unsafe_path')
        path = self.repo / relative
        if not path.resolve().is_relative_to(self.repo):
            raise AdmissionError('unsafe_path')
        for part in (path, *path.parents):
            if part == self.repo:
                break
            if part.is_symlink():
                raise AdmissionError('unsafe_path')
        return path

    def assert_allowed(self) -> None:
        # Include deletions, staged/unstaged changes and ignored untracked files.
        changed = set(filter(None, git(self.repo, 'diff', '--name-only', '-z', self.base, '--').split('\0')))
        changed.update(filter(None, git(self.repo, 'ls-files', '--others', '-z').split('\0')))
        if not changed <= self.allowed:
            raise AdmissionError('unexpected_store_change')
        for relative in changed:
            path = self.safe_path(relative)
            if not path.is_file():
                raise AdmissionError('unexpected_store_change')

    def publish(self, paths: list[str]) -> str:
        if not paths or not set(paths) <= self.allowed:
            raise AdmissionError('unsafe_path')
        self.assert_allowed()
        if self.remote_head() != self.head or git(self.repo, 'rev-parse', 'HEAD') != self.head:
            raise AdmissionError('object_drift')
        staged = set(filter(None, git(self.repo, 'diff', '--cached', '--name-only').splitlines()))
        if not staged <= set(paths):
            raise AdmissionError('unexpected_store_change')
        for relative in paths:
            if not self.safe_path(relative).is_file():
                raise AdmissionError('unsafe_path')
        git(self.repo, 'add', '--', *paths)
        if not git(self.repo, 'diff', '--cached', '--name-only'):
            return self.head
        git(self.repo, '-c', 'user.name=stock-market-data-bot', '-c',
            'user.email=stock-market-data-bot@users.noreply.github.com',
            'commit', '--quiet', '--no-gpg-sign', '-m', 'data: record daily admission budget or diagnostic')
        candidate = git(self.repo, 'rev-parse', 'HEAD')
        # No pull/rebase/force retry: a concurrent writer invalidates this operation.
        git(self.repo, 'push', 'origin', f'{candidate}:refs/heads/main')
        if self.remote_head() != candidate:
            raise AdmissionError('object_drift')
        self.verify(self.repo, candidate)
        self.head = candidate
        return candidate

    def report(self, value: dict[str, Any]) -> str:
        path = self.safe_path(self.report_relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                   sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
        return self.publish([self.report_relative])


def load_helpers():
    import collection_universe as collection
    import market_data_collectors as collectors
    import market_data_store as store
    import rebase_market_data_baseline as rebaser
    return SimpleNamespace(collection=collection, collectors=collectors, store=store, rebaser=rebaser)


def prepare_series(h, root: Path, universe: dict, config: dict, day: str) -> list[dict]:
    target = max(int(x) for x in config['series']['analysis_windows_may_read_last_sessions'])
    candidates = []
    for symbol, _asset in h.collection.daily_universe(universe):
        try:
            path = h.rebaser._series_index_path(root, h.collectors.TWELVE_PROVIDER, symbol)
            index = h.rebaser._read_json(path)
            effective = h.collection.ticker_effective_at(universe, symbol)
            expected = {'provider': h.collectors.TWELVE_PROVIDER, 'symbol': symbol,
                        'session': 'regular', 'interval': '1d', 'series_semantics': 'daily_regular_ohlcv',
                        'adjustment_semantics': 'provider_reported', 'identity_effective_from': effective}
            if any(index.get(k) != v for k, v in expected.items()):
                raise ValueError
            existing = h.store.read_daily_series(root, provider=h.collectors.TWELVE_PROVIDER, symbol=symbol)
            outputsize = h.rebaser._replacement_history_outputsize(existing, trade_date=day, target_records=target)
            candidates.append({'symbol': symbol, 'existing': existing, 'effective': effective,
                               'outputsize': outputsize, 'target_records': target})
        except Exception:
            raise AdmissionError('stored_series_invalid') from None
    if not candidates or len({x['symbol'] for x in candidates}) != len(candidates):
        raise AdmissionError('contract_invalid')
    return candidates


def compare_replacement(h, candidate: dict, fetched: list[dict], day: str, limits: dict) -> dict:
    """Same admission rules as rebase, with no archive or replacement call."""
    existing, effective, target = candidate['existing'], candidate['effective'], candidate['target_records']
    evidence: dict[str, Any] = {}
    try:
        rows = [h.rebaser._validated_replacement_bar(x) for x in fetched]
        rows = sorted((x for x in rows if x['trade_date'] < day and
                       (effective is None or x['trade_date'] >= effective)), key=lambda x: x['trade_date'])
        if not rows or (len(rows) < target and not effective):
            raise ValueError
        first_required = min(existing[0]['trade_date'], rows[-target:][0]['trade_date'])
        rows = [x for x in rows if x['trade_date'] >= first_required]
        by_date = {x['trade_date']: x for x in rows}
        evidence['missing_existing_dates'] = [x['trade_date'] for x in existing if x['trade_date'] not in by_date]
        evidence['overlap_differences'] = [
            {'trade_date': x['trade_date'], 'changed_fields': h.rebaser._field_diff(x, by_date[x['trade_date']])}
            for x in existing if x['trade_date'] in by_date and x != by_date[x['trade_date']]]
        revisions = h.rebaser.validate_replacement(existing, rows, **limits)
        old_dates = {x['trade_date'] for x in existing}
        return {'symbol': candidate['symbol'], 'status': 'PASS', 'revisions': revisions,
                'existing_records': len(existing), 'replacement_records': len(rows),
                'new_records': sum(x['trade_date'] not in old_dates for x in rows),
                'last_trade_date': rows[-1]['trade_date'],
                'latest_date_advanced': rows[-1]['trade_date'] > existing[-1]['trade_date'],
                'replacement_sha256': h.store.json_sha256(rows), 'history_outputsize': candidate['outputsize']}
    except Exception:
        raise AdmissionError('replacement_rejected', evidence=evidence) from None



def validate_budget_file(path: Path, day: str, per_day: int, per_minute: int) -> None:
    """Absent ledger is valid only inside the fresh serialized writer context."""
    if not path.exists():
        return
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        credits, minutes = state['credits'], state['minutes']
        if (state.get('trade_date') != day or type(credits) is not int or
            not 0 <= credits <= per_day or not isinstance(minutes, dict)):
            raise ValueError
        for stamp, value in minutes.items():
            if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z', stamp) or
                type(value) is not int or not 0 <= value <= per_minute):
                raise ValueError
            datetime.strptime(stamp, '%Y-%m-%dT%H:%MZ')
    except (ValueError, TypeError, KeyError, OSError):
        raise AdmissionError('budget_state_invalid') from None


def debit_before_request(h, writer, root: Path, day: str, budget: dict,
                         *, clock: Callable[[], datetime] = utc_now) -> None:
    if clock().astimezone(ET).date().isoformat() != day:
        raise AdmissionError('invalid_target')
    kwargs = {'per_day': int(budget['hard_credits_per_day']), 'per_minute': int(budget['hard_credits_per_minute'])}
    if hasattr(writer, 'safe_path'):
        path = writer.safe_path(writer.budget_relative)
        validate_budget_file(path, day, **kwargs)
    h.collectors.wait_for_twelve_budget(root, day, **kwargs)
    stamp = clock()
    if stamp.second >= 55:
        time.sleep(61 - stamp.second)
        stamp = clock()
    if stamp.astimezone(ET).date().isoformat() != day:
        raise AdmissionError('invalid_target')
    # Check the real path before the existing helper can follow a symlink.
    if hasattr(writer, 'safe_path'):
        writer.safe_path(writer.budget_relative)
    h.collectors.consume_twelve_credit(root, day, amount=1, **kwargs)
    writer.publish([writer.budget_relative])
    after = clock()
    if after.strftime('%Y-%m-%dT%H:%MZ') != stamp.strftime('%Y-%m-%dT%H:%MZ') or after.second >= 58:
        raise AdmissionError('budget_slot_expired', debited=True)


def public_summary(report: dict[str, Any]) -> dict[str, Any]:
    safe = {k: report[k] for k in ('status', 'compute_sha', 'store_start_sha', 'request_id',
            'requests_attempted', 'budget_debits', 'target_count') if k in report}
    safe['mode'] = MODE
    safe['admitted_count'] = len(report.get('results', []))
    safe['failure_count'] = len(report.get('failures', []))
    safe['failure_codes'] = sorted({x.get('code') if x.get('code') in CODES else 'internal_error'
                                   for x in report.get('failures', [])})
    return safe


def execute(h, writer, *, compute: Path, compute_sha: str, day: str, request_id: str,
            config: dict, universe: dict, access: dict, key: str,
            clock: Callable[[], datetime] = utc_now) -> tuple[dict, str]:
    maturity = config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
    validate_target(day, maturity, clock())
    if not key:
        raise AdmissionError('credential_missing')
    root = writer.repo / 'data/market-data'
    candidates = prepare_series(h, root, universe, config, day)
    budget = config['collector']['budgets']['twelve_data_basic']
    limits = {k: int(config['series']['baseline_initialization']['explicit_rebase'][k]) for k in (
        'maximum_revised_overlap_records', 'revised_overlap_must_be_within_last_existing_sessions')}
    report = {'schema_version': 1, 'market_fact_authority': False, 'mode': MODE,
              'status': 'IN_PROGRESS', 'compute_sha': compute_sha, 'store_start_sha': writer.base,
              'trade_date': day, 'request_id': request_id, 'started_at': clock().isoformat(),
              'target_count': len(candidates), 'requests_attempted': 0, 'budget_debits': 0,
              'results': [], 'failures': [], 'production_recovery_accepted': False}
    writer.report(report)
    started = time.monotonic()
    for candidate in candidates:
        try:
            validate_target(day, maturity, clock())
            if time.monotonic() - started > 600:
                raise AdmissionError('deadline_exceeded')
            if git(compute, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != compute_sha:
                raise AdmissionError('object_drift')
            debit_before_request(h, writer, root, day, budget, clock=clock)
            report['budget_debits'] += 1
        except Exception as exc:
            code = exc.code if isinstance(exc, AdmissionError) else (
                'budget_exhausted' if isinstance(exc, h.collectors.BudgetExceeded) else 'budget_state_invalid')
            if isinstance(exc, AdmissionError) and exc.debited:
                report['budget_debits'] += 1
            report['failures'].append({'symbol': candidate['symbol'], 'code': code})
            report['status'] = 'BLOCKED'
            break
        report['requests_attempted'] += 1
        try:
            fetched = h.rebaser._fetch_replacement_daily(candidate['symbol'], key, access,
                outputsize=candidate['outputsize'], trade_date=day, effective=candidate['effective'])
            report['results'].append(compare_replacement(h, candidate, fetched, day, limits))
        except Exception as exc:
            code = exc.code if isinstance(exc, AdmissionError) else 'provider_response_rejected'
            failure = {'symbol': candidate['symbol'], 'code': code}
            if isinstance(exc, AdmissionError):
                failure['evidence'] = exc.evidence
            report['failures'].append(failure)
    if report['status'] != 'BLOCKED':
        report['status'] = 'PASS' if not report['failures'] and len(report['results']) == len(candidates) else 'FAIL'
    report['finished_at'] = clock().isoformat()
    report['budget_head_before_report'] = writer.head
    writer.assert_allowed()
    return report, writer.report(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trade-date', required=True)
    parser.add_argument('--compute-sha', required=True)
    parser.add_argument('--store-sha', required=True)
    parser.add_argument('--store-repo', required=True)
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REPOSITORY') != 'yuatom/stock-market-data' or
            os.environ.get('GITHUB_REF') != 'refs/heads/main'):
            raise AdmissionError('wrong_execution_surface')
        if not re.fullmatch(r'[0-9a-f]{40}', args.compute_sha) or not re.fullmatch(r'[0-9a-f]{40}', args.store_sha):
            raise AdmissionError('object_drift')
        compute = Path(__file__).resolve().parents[1]
        if git(compute, 'rev-parse', 'HEAD') != args.compute_sha or git(compute, 'status', '--porcelain'):
            raise AdmissionError('object_drift')
        key = os.environ.get('TWELVE_DATA_API_KEY', '')
        if not key:
            raise AdmissionError('credential_missing')
        h = load_helpers()
        mode_contract = h.collectors.load_yaml(compute / 'config/daily-baseline-admission.yaml')
        if (mode_contract.get('mode') != MODE or mode_contract.get('market_facts_read_only') is not True or
            mode_contract.get('durable_predebit_required') is not True):
            raise AdmissionError('contract_invalid')
        config = h.collectors.load_yaml(compute / 'config/market-data-store.yaml')
        validate_target(args.trade_date, config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et'], utc_now())
        writer = StorePublisher(Path(args.store_repo), args.store_sha, args.trade_date, args.request_id)
        report, sha = execute(h, writer, compute=compute, compute_sha=args.compute_sha,
            day=args.trade_date, request_id=args.request_id, config=config,
            universe=h.collection.load_collection_universe(compute / 'config/collection-universe.json'),
            access=h.collectors.load_yaml(compute / 'config/market-data-collector-access.yaml'), key=key)
        summary = public_summary(report)
        summary.update(store_commit_sha=sha, result_state_path=writer.report_relative,
                       result_blob_sha=git(writer.repo, 'rev-parse', f'{sha}:{writer.report_relative}'))
        print(json.dumps(summary, sort_keys=True))
        return 0 if report['status'] == 'PASS' else 2
    except Exception as exc:
        code = exc.code if isinstance(exc, AdmissionError) else 'internal_error'
        print(json.dumps({'mode': MODE, 'status': 'BLOCKED', 'failure_codes': [code]}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
