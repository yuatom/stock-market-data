#!/usr/bin/env python3
"""Explicit mature-history tail append, with current-day guarded accounting.

This is not a rebase or a route around a rejected history. All retained dates
must still be fetched and agree exactly. Only new tail records may be appended.
"""
from __future__ import annotations

import copy
import argparse
import importlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import recover_market_data_baseline as recovery
from recover_market_data_baseline import RecoveryError
from fixed_baseline_targets import parse_fixed_selection, resolve_fixed_targets
from replacement_diagnostics import ReplacementDiagnostic

MODE = 'daily_history_tail_backfill'
ET = ZoneInfo('America/New_York')


def validate_dates(execution_date: str, historical_cutoff: str, maturity: str,
                   now: datetime) -> str:
    """Validate two distinct clocks; return the exclusive provider cutoff."""
    try:
        if now.tzinfo is None:
            raise ValueError
        for day in (execution_date, historical_cutoff):
            if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                raise ValueError
        execution = datetime.strptime(execution_date, '%Y-%m-%d').date()
        cutoff = datetime.strptime(historical_cutoff, '%Y-%m-%d').date()
        if not isinstance(maturity, str) or not re.fullmatch(r'\d{2}:\d{2}', maturity):
            raise ValueError
        hour, minute = (int(x) for x in maturity.split(':'))
        local = now.astimezone(ET)
        if execution != local.date() or cutoff >= execution:
            raise ValueError
        next_day = cutoff + timedelta(days=1)
        boundary = datetime(next_day.year, next_day.month, next_day.day, hour, minute, tzinfo=ET)
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise RecoveryError('invalid_target') from None
    if local < boundary:
        raise RecoveryError('input_not_mature')
    return next_day.isoformat()


def qualify_tail(h, item: dict, fetched: list[dict], cutoff: str) -> None:
    """Zero-revision full retained overlap; no prefix growth or internal inserts."""
    existing = item['existing']
    if not existing or cutoff < existing[-1]['trade_date']:
        raise RecoveryError('invalid_target')
    rows = [h.rebaser._validated_replacement_bar(x) for x in fetched]
    rows = sorted((x for x in rows if x['trade_date'] <= cutoff and
                   (item['effective'] is None or x['trade_date'] >= item['effective'])),
                  key=lambda x: x['trade_date'])
    if not rows or rows[-1]['trade_date'] != cutoff or (len(rows) < item['target_records'] and not item['effective']):
        raise RecoveryError('replacement_rejected')
    # Reuse the unchanged strict canonical comparison, with no revisions allowed.
    h.rebaser.validate_replacement(existing, rows, maximum_revised_overlap_records=0,
                                 revised_overlap_must_be_within_last_existing_sessions=1)
    old_dates = {x['trade_date'] for x in existing}
    if any(existing[0]['trade_date'] <= x['trade_date'] <= existing[-1]['trade_date'] and
           x['trade_date'] not in old_dates for x in rows):
        raise RecoveryError('candidate_invalid')
    new_rows = [x for x in rows if x['trade_date'] > existing[-1]['trade_date']]
    combined = copy.deepcopy(existing) + new_rows
    if len(combined) < item['target_records'] and not item['effective']:
        # Older fetched prefix rows are not persisted by this mode and cannot
        # satisfy the retained analytical baseline minimum.
        raise RecoveryError('replacement_rejected')
    item.update(rows=combined, new_rows=new_rows, summary={
        'symbol': item['symbol'], 'status': 'QUALIFIED', 'existing_records': len(existing),
        'new_records': len(new_rows), 'record_count': len(combined), 'revised_overlap_records': 0,
        'last_trade_date': combined[-1]['trade_date'], 'historical_cutoff': cutoff,
        'exchange_calendar_completeness_established': False,
        'coverage_scope': 'provider_returned_tail_with_full_retained_overlap',
        'replacement_sha256': h.store.json_sha256(combined)})


