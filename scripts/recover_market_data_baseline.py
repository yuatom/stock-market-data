#!/usr/bin/env python3
"""Guard the existing manual rebase: qualify all, stage all, publish one fact commit.

The budget checkout never receives rebuilt histories. The disposable worktree is
not published until every replacement, archive, shard and declared output passes.
A lost push/verification acknowledgement is UNKNOWN, never an automatic retry.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dynamic_baseline_targets import parse_dynamic_selection
from typing import Any

MODE = 'daily_baseline_rebase'
PREFIX = 'data/market-data/'
INVENTORY = PREFIX + 'collector-state/market-data-inventory.json'
SAFE_CODES = frozenset({'incomplete_targets', 'unsafe_path', 'archive_exists',
    'original_invalid', 'replacement_rejected', 'candidate_invalid', 'unexpected_change',
    'object_drift', 'publication_unconfirmed', 'closure_invalid', 'credential_missing',
    'wrong_execution_surface', 'invalid_target', 'input_not_mature', 'budget_exhausted',
    'budget_slot_expired', 'budget_state_invalid', 'deadline_exceeded', 'internal_error'})


class RecoveryError(RuntimeError):
    def __init__(self, code: str, *, candidate_sha: str | None = None):
        self.code = code if code in SAFE_CODES else 'internal_error'
        self.candidate_sha = candidate_sha
        super().__init__(self.code)


def admission():
    return importlib.import_module('admit_market_data_baseline')


def now_utc():
    return datetime.now(timezone.utc)


def require_complete(targets: list[str], entries: list[dict]) -> None:
    actual = [x.get('symbol') for x in entries]
    if (not targets or len(targets) != len(set(targets)) or
        len(actual) != len(set(actual)) or set(actual) != set(targets)):
        raise RecoveryError('incomplete_targets')


def safe_path(repo: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative.startswith(PREFIX) or
        '..' in Path(relative).parts or Path(relative).is_absolute()):
        raise RecoveryError('unsafe_path')
    path = repo / relative
    if not path.resolve().is_relative_to(repo.resolve()):
        raise RecoveryError('unsafe_path')
    for part in (path, *path.parents):
        if part == repo:
            break
        if part.is_symlink():
            raise RecoveryError('unsafe_path')
    return path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(',', ':')) + '\n'
    path.write_text(payload, encoding='utf-8')


def validate_manifest(value: dict, outputs: list[dict], request_id: str, day: str) -> None:
    if not outputs or len({x['path'] for x in outputs}) != len(outputs):
        raise RecoveryError('closure_invalid')
    required = []
    for item in outputs:
        path, blob = item['path'], item['blob_sha']
        if not path.startswith(PREFIX) or not re.fullmatch(r'[0-9a-f]{40}', blob):
            raise RecoveryError('closure_invalid')
        required.append({'path': path[len(PREFIX):], 'blob_sha': blob})
    if (value.get('schema_version') != 1 or value.get('request_id') != request_id or
        value.get('trade_date') != day or value.get('stage') != MODE or
        value.get('required_outputs') != required):
        raise RecoveryError('closure_invalid')


def verify_outputs(repo: Path, sha: str, manifest: str, outputs: list[dict],
                   request_id: str, day: str) -> dict:
    a = admission()
    try:
        receipt = json.loads(a._run([sys.executable,
            str(Path(__file__).with_name('verify_store_publication.py')),
            '--store-root', str(repo), '--expected-commit', sha,
            '--writer-status', 'store_persisted', '--required-manifest', manifest]))
        if (receipt.get('status') != 'durability_verified' or
            receipt.get('store_commit_sha') != sha or receipt.get('remote_main_sha') != sha or
            receipt.get('durability_ref') != f'refs/tags/market-data-read/{sha}' or
            receipt.get('verified_required_outputs') != outputs):
            raise RecoveryError('closure_invalid')
        value = json.loads(a.git(repo, 'show', f'{sha}:{manifest}'))
        validate_manifest(value, outputs, request_id, day)
        return receipt
    except Exception:
        raise RecoveryError('closure_invalid') from None


def make_writer(repo: Path, expected: str, day: str, request_id: str):
    a = admission()

    def verify_metadata(root, sha, *, required_manifest=None, expected_report=None):
        if required_manifest is None:
            return a.verify_durability(root, sha)
        outputs = [{'path': expected_report['path'], 'blob_sha': expected_report['blob_sha']}]
        return verify_outputs(root, sha, required_manifest, outputs, request_id, day)

    class RecoveryPublisher(a.StorePublisher):
        def report(self, value):
            self.assert_current()
            if (value.get('request_id') != request_id or value.get('trade_date') != day or
                value.get('mode') != MODE):
                raise RecoveryError('closure_invalid')
            budget_path = self.safe_path(self.budget_relative)
            budget = a.read_budget_state(budget_path, day)
            path = self.safe_path(self.report_relative)
            write_json(path, value)
            blob = a.git(self.repo, 'hash-object', '--', str(path))
            budget.update(schema_version=1, request_id=request_id, stage=MODE,
                          required_outputs=[{'path': self.report_relative[len(PREFIX):], 'blob_sha': blob}])
            write_json(budget_path, budget)
            return self.publish([self.budget_relative, self.report_relative], expected_report={
                'path': self.report_relative, 'blob_sha': blob})

    writer = RecoveryPublisher(repo, expected, day, request_id, verify=verify_metadata)
    writer.report_relative = f'{PREFIX}collector-state/{day[:7]}/{day}-daily-baseline-recovery-{request_id}.json'
    writer.allowed = {writer.budget_relative, writer.report_relative}
    if writer.safe_path(writer.report_relative).exists():
        raise RecoveryError('archive_exists')
    return writer


def source_guard(compute: Path, sha: str) -> None:
    a = admission()
    if (a.git(compute, 'rev-parse', 'HEAD') != sha or
        a.git(compute, 'status', '--porcelain') or
        a.git(compute, 'ls-remote', 'origin', 'refs/heads/main').split() != [sha, 'refs/heads/main']):
        raise RecoveryError('object_drift')


def series_paths(repo: Path, provider: str, symbol: str, index: dict) -> set[str]:
    if not re.fullmatch(r'[A-Z0-9.^_-]+', symbol) or not re.fullmatch(r'[a-z0-9_]+', provider):
        raise RecoveryError('unsafe_path')
    prefix = f'{PREFIX}series/daily/{provider}/{symbol}/'
    names = []
    for meta in index['shards']:
        name = meta.get('path')
        if not isinstance(name, str) or not re.fullmatch(r'\d{6}\.json', name):
            raise RecoveryError('unsafe_path')
        names.append(name)
    if not names or len(names) != len(set(names)):
        raise RecoveryError('original_invalid')
    paths = {prefix + '_index.json', *(prefix + name for name in names)}
    for relative in paths:
        if not safe_path(repo, relative).is_file():
            raise RecoveryError('original_invalid')
    actual = {str(p.relative_to(repo)) for p in (repo / prefix).rglob('*') if not p.is_dir()}
    if actual != paths:
        raise RecoveryError('unexpected_change')
    return paths


def original_entries(h, repo: Path, universe: dict, config: dict, day: str, *, targets: list[dict] | None = None) -> list[dict]:
    a = admission()
    provider = h.collectors.TWELVE_PROVIDER
    originals = {}
    selected = targets if targets is not None else [
        {'symbol': symbol, 'asset_class': asset, 'effective': h.collection.ticker_effective_at(universe, symbol)}
        for symbol, asset in h.collection.daily_universe(universe)]
    for selected_item in selected:
        symbol = selected_item['symbol']
        index_rel = f'{PREFIX}series/daily/{provider}/{symbol}/_index.json'
        index = json.loads(safe_path(repo, index_rel).read_text())
        paths = series_paths(repo, provider, symbol, index)  # Before any helper follows shard paths.
        archive_rel = f'{PREFIX}recovery/{day}/{provider}/daily-baseline-rebase/{symbol}.json'
        if safe_path(repo, archive_rel).exists():
            raise RecoveryError('archive_exists')
        originals[symbol] = {'old_index': index, 'index_relative': index_rel,
            'old_files': {p: a.git(repo, 'hash-object', '--', str(repo / p)) for p in paths},
            'archive_relative': archive_rel}
    entries = (a.prepare_series(h, repo / PREFIX, universe, config, day) if targets is None else
               a.prepare_series(h, repo / PREFIX, universe, config, day, targets=selected))
    for item in entries:
        item.update(originals[item['symbol']])
    require_complete(list(originals), entries)
    return entries


def qualify(h, item: dict, fetched: list[dict], day: str, limits: dict) -> None:
    summary = admission().compare_replacement(h, item, fetched, day, limits)
    rows = sorted((h.rebaser._validated_replacement_bar(x) for x in fetched
                   if x['trade_date'] < day and
                   (item['effective'] is None or x['trade_date'] >= item['effective'])),
                  key=lambda x: x['trade_date'])
    start = min(item['existing'][0]['trade_date'], rows[-item['target_records']:][0]['trade_date'])
    rows = [x for x in rows if x['trade_date'] >= start]
    if h.store.json_sha256(rows) != summary['replacement_sha256']:
        raise RecoveryError('replacement_rejected')
    item.update(rows=rows, summary=summary)


def validate_candidate(h, stage: Path, entries: list[dict], day: str) -> set[str]:
    provider, root = h.collectors.TWELVE_PROVIDER, stage / PREFIX
    outputs = set()
    for item in entries:
        symbol = item['symbol']
        index = json.loads(safe_path(stage, item['index_relative']).read_text())
        paths = series_paths(stage, provider, symbol, index)
        rows = h.store.read_daily_series(root, provider=provider, symbol=symbol)
        if h.store.json_sha256(rows) != item['summary']['replacement_sha256'] or rows != item['rows']:
            raise RecoveryError('candidate_invalid')
        for key in ('schema_version', 'provider', 'symbol', 'asset_class', 'series_id', 'session',
                    'interval', 'series_semantics', 'adjustment_semantics', 'identity_effective_from',
                    'sort_order', 'shard_record_limit', 'shard_byte_limit'):
            if index.get(key) != item['old_index'].get(key):
                raise RecoveryError('candidate_invalid')
        archive_rel = item['archive_relative'][len(PREFIX):]
        if (index.get('first_trade_date') != rows[0]['trade_date'] or
            index.get('last_trade_date') != rows[-1]['trade_date'] or
            index.get('record_count') != len(rows) or
            index.get('baseline_initialization', {}).get('recovery_archive_path') != archive_rel or
            index.get('baseline_initialization', {}).get('revised_overlap_records') != len(item['summary']['revisions'])):
            raise RecoveryError('candidate_invalid')
        cursor = 0
        for shard in index['shards']:
            if shard['start'] != cursor or shard['end'] != cursor + shard['count'] - 1:
                raise RecoveryError('candidate_invalid')
            cursor += shard['count']
        archive = json.loads(safe_path(stage, item['archive_relative']).read_text())
        expected = {'schema_version': 1, 'market_fact_recovery_archive': True,
                    'provider': provider, 'symbol': symbol, 'trade_date': day,
                    'old_index': item['old_index'], 'old_records': item['existing'],
                    'revisions': item['summary']['revisions'], 'replacement_record_count': len(rows)}
        if any(archive.get(k) != v for k, v in expected.items()):
            raise RecoveryError('candidate_invalid')
        outputs.update(paths)
        outputs.add(item['archive_relative'])
    return outputs


def assert_changes(stage: Path, parent: str, allowed: set[str], deletions: set[str]) -> None:
    a = admission()
    changed = set(filter(None, a.git(stage, 'diff', '--name-only', '-z', parent, '--').split('\0')))
    changed.update(filter(None, a.git(stage, 'ls-files', '--others', '-z').split('\0')))
    if not changed <= allowed:
        raise RecoveryError('unexpected_change')
    for relative in changed:
        path = safe_path(stage, relative)
        if not path.is_file() and not (relative in deletions and not path.exists()):
            raise RecoveryError('unexpected_change')


def stage_and_publish(h, writer, entries: list[dict], report: dict, *, compute: Path,
                      compute_sha: str, day: str, maturity: str, deadline: float, clock) -> dict:
    a = admission()
    writer.assert_current()
    parent = writer.head
    old_paths = {p for item in entries for p in item['old_files']}
    with tempfile.TemporaryDirectory(prefix='market-recovery-') as temp:
        stage = Path(temp) / 'candidate'
        a.git(writer.repo, 'worktree', 'add', '--detach', str(stage), parent)
        try:
            root = stage / PREFIX
            for item in entries:
                for path, blob in item['old_files'].items():
                    if a.git(stage, 'hash-object', '--', str(safe_path(stage, path))) != blob:
                        raise RecoveryError('object_drift')
                archive = h.rebaser._archive_old_series(store_root=root, trade_date=day,
                    provider=h.collectors.TWELVE_PROVIDER, symbol=item['symbol'],
                    index=item['old_index'], records=item['existing'],
                    revisions=item['summary']['revisions'], replacement_record_count=len(item['rows']))
                if archive != safe_path(stage, item['archive_relative']):
                    raise RecoveryError('unsafe_path')
                h.rebaser._replace_series(index_path=stage / item['index_relative'],
                    index=copy.deepcopy(item['old_index']), records=item['rows'], trade_date=day,
                    target_records=item['target_records'], archive_path=archive,
                    revision_count=len(item['summary']['revisions']), store_root=root,
                    history_outputsize=item['outputsize'])
            outputs = validate_candidate(h, stage, entries, day)
            inventory = importlib.import_module('build_market_data_inventory').build_inventory(root)
            write_json(safe_path(stage, INVENTORY), inventory)
            # Immutable document does not claim its own publication succeeded.
            final = dict(report, status='CANDIDATE_VALIDATED',
                         results=[x['summary'] for x in entries],
                         finished_at=clock().isoformat(), budget_parent=parent,
                         publication_requires_terminal_receipt=True)
            write_json(safe_path(stage, writer.report_relative), final)
            outputs.update({INVENTORY, writer.report_relative})
            required = [{'path': p, 'blob_sha': a.git(stage, 'hash-object', '--', str(safe_path(stage, p)))}
                        for p in sorted(outputs)]
            budget_path = safe_path(stage, writer.budget_relative)
            budget = a.read_budget_state(budget_path, day)
            budget.update(schema_version=1, request_id=writer.request_id, stage=MODE,
                          required_outputs=[{'path': x['path'][len(PREFIX):], 'blob_sha': x['blob_sha']}
                                            for x in required])
            validate_manifest(budget, required, writer.request_id, day)
            write_json(budget_path, budget)
            allowed = outputs | old_paths | {writer.budget_relative}
            assert_changes(stage, parent, allowed, old_paths - outputs)
            a.git(stage, 'add', '-A', '--', *sorted(allowed))
            a.git(stage, '-c', 'user.name=stock-market-data-bot', '-c',
                  'user.email=stock-market-data-bot@users.noreply.github.com', 'commit',
                  '--quiet', '--no-gpg-sign', '-m', 'data: publish fully validated daily history recovery')
            candidate = a.git(stage, 'rev-parse', 'HEAD')
            if a.git(stage, 'rev-parse', 'HEAD^') != parent:
                raise RecoveryError('object_drift')
            # Recheck after all staging/commit work; never integrate a moving remote.
            writer.assert_current(parent)
            source_guard(compute, compute_sha)
            a.validate_target(day, maturity, clock())
            if time.monotonic() > deadline:
                raise RecoveryError('deadline_exceeded')
            try:
                a.git(stage, 'push', 'origin', f'{candidate}:refs/heads/main')
                receipt = verify_outputs(stage, candidate, writer.budget_relative, required,
                                         writer.request_id, day)
            except Exception:
                raise RecoveryError('publication_unconfirmed', candidate_sha=candidate) from None
            return {'mode': MODE, 'status': 'PASS', 'compute_sha': compute_sha,
                    'store_start_sha': writer.base, 'store_commit_sha': candidate,
                    'request_id': writer.request_id, 'recovered_count': len(entries),
                    'requests_attempted': report['requests_attempted'], 'budget_debits': report['budget_debits'],
                    'result_state_path': writer.report_relative,
                    'verified_required_outputs': receipt['verified_required_outputs'],
                    'production_recovery_accepted': False}
        finally:
            # Cleanup is local only, never a rollback/push of production refs.
            try:
                a.git(writer.repo, 'worktree', 'remove', '--force', str(stage))
            except Exception:
                pass


def recover(h, *, compute: Path, compute_sha: str, repo: Path, store_sha: str, day: str,
            request_id: str, config: dict, universe: dict, access: dict, key: str,
            clock=now_utc, dynamic_selector: dict | None = None, discovery_repo: Path | None = None) -> dict:
    a = admission()
    maturity = config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
    a.validate_target(day, maturity, clock())
    source_guard(compute, compute_sha)
    if not key:
        raise RecoveryError('credential_missing')
    writer = make_writer(repo, store_sha, day, request_id)
    if dynamic_selector is None:
        entries = original_entries(h, writer.repo, universe, config, day)
        targets = [symbol for symbol, _ in h.collection.daily_universe(universe)]
    else:
        from dynamic_baseline_targets import resolve_dynamic_targets
        try:
            selected = resolve_dynamic_targets(h, store_repo=writer.repo, store_sha=store_sha,
                discovery_repo=discovery_repo, selector=dynamic_selector, universe=universe, day=day)
            entries = original_entries(h, writer.repo, universe, config, day, targets=selected)
        except Exception:
            raise RecoveryError('original_invalid') from None
        targets = [item['symbol'] for item in selected]
    require_complete(targets, entries)
    report = {'schema_version': 1, 'mode': MODE, 'status': 'IN_PROGRESS',
              'compute_sha': compute_sha, 'store_start_sha': store_sha, 'trade_date': day,
              'request_id': request_id, 'target_count': len(entries), 'requests_attempted': 0,
              'budget_debits': 0, 'started_at': clock().isoformat(), 'market_fact_authority': False,
              'production_recovery_accepted': False, 'timing_diagnostics': []}
    if dynamic_selector is not None:
        report['dynamic_selection'] = dynamic_selector
        report['source_result_commit_sha'] = selected[0]['source_result_commit_sha']
    writer.report(report)
    deadline = time.monotonic() + 600
    budget = config['collector']['budgets']['twelve_data_basic']
    limits = {k: int(config['series']['baseline_initialization']['explicit_rebase'][k]) for k in
              ('maximum_revised_overlap_records', 'revised_overlap_must_be_within_last_existing_sessions')}
    try:
        for item in entries:
            source_guard(compute, compute_sha)
            a.validate_target(day, maturity, clock())
            debit = a.debit_before_request(h, writer, writer.repo / PREFIX, day, budget, clock=clock,
                deadline=deadline, maturity=maturity, compute=compute, compute_sha=compute_sha,
                timing=report['timing_diagnostics'])
            report['budget_debits'] += 1
            attempted = False
            def before_request():
                nonlocal attempted
                if attempted:
                    raise RecoveryError('candidate_invalid')
                a.guard_provider_request(writer, debit, compute, compute_sha, day, clock=clock, deadline=deadline,
                    timing=report['timing_diagnostics'])
                attempted = True
                report['requests_attempted'] += 1
            fetched = h.rebaser._fetch_replacement_daily(item['symbol'], key, access,
                outputsize=item['outputsize'], trade_date=day, effective=item['effective'], before_request=before_request)
            if not attempted:
                raise RecoveryError('candidate_invalid')
            qualify(h, item, fetched, day, limits)
        require_complete(targets, entries)
        return stage_and_publish(h, writer, entries, report, compute=compute,
            compute_sha=compute_sha, day=day, maturity=maturity, deadline=deadline, clock=clock)
    except Exception as exc:
        if getattr(exc, 'debited', False):
            report['budget_debits'] += 1
        # Never make another write when a fact push may already have happened.
        if not getattr(exc, 'candidate_sha', None):
            try:
                writer.report(dict(report, status='BLOCKED', failure_code=failure_summary(exc)['failure_code']))
            except Exception:
                pass  # A dirty/drifting budget checkout or failed publication cannot be hidden.
        raise


def failure_summary(exc: Exception) -> dict:
    code = getattr(exc, 'code', 'internal_error')
    out = {'mode': MODE, 'status': 'BLOCKED', 'failure_code': code if code in SAFE_CODES else 'internal_error'}
    candidate = getattr(exc, 'candidate_sha', None)
    if isinstance(candidate, str) and re.fullmatch(r'[0-9a-f]{40}', candidate):
        out.update(candidate_commit_sha=candidate, publication_outcome='UNCONFIRMED', retry_allowed=False)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('trade-date', 'compute-sha', 'store-sha', 'store-repo', 'request-id'):
        p.add_argument('--' + flag, required=True)
    p.add_argument('--dynamic-selection', default=None)
    p.add_argument('--discovery-repo', default=None)
    args = p.parse_args(argv)
    os.umask(0o077)
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REPOSITORY') != 'yuatom/stock-market-data' or
            os.environ.get('GITHUB_REF') != 'refs/heads/main' or
            os.environ.get('GITHUB_SHA') != args.compute_sha):
            raise RecoveryError('wrong_execution_surface')
        a = admission()
        h = a.load_helpers()
        root = Path(__file__).resolve().parents[1]
        result = recover(h, compute=root, compute_sha=args.compute_sha, repo=Path(args.store_repo),
            store_sha=args.store_sha, day=args.trade_date, request_id=args.request_id,
            config=h.collectors.load_yaml(root / 'config/market-data-store.yaml'),
            universe=h.collection.load_collection_universe(root / 'config/collection-universe.json'),
            access=h.collectors.load_yaml(root / 'config/market-data-collector-access.yaml'),
            key=os.environ.get('TWELVE_DATA_API_KEY', ''),
            dynamic_selector=parse_dynamic_selection(args.dynamic_selection),
            discovery_repo=Path(args.discovery_repo) if args.discovery_repo else None)
        # Detailed closure paths are available in the private manifest, not public logs.
        public = {k: v for k, v in result.items() if k != 'verified_required_outputs'}
        public['verified_required_output_count'] = len(result['verified_required_outputs'])
        print(json.dumps(public, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(failure_summary(exc), sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