def stage_tail(h, stage: Path, entries: list[dict], execution_date: str, cutoff: str) -> set[str]:
    """Use the normal append primitive, retaining old logical rows and sealed blobs."""
    outputs = set()
    provider = h.collectors.TWELVE_PROVIDER
    for item in entries:
        for path, blob in item['old_files'].items():
            if h.store.git_blob_sha_bytes(recovery.safe_path(stage, path).read_bytes()) != blob:
                raise RecoveryError('object_drift')
        old = item['old_index']
        h.store.append_daily_bars(stage / recovery.PREFIX, provider=provider,
            symbol=item['symbol'], asset_class=old['asset_class'], bars=item['new_rows'],
            series_semantics=old['series_semantics'], adjustment_semantics=old['adjustment_semantics'],
            identity_effective_from=item['effective'], shard_record_limit=old['shard_record_limit'],
            shard_byte_limit=old['shard_byte_limit'], lineage={
                'ingest_kind': MODE, 'execution_date': execution_date,
                'historical_cutoff': cutoff, 'history_outputsize': item['outputsize']})
    # Validate every target after all writes, so a late writer cannot corrupt an
    # earlier target after its check. No path is published until this completes.
    for item in entries:
        old = item['old_index']
        index = json.loads(recovery.safe_path(stage, item['index_relative']).read_text())
        paths = recovery.series_paths(stage, provider, item['symbol'], index)
        rows = h.store.read_daily_series(stage / recovery.PREFIX, provider=provider, symbol=item['symbol'])
        if (h.store.canonical_bytes(rows) != h.store.canonical_bytes(item['rows']) or
            h.store.canonical_bytes(rows[:len(item['existing'])]) != h.store.canonical_bytes(item['existing']) or
            h.store.json_sha256(rows) != item['summary']['replacement_sha256']):
            raise RecoveryError('candidate_invalid')
        mutable = {'record_count', 'last_trade_date', 'shards', 'lineage_segments'}
        if {k: v for k, v in index.items() if k not in mutable} != {k: v for k, v in old.items() if k not in mutable}:
            raise RecoveryError('candidate_invalid')
        expected_lineage = copy.deepcopy(old.get('lineage_segments', []))
        if item['new_rows']:
            expected_lineage.append({'ingest_kind': MODE, 'execution_date': execution_date,
                'historical_cutoff': cutoff, 'history_outputsize': item['outputsize'],
                'start': len(item['existing']), 'end': len(rows)-1})
        if (index['lineage_segments'] != expected_lineage or index['record_count'] != len(rows) or
            index['first_trade_date'] != rows[0]['trade_date'] or index['last_trade_date'] != rows[-1]['trade_date']):
            raise RecoveryError('candidate_invalid')
        cursor = 0
        for shard in index['shards']:
            if shard['start'] != cursor or shard['end'] != cursor + shard['count'] - 1:
                raise RecoveryError('candidate_invalid')
            cursor += shard['count']
        prefix = str(Path(item['index_relative']).parent) + '/'
        for shard in old['shards']:
            if shard['sealed']:
                path = prefix + shard['path']
                if h.store.git_blob_sha_bytes(recovery.safe_path(stage, path).read_bytes()) != item['old_files'][path]:
                    raise RecoveryError('candidate_invalid')
        outputs.update(paths)
    return outputs


def failure_summary(exc: Exception) -> dict:
    value = recovery.failure_summary(exc)
    value['mode'] = MODE
    if isinstance(value.get('terminal_report_failure'), dict):
        value['terminal_report_failure']['mode'] = MODE
    return value


def publish_tail(h, writer, entries, report, *, compute, compute_sha, execution_date,
                 historical_cutoff, maturity, deadline, clock):
    a = recovery.admission()
    writer.assert_current()
    parent = writer.head
    with tempfile.TemporaryDirectory(prefix='market-tail-') as temp:
        stage = Path(temp) / 'candidate'
        a.git(writer.repo, 'worktree', 'add', '--detach', str(stage), parent)
        try:
            outputs = stage_tail(h, stage, entries, execution_date, historical_cutoff)
            inventory = importlib.import_module('build_market_data_inventory').build_inventory(stage / recovery.PREFIX)
            recovery.write_json(recovery.safe_path(stage, recovery.INVENTORY), inventory)
            final = dict(report, status='CANDIDATE_VALIDATED', results=[x['summary'] for x in entries],
                         finished_at=clock().isoformat(), budget_parent=parent,
                         publication_requires_terminal_receipt=True)
            recovery.write_json(recovery.safe_path(stage, writer.report_relative), final)
            outputs.update((recovery.INVENTORY, writer.report_relative))
            required = [{'path': path, 'blob_sha': a.git(stage, 'hash-object', '--', str(recovery.safe_path(stage, path)))}
                        for path in sorted(outputs)]
            budget_path = recovery.safe_path(stage, writer.budget_relative)
            budget = a.read_budget_state(budget_path, execution_date)
            budget.update(schema_version=1, request_id=writer.request_id, stage=MODE,
                          required_outputs=[{'path': x['path'][len(recovery.PREFIX):], 'blob_sha': x['blob_sha']} for x in required])
            recovery.validate_manifest(budget, required, writer.request_id, execution_date, mode=MODE)
            recovery.write_json(budget_path, budget)
            allowed = outputs | {writer.budget_relative}
            recovery.assert_changes(stage, parent, allowed, set())
            a.git(stage, 'add', '-A', '--', *sorted(allowed))
            a.git(stage, '-c', 'user.name=stock-market-data-bot', '-c',
                  'user.email=stock-market-data-bot@users.noreply.github.com', 'commit',
                  '--quiet', '--no-gpg-sign', '-m', 'data: publish guarded historical tail append')
            candidate = a.git(stage, 'rev-parse', 'HEAD')
            if a.git(stage, 'rev-parse', 'HEAD^') != parent:
                raise RecoveryError('object_drift')
            writer.assert_current(parent)
            recovery.source_guard(compute, compute_sha)
            validate_dates(execution_date, historical_cutoff, maturity, clock())
            if time.monotonic() > deadline:
                raise RecoveryError('deadline_exceeded')
            publication = {'candidate_commit_sha': candidate, 'last_verified_commit_sha': writer.head,
                           'publication_kind': 'market_facts', 'publication_phase': 'main_push',
                           'main_publication_outcome': 'UNCONFIRMED', 'durability_outcome': 'UNCONFIRMED'}
            try:
                a.git(stage, 'push', 'origin', f'{candidate}:refs/heads/main')
                publication['publication_phase'] = 'durability_verification'
                receipt = recovery.verify_outputs(stage, candidate, writer.budget_relative, required,
                    writer.request_id, execution_date, mode=MODE)
            except Exception as exc:
                raise RecoveryError('publication_unconfirmed', candidate_sha=candidate, publication=publication,
                    verifier_reason=getattr(exc, 'verifier_reason', None),
                    publication_failure_code=getattr(exc, 'code', 'internal_error')) from None
            return {'mode': MODE, 'status': 'PASS', 'compute_sha': compute_sha, 'store_start_sha': writer.base,
                    'store_commit_sha': candidate, 'request_id': writer.request_id, 'execution_date': execution_date,
                    'historical_cutoff': historical_cutoff, 'appended_count': sum(bool(x['new_rows']) for x in entries),
                    'requests_attempted': report['requests_attempted'], 'budget_debits': report['budget_debits'],
                    'result_state_path': writer.report_relative, 'verified_required_outputs': receipt['verified_required_outputs'],
                    'exchange_calendar_completeness_established': False, 'production_recovery_accepted': False}
        finally:
            try:
                a.git(writer.repo, 'worktree', 'remove', '--force', str(stage))
            except Exception:
                pass


def backfill(h, *, compute, compute_sha, repo, store_sha, execution_date, historical_cutoff,
             request_id, config, universe, access, key, fixed_selector, clock=recovery.now_utc):
    a = recovery.admission()
    maturity = config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
    data_day = validate_dates(execution_date, historical_cutoff, maturity, clock())
    recovery.source_guard(compute, compute_sha)
    if not key:
        raise RecoveryError('credential_missing')
    # There is intentionally no omitted-selector/all-universe or dynamic fallback.
    if not fixed_selector:
        raise RecoveryError('incomplete_targets')
    selected = resolve_fixed_targets(h, store_repo=repo, store_sha=store_sha, selector=fixed_selector, universe=universe)
    writer = recovery.make_writer(repo, store_sha, execution_date, request_id, mode=MODE)
    originals = {}
    for item in selected:
        relative = f'{recovery.PREFIX}series/daily/{h.collectors.TWELVE_PROVIDER}/{item["symbol"]}/_index.json'
        index = json.loads(recovery.safe_path(repo, relative).read_text())
        # Do not let prepare_series follow a shard path before these checks.
        paths = recovery.series_paths(repo, h.collectors.TWELVE_PROVIDER, item['symbol'], index)
        originals[item['symbol']] = dict(old_index=index, index_relative=relative,
            old_files={path: a.git(repo, 'hash-object', '--', str(recovery.safe_path(repo, path))) for path in paths})
    entries = a.prepare_series(h, repo / recovery.PREFIX, universe, config, data_day, targets=selected)
    for item in entries:
        if historical_cutoff < item['existing'][-1]['trade_date']:
            raise RecoveryError('invalid_target')
        item.update(originals[item['symbol']])
    targets = [x['symbol'] for x in selected]
    recovery.require_complete(targets, entries)
    report = {'schema_version': 1, 'mode': MODE, 'status': 'IN_PROGRESS', 'compute_sha': compute_sha,
              'store_start_sha': store_sha, 'trade_date': execution_date, 'execution_date': execution_date,
              'historical_cutoff': historical_cutoff, 'request_id': request_id, 'target_count': len(entries),
              'requests_attempted': 0, 'budget_debits': 0, 'started_at': clock().isoformat(),
              'market_fact_authority': False, 'production_recovery_accepted': False,
              'fixed_selection': fixed_selector, 'timing_diagnostics': []}
    try:
        writer.report(report)
        deadline = time.monotonic() + 600
        budget = config['collector']['budgets']['twelve_data_basic']
        for offset, item in enumerate(entries):
            recovery.source_guard(compute, compute_sha)
            validate_dates(execution_date, historical_cutoff, maturity, clock())
            if offset == 0:
                path = writer.safe_path(writer.budget_relative)
                per_day = int(budget['hard_credits_per_day'])
                a.validate_budget_file(path, execution_date, per_day=per_day, per_minute=int(budget['hard_credits_per_minute']))
                if len(entries) > per_day - a.read_budget_state(path, execution_date)['credits']:
                    raise RecoveryError('budget_exhausted')
            debit = a.debit_before_request(h, writer, repo / recovery.PREFIX, execution_date, budget, clock=clock,
                deadline=deadline, maturity='00:00', compute=compute, compute_sha=compute_sha,
                timing=report['timing_diagnostics'])
            report['budget_debits'] += 1
            attempted = False
            def before_request():
                nonlocal attempted
                if attempted:
                    raise RecoveryError('candidate_invalid')
                a.guard_provider_request(writer, debit, compute, compute_sha, execution_date, clock=clock,
                                         deadline=deadline, timing=report['timing_diagnostics'])
                validate_dates(execution_date, historical_cutoff, maturity, clock())
                attempted = True
                report['requests_attempted'] += 1
            fetched = h.rebaser._fetch_replacement_daily(item['symbol'], key, access, outputsize=item['outputsize'],
                trade_date=data_day, effective=item['effective'], before_request=before_request)
            if not attempted:
                raise RecoveryError('candidate_invalid')
            try:
                qualify_tail(h, item, fetched, historical_cutoff)
            except Exception as exc:
                evidence = getattr(exc, 'rejection_evidence', None)
                safe = RecoveryError('replacement_rejected')
                safe.replacement_diagnostic = ReplacementDiagnostic(item['symbol'], tuple(targets), evidence)
                raise safe from None
        recovery.require_complete(targets, entries)
        if not any(x['new_rows'] for x in entries):
            writer.report(dict(report, status='NOOP', results=[x['summary'] for x in entries]))
            return dict(report, status='NOOP', store_commit_sha=writer.head, verified_required_outputs=[])
        return publish_tail(h, writer, entries, report, compute=compute, compute_sha=compute_sha,
            execution_date=execution_date, historical_cutoff=historical_cutoff, maturity=maturity,
            deadline=deadline, clock=clock)
    except Exception as exc:
        if getattr(exc, 'debited', False):
            report['budget_debits'] += 1
        unconfirmed = int(getattr(exc, 'predebit', {}).get('state') == 'UNCONFIRMED')
        progress = {'requests_attempted': report['requests_attempted'], 'budget_debits': report['budget_debits'],
                    'budget_debits_confirmed': report['budget_debits'], 'budget_debits_unconfirmed': unconfirmed,
                    'budget_debits_attempted': report['budget_debits'] + unconfirmed,
                    'terminal_report_outcome': 'NOT_ATTEMPTED'}
        if not getattr(exc, 'candidate_sha', None) and not getattr(exc, 'publication', None):
            try:
                writer.report(dict(report, **failure_summary(exc)))
                progress['terminal_report_outcome'] = 'CONFIRMED'
            except Exception as diagnostic_exc:
                progress['terminal_report_outcome'] = 'UNCONFIRMED'
                exc.terminal_report_failure = failure_summary(diagnostic_exc)
        exc.recovery_progress = progress
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('execution-date', 'historical-cutoff', 'compute-sha', 'store-sha', 'store-repo', 'request-id', 'fixed-selection'):
        p.add_argument('--' + flag, required=True)
    args = p.parse_args(argv)
    os.umask(0o077)
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REPOSITORY') != 'yuatom/stock-market-data' or
            os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_SHA') != args.compute_sha):
            raise RecoveryError('wrong_execution_surface')
        root = Path(__file__).resolve().parents[1]
        h = recovery.admission().load_helpers()
        result = backfill(h, compute=root, compute_sha=args.compute_sha, repo=Path(args.store_repo),
            store_sha=args.store_sha, execution_date=args.execution_date, historical_cutoff=args.historical_cutoff,
            request_id=args.request_id, config=h.collectors.load_yaml(root/'config/market-data-store.yaml'),
            universe=h.collection.load_collection_universe(root/'config/collection-universe.json'),
            access=h.collectors.load_yaml(root/'config/market-data-collector-access.yaml'),
            key=os.environ.get('TWELVE_DATA_API_KEY', ''), fixed_selector=parse_fixed_selection(args.fixed_selection))
        public = {k: v for k, v in result.items() if k not in ('verified_required_outputs', 'timing_diagnostics', 'fixed_selection')}
        public['verified_required_output_count'] = len(result['verified_required_outputs'])
        print(json.dumps(public, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(failure_summary(exc), sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
