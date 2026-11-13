#!/usr/bin/env python3
"""Explicit fixed subset, one guarded atomic transaction per symbol.

This does not relax replacement gates. Only a positively identified quality
rejection with a durable diagnostic may be quarantined. Unknown states stop.
Each completed fact publication remains real even if a later transaction fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import admit_market_data_baseline as a
import recover_market_data_baseline as r
from fixed_baseline_targets import parse_fixed_selection, resolve_fixed_targets

MODE = 'daily_symbol_maintenance'
ISOLATABLE_RULES = frozenset({
    'revision_outside_tail', 'revised_overlap_limit', 'missing_existing_dates',
    'insufficient_replacement_records', 'replacement_empty',
    'duplicate_replacement_dates', 'invalid_replacement_bar',
})


def _fail(code='closure_invalid'):
    raise r.RecoveryError(code)


def _remote_exact(repo, sha):
    if a.git(repo, 'ls-remote', 'origin', 'refs/heads/main').split() != [sha, 'refs/heads/main']:
        _fail('object_drift')


def _clean(repo):
    if a.git(repo, 'status', '--porcelain') or a.git(repo, 'ls-files', '--others'):
        _fail('object_drift')


def _adopt(repo, sha):
    """Only adopt the exact receipt, never fetch/pull whatever main became."""
    _remote_exact(repo, sha)
    _clean(repo)
    local = a.git(repo, 'rev-parse', 'HEAD')
    a.git(repo, 'merge-base', '--is-ancestor', local, sha)
    if local != sha:
        a.git(repo, 'merge', '--ff-only', '--no-edit', sha)
    if a.git(repo, 'rev-parse', 'HEAD') != sha:
        _fail('object_drift')
    _clean(repo)
    _remote_exact(repo, sha)


def _budget(repo, day, config):
    limits = config['collector']['budgets']['twelve_data_basic']
    path = r.safe_path(repo, f'{r.PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json')
    a.validate_budget_file(path, day, per_day=limits['hard_credits_per_day'],
                           per_minute=limits['hard_credits_per_minute'])
    return a.read_budget_state(path, day)


def _one_debit(before, after):
    if (type(after.get('credits')) is not int or after['credits'] != before['credits'] + 1 or
        type(after.get('minutes')) is not dict):
        _fail('budget_state_invalid')
    old, new = before['minutes'], after['minutes']
    if not set(old) <= set(new):
        _fail('budget_state_invalid')
    deltas = []
    for key, value in new.items():
        if type(value) is not int or not 0 <= value <= 8:
            _fail('budget_state_invalid')
        deltas.append(value - old.get(key, 0))
    if any(x not in (0, 1) for x in deltas) or sum(deltas) != 1 or after['credits'] > 200:
        _fail('budget_state_invalid')


def _qualifying_rejection(exc, symbol):
    if not isinstance(exc, (a.AdmissionError, r.RecoveryError)) or exc.code != 'replacement_rejected':
        return False
    if type(getattr(exc, 'confirmed_terminal', None)) is not r.ConfirmedTerminalReceipt:
        return False
    if getattr(exc, 'publication', None) or getattr(exc, 'candidate_sha', None) or getattr(exc, 'terminal_report_failure', None):
        return False
    summary = r.failure_summary(exc)
    return (summary.get('failed_symbol') == symbol and
            summary.get('replacement_rejection', {}).get('rejection_rule') in ISOLATABLE_RULES and
            summary.get('terminal_report_outcome') == 'CONFIRMED' and
            all(type(summary.get(k)) is int and summary[k] == expected for k, expected in (
                ('requests_attempted', 1), ('budget_debits', 1), ('budget_debits_confirmed', 1),
                ('budget_debits_attempted', 1), ('budget_debits_unconfirmed', 0))))


def _immutable_blob(repo, sha, path):
    entry = a.git(repo, 'ls-tree', sha, '--', path).split('\t')
    if len(entry) != 2 or entry[1] != path:
        _fail()
    mode, kind, blob = entry[0].split()
    if mode != '100644' or kind != 'blob' or not re.fullmatch(r'[0-9a-f]{40}', blob):
        _fail()
    return blob


def _immutable_series(repo, sha, symbol, store):
    """Read an immutable series closure, never the mutable checkout or manifest."""
    prefix = f'{r.PREFIX}series/daily/twelve_data_basic/{symbol}/'
    index_path = prefix + '_index.json'
    _immutable_blob(repo, sha, index_path)
    index = json.loads(a.git(repo, 'show', f'{sha}:{index_path}'))
    if (type(index) is not dict or index.get('schema_version') != 1 or
        index.get('provider') != 'twelve_data_basic' or index.get('symbol') != symbol or
        type(index.get('shards')) is not list or not index['shards']):
        _fail()
    paths = {index_path}; rows = []; cursor = 0
    for meta in index['shards']:
        name = meta.get('path')
        if type(name) is not str or not re.fullmatch(r'[0-9]{6}\.json', name) or prefix + name in paths:
            _fail()
        path = prefix + name; paths.add(path)
        blob = _immutable_blob(repo, sha, path)
        doc = json.loads(a.git(repo, 'show', f'{sha}:{path}'))
        if (type(doc) is not dict or doc.get('series_id') != index.get('series_id') or
            type(doc.get('records')) is not list or not doc['records'] or
            type(meta.get('start')) is not int or meta['start'] != cursor or
            type(doc.get('start')) is not int or doc['start'] != cursor):
            _fail()
        actual = store._shard_meta(name, cursor, doc, bool(meta.get('sealed')))
        for key in ('start', 'end', 'count', 'byte_length', 'json_sha256', 'blob_sha'):
            if store.canonical_bytes(actual[key]) != store.canonical_bytes(meta.get(key)):
                _fail()
        if meta['blob_sha'] != blob:
            _fail()
        rows.extend(doc['records']); cursor += len(doc['records'])
    actual_paths = set(filter(None, a.git(repo, 'ls-tree', '-r', '--name-only', '-z', sha, '--', prefix).split('\0')))
    dates = [row['trade_date'] for row in rows]
    if (actual_paths != paths or dates != sorted(set(dates)) or
        type(index.get('record_count')) is not int or index['record_count'] != len(rows) or
        index.get('first_trade_date') != dates[0] or index.get('last_trade_date') != dates[-1]):
        _fail()
    return index, rows, paths


def _success_outputs(repo, *, start, sha, symbol, day, path, report):
    """Independently derive required outputs and bind old/new facts to receipts.

    This checks retained immutable evidence, not provider truth or a new
    historical qualification. It never authorizes another request or write.
    """
    try:
        store = a.load_helpers().store
        old_index, old_rows, _ = _immutable_series(repo, start, symbol, store)
        index, rows, paths = _immutable_series(repo, sha, symbol, store)
        summaries = report.get('results')
        if type(summaries) is not list or len(summaries) != 1:
            _fail()
        summary = summaries[0]
        old_dates = {row['trade_date'] for row in old_rows}
        expected = dict(symbol=symbol, status='PASS', existing_records=len(old_rows),
            replacement_records=len(rows), new_records=sum(row['trade_date'] not in old_dates for row in rows),
            last_trade_date=rows[-1]['trade_date'], latest_date_advanced=rows[-1]['trade_date'] > old_rows[-1]['trade_date'],
            replacement_sha256=store.json_sha256(rows))
        if any(store.canonical_bytes(summary.get(k)) != store.canonical_bytes(v) for k, v in expected.items()):
            _fail()
        for key in ('schema_version', 'provider', 'symbol', 'asset_class', 'series_id', 'session',
                    'interval', 'series_semantics', 'adjustment_semantics', 'identity_effective_from',
                    'sort_order', 'shard_record_limit', 'shard_byte_limit'):
            if store.canonical_bytes(index.get(key)) != store.canonical_bytes(old_index.get(key)):
                _fail()
        archive_path = f'{r.PREFIX}recovery/{day}/twelve_data_basic/daily-baseline-rebase/{symbol}.json'
        _immutable_blob(repo, sha, archive_path)
        archive = json.loads(a.git(repo, 'show', f'{sha}:{archive_path}'))
        revisions = summary.get('revisions')
        if type(revisions) is not list:
            _fail()
        archived = dict(schema_version=1, market_fact_recovery_archive=True, provider='twelve_data_basic',
            symbol=symbol, trade_date=day, old_index=old_index, old_records=old_rows,
            revisions=revisions, replacement_record_count=len(rows))
        if any(store.canonical_bytes(archive.get(k)) != store.canonical_bytes(v) for k, v in archived.items()):
            _fail()
        baseline = index.get('baseline_initialization', {})
        if (baseline.get('recovery_archive_path') != archive_path[len(r.PREFIX):] or
            type(baseline.get('revised_overlap_records')) is not int or
            baseline['revised_overlap_records'] != len(revisions)):
            _fail()
        paths.update({path, r.INVENTORY, archive_path})
        return [{'path': p, 'blob_sha': _immutable_blob(repo, sha, p)} for p in sorted(paths)]
    except Exception:
        _fail()


def _verify_terminal(repo, *, start, sha, path, day, request_id, symbol, selector,
                     before, remaining, passed, compute_sha, result=None, rejection=None):
    """Check immutable closure, budget delta and write boundary before continuing."""
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        _fail()
    expected_path = f'{r.PREFIX}collector-state/{day[:7]}/{day}-daily-baseline-recovery-{request_id}-{symbol}.json'
    if path != expected_path:
        _fail()
    a.git(repo, 'merge-base', '--is-ancestor', start, sha)
    budget_path = f'{r.PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json'
    report = json.loads(a.git(repo, 'show', f'{sha}:{path}'))
    budget = json.loads(a.git(repo, 'show', f'{sha}:{budget_path}'))
    _one_debit(before, budget)
    expected = {'compute_sha': compute_sha, 'request_id': request_id, 'trade_date': day, 'mode': r.MODE,
                'store_start_sha': start, 'fixed_selection': selector,
                'transaction_symbol': symbol, 'target_count': 1,
                'requests_attempted': 1, 'budget_debits': 1}
    if (any(report.get(k) != v for k, v in expected.items()) or
        any(type(report.get(k)) is not int for k in ('target_count', 'requests_attempted', 'budget_debits'))):
        _fail()
    required = (_success_outputs(repo, start=start, sha=sha, symbol=symbol, day=day, path=path, report=report)
                if result is not None else [{'path': path, 'blob_sha': _immutable_blob(repo, sha, path)}])
    r.validate_manifest(budget, required, request_id, day)
    for item in required:
        r.safe_path(repo, item['path'])
        if a.git(repo, 'rev-parse', f"{sha}:{item['path']}") != item['blob_sha']:
            _fail()
    series = f'{r.PREFIX}series/daily/twelve_data_basic/{symbol}/'
    archive = f'{r.PREFIX}recovery/{day}/twelve_data_basic/daily-baseline-rebase/{symbol}.json'
    allowed = {budget_path, path}
    if result is not None:
        if (result.get('status') != 'PASS' or result.get('compute_sha') != compute_sha or result.get('store_start_sha') != start or
            result.get('request_id') != request_id or result.get('recovered_count') != 1 or
            result.get('requests_attempted') != 1 or result.get('budget_debits') != 1 or
            result.get('verified_required_outputs') != required or
            report.get('status') != 'CANDIDATE_VALIDATED'):
            _fail()
        allowed |= {r.INVENTORY, archive}
    else:
        if (report.get('status') != 'BLOCKED' or report.get('failed_symbol') != symbol or
            report.get('failure_code') != 'replacement_rejected' or
            report.get('replacement_rejection') != rejection['replacement_rejection'] or
            required != [{'path': path, 'blob_sha': a.git(repo, 'rev-parse', f'{sha}:{path}')} ]):
            _fail()
    for commit, parent in _linear_commits(repo, start, sha):
        changed = filter(None, a.git(repo, 'diff', '--name-only', '-z', parent, commit, '--').split('\0'))
        for name in changed:
            if name not in allowed and not (result is not None and name.startswith(series)):
                _fail('unexpected_change')
    for target in remaining:
        if target['symbol'] in passed:
            continue
        index = f"{r.PREFIX}series/daily/twelve_data_basic/{target['symbol']}/_index.json"
        if a.git(repo, 'rev-parse', f'{sha}:{index}') != target['index_blob_sha']:
            _fail('object_drift')
    if a.git(repo, 'ls-remote', 'origin', f'refs/tags/market-data-read/{sha}').split() != [sha, f'refs/tags/market-data-read/{sha}']:
        _fail('durability_failed')
    return report



def _publish_metadata(repo, head, day, request_id, config, value, *, deadline, symbol=None):
    """An immutable plan/checkpoint. Never called after an unknown child outcome."""
    if time.monotonic() > deadline:
        _fail('deadline_exceeded')
    before = _budget(repo, day, config)
    writer = r.make_writer(repo, head, day, request_id, mode=MODE, symbol=symbol)
    sha = writer.report(value)
    after = _budget(repo, day, config)
    if any(after[k] != before[k] for k in ('credits', 'minutes')):
        _fail('budget_state_invalid')
    return sha, writer.report_relative



def _linear_commits(repo, start, end):
    previous = start
    rows = []
    a.git(repo, 'merge-base', '--is-ancestor', start, end)
    for commit in a.git(repo, 'rev-list', '--reverse', f'{start}..{end}').splitlines():
        parents = a.git(repo, 'rev-list', '--parents', '-n', '1', commit).split()
        if parents != [commit, previous]:
            _fail('object_drift')
        rows.append((commit, previous)); previous = commit
    if previous != end:
        _fail('object_drift')
    return rows


def _coverage(report, symbol):
    rows = report.get('results')
    if type(rows) is not list or len(rows) != 1 or rows[0].get('symbol') != symbol:
        _fail()
    summary = rows[0]
    last = summary.get('last_trade_date'); count = summary.get('new_records')
    advanced = summary.get('latest_date_advanced')
    if (type(last) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', last) or
        date.fromisoformat(last).isoformat() != last or type(count) is not int or
        not 0 <= count <= 5000 or type(advanced) is not bool):
        _fail()
    return {'last_trade_date': last, 'new_records': count, 'latest_date_advanced': advanced,
            'completeness_proven': False}


def _immutable_budget(repo, sha, path):
    if not a.git(repo, 'ls-tree', '--name-only', sha, '--', path):
        return {'credits': 0, 'minutes': {}}
    value = json.loads(a.git(repo, 'show', f'{sha}:{path}'))
    if type(value.get('credits')) is not int or type(value.get('minutes')) is not dict:
        _fail('budget_state_invalid')
    return value


def _metadata_proof(repo, sha, parent, path, day, request_id):
    if _linear_commits(repo, parent, sha) != [(sha, parent)]:
        _fail('object_drift')
    budget_path = f'{r.PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json'
    changed = set(filter(None, a.git(repo, 'diff', '--name-only', '-z', parent, sha, '--').split('\0')))
    if changed != {path, budget_path}:
        _fail('unexpected_change')
    budget = _immutable_budget(repo, sha, budget_path)
    before = _immutable_budget(repo, parent, budget_path)
    if any(budget[k] != before[k] for k in ('credits', 'minutes')):
        _fail('budget_state_invalid')
    required = [{'path': path, 'blob_sha': a.git(repo, 'rev-parse', f'{sha}:{path}')}]
    r.validate_manifest(budget, required, request_id, day, mode=MODE)
    tag = f'refs/tags/market-data-read/{sha}'
    if a.git(repo, 'ls-remote', 'origin', tag).split() != [sha, tag]:
        _fail('durability_failed')


def read_progress(repo: Path, *, day: str, request_id: str, manifest_commit_sha: str) -> dict:
    """Reverify an immutable plan/child/checkpoint chain, without fetch or writes."""
    result = {'status': 'UNKNOWN', 'published_symbols': [], 'quarantined_symbols': [],
              'unreconciled_symbols': [], 'not_attempted_symbols': [],
              'automatic_retry_allowed': False, 'completeness_proven': False}
    try:
        if (type(manifest_commit_sha) is not str or not re.fullmatch(r'[0-9a-f]{40}', manifest_commit_sha) or
            not re.fullmatch(r'\d+-\d+', request_id) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)):
            _fail('invalid_target')
        path = f'{r.PREFIX}collector-state/{day[:7]}/{day}-daily-symbol-maintenance-{request_id}.json'
        plan = json.loads(a.git(repo, 'show', f'{manifest_commit_sha}:{path}'))
        selector = plan.get('fixed_selection')
        if (plan.get('schema_version') != 1 or plan.get('kind') != 'batch_plan' or plan.get('mode') != MODE or
            plan.get('status') != 'PLANNED' or plan.get('request_id') != request_id or plan.get('trade_date') != day or
            type(selector) is not dict or set(selector) != {'schema_version', 'targets'} or
            type(selector['schema_version']) is not int or selector['schema_version'] != 1 or
            type(selector['targets']) is not list or not selector['targets'] or
            plan.get('budget_limits') != {'per_day': 200, 'per_minute': 8} or
            plan.get('rebase_limits') != {'max_revisions': 3, 'tail_sessions': 3} or
            plan.get('maximum_provider_requests') != len(selector['targets']) or
            plan.get('overall_timeout_seconds') != 600):
            _fail()
        for key in ('compute_sha', 'store_start_sha'):
            if type(plan.get(key)) is not str or not re.fullmatch(r'[0-9a-f]{40}', plan[key]):
                _fail()
        targets = selector['targets']; names = []
        for target in targets:
            if (type(target) is not dict or set(target) != {'symbol', 'index_blob_sha'} or
                type(target['symbol']) is not str or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}', target['symbol']) or
                target['symbol'] in names or type(target['index_blob_sha']) is not str or
                not re.fullmatch(r'[0-9a-f]{40}', target['index_blob_sha'])):
                _fail()
            names.append(target['symbol'])
            index_path=f"{r.PREFIX}series/daily/twelve_data_basic/{target['symbol']}/_index.json"
            if a.git(repo,'rev-parse',f"{plan['store_start_sha']}:{index_path}") != target['index_blob_sha']:
                _fail('object_drift')
        _metadata_proof(repo, manifest_commit_sha, plan['store_start_sha'], path, day, request_id)
        remote = a.git(repo, 'ls-remote', 'origin', 'refs/heads/main').split()
        if len(remote) != 2 or remote[1] != 'refs/heads/main':
            _fail('object_drift')
        head = remote[0]
        a.git(repo, 'merge-base', '--is-ancestor', manifest_commit_sha, head)
        if a.git(repo, 'log', '--format=%H', f'{manifest_commit_sha}..{head}', '--', path):
            _fail('object_drift')
        verified_rows = []; passed = set(); previous = manifest_commit_sha; gap = False
        budget_path=f'{r.PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json'
        for index, target in enumerate(targets):
            symbol=target['symbol']; checkpoint=path[:-5]+f'-{symbol}.json'
            child=f'{r.PREFIX}collector-state/{day[:7]}/{day}-daily-baseline-recovery-{request_id}-{symbol}.json'
            versions=a.git(repo,'log','--format=%H',f'{manifest_commit_sha}..{head}','--',checkpoint).splitlines()
            if versions:
                if gap or len(versions)!=1:
                    _fail('object_drift')
                sha=versions[0]; value=json.loads(a.git(repo,'show',f'{sha}:{checkpoint}'))
                rows=value.get('transactions')
                if (value.get('kind')!='batch_checkpoint' or value.get('mode')!=MODE or
                    value.get('status')!='PROGRESS_VALIDATED' or value.get('request_id')!=request_id or
                    value.get('trade_date')!=day or value.get('compute_sha')!=plan['compute_sha'] or
                    value.get('batch_manifest_commit_sha')!=manifest_commit_sha or value.get('batch_manifest_path')!=path or
                    value.get('fixed_selection')!=selector or type(rows) is not list or len(rows)!=index+1 or
                    rows[:-1]!=verified_rows or value.get('requests_attempted')!=len(rows) or
                    value.get('budget_debits_confirmed')!=len(rows) or value.get('budget_debits_unconfirmed')!=0):
                    _fail()
                row=rows[-1]
                if (row.get('symbol')!=symbol or row.get('status') not in ('PASS','QUARANTINED') or
                    row.get('store_start_sha')!=previous or row.get('result_state_path')!=child or
                    type(row.get('store_commit_sha')) is not str or not re.fullmatch(r'[0-9a-f]{40}',row['store_commit_sha']) or
                    value.get('store_start_sha')!=row['store_commit_sha']):
                    _fail()
                child_sha=row['store_commit_sha']
                if a.git(repo,'log','--format=%H',f'{child_sha}..{head}','--',child):
                    _fail('object_drift')
                _metadata_proof(repo,sha,child_sha,checkpoint,day,request_id)
                child_report=json.loads(a.git(repo,'show',f'{child_sha}:{child}'))
                before=_immutable_budget(repo,previous,budget_path)
                args=dict(start=previous,sha=child_sha,path=child,day=day,request_id=request_id,symbol=symbol,
                          selector={'schema_version':1,'targets':[target]},before=before,remaining=targets,
                          passed=passed|({symbol} if row['status']=='PASS' else set()),compute_sha=plan['compute_sha'])
                if row['status']=='PASS':
                    budget=_immutable_budget(repo,child_sha,budget_path)
                    proof={'status':'PASS','compute_sha':child_report.get('compute_sha'),'store_start_sha':child_report.get('store_start_sha'),
                           'request_id':child_report.get('request_id'),'recovered_count':1,
                           'requests_attempted':child_report.get('requests_attempted'),'budget_debits':child_report.get('budget_debits'),
                           'verified_required_outputs':[{'path':r.PREFIX+x['path'],'blob_sha':x['blob_sha']} for x in budget['required_outputs']]}
                    verified=_verify_terminal(repo,**args,result=proof)
                    coverage=_coverage(verified,symbol)
                    if any(row.get(k)!=v for k,v in coverage.items()): _fail()
                    passed.add(symbol)
                else:
                    evidence=child_report.get('replacement_rejection',{})
                    if evidence.get('rejection_rule') not in ISOLATABLE_RULES or row.get('replacement_rejection')!=evidence:
                        _fail()
                    _verify_terminal(repo,**args,rejection={'replacement_rejection':evidence})
                verified_rows=rows
                verified_rows[-1]=dict(row,checkpoint_commit_sha=sha,checkpoint_path=checkpoint)
                # A later corrupt checkpoint must not hide a verified prefix.
                # Overall status remains UNKNOWN until the whole scan succeeds.
                result['published_symbols']=[x['symbol'] for x in verified_rows if x['status']=='PASS']
                result['quarantined_symbols']=[x['symbol'] for x in verified_rows if x['status']=='QUARANTINED']
                previous=sha
            else:
                gap=True
                if a.git(repo,'ls-tree','--name-only',head,'--',child): result['unreconciled_symbols'].append(symbol)
                else: result['not_attempted_symbols'].append(symbol)
        result['published_symbols']=[x['symbol'] for x in verified_rows if x['status']=='PASS']
        result['quarantined_symbols']=[x['symbol'] for x in verified_rows if x['status']=='QUARANTINED']
        if len(verified_rows)==len(names) and len(passed)==len(names): result['status']='PASS'
        elif passed: result['status']='PARTIAL'
        elif len(verified_rows)==len(names): result['status']='BLOCKED'
    except Exception as exc:
        result['failure']=r.failure_summary(exc)
    return result


def maintain(h, *, compute: Path, compute_sha: str, repo: Path, store_sha: str,
             day: str, request_id: str, config: dict, universe: dict, access: dict,
             key: str, fixed_selector: dict, clock=r.now_utc) -> dict:
    """No provider retry, no automatic resume and no dynamic/default expansion."""
    deadline = time.monotonic() + 600
    out = {'schema_version': 1, 'mode': MODE, 'status': 'BLOCKED',
           'compute_sha': compute_sha, 'store_start_sha': store_sha, 'trade_date': day,
           'request_id': request_id, 'transactions': [], 'quarantined_symbols': [],
           'not_attempted_symbols': [], 'published_count': 0, 'requests_attempted': 0,
           'budget_debits_confirmed': 0, 'budget_debits_unconfirmed': 0,
           'last_verified_commit_sha': store_sha, 'stop': None,
           'production_recovery_accepted': False, 'automatic_retry_allowed': False, 'completeness_proven': False}
    targets = []; touched = set(); passed = set(); head = store_sha
    observed = []
    try:
        maturity = config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
        a.validate_target(day, maturity, clock())
        r.source_guard(compute, compute_sha)
        if not key:
            _fail('credential_missing')
        if not isinstance(request_id, str) or not re.fullmatch(r'\d+-\d+', request_id):
            _fail('invalid_target')
        gates = config['series']['baseline_initialization']['explicit_rebase']
        budget_config = config['collector']['budgets']['twelve_data_basic']
        values = (gates['maximum_revised_overlap_records'], gates['revised_overlap_must_be_within_last_existing_sessions'],
                  budget_config['hard_credits_per_day'], budget_config['hard_credits_per_minute'])
        if any(type(x) is not int for x in values) or values != (3, 3, 200, 8):
            _fail('invalid_target')
        _clean(repo); _remote_exact(repo, store_sha)
        if a.git(repo, 'rev-parse', 'HEAD') != store_sha:
            _fail('object_drift')
        selected = resolve_fixed_targets(h, store_repo=repo, store_sha=store_sha,
                                         selector=fixed_selector, universe=universe)
        # Copy only the exact schema fields; never refresh selector blobs later.
        targets = [{'symbol': x['symbol'], 'index_blob_sha': x['index_blob_sha']} for x in selected]
        out['fixed_selection'] = {'schema_version': 1, 'targets': targets}
        r.original_entries(h, repo, universe, config, day, targets=selected)
        before = _budget(repo, day, config)
        if len(targets) > 200 - before['credits']:
            _fail('budget_exhausted')
        plan = {'schema_version': 1, 'mode': MODE, 'kind': 'batch_plan', 'status': 'PLANNED',
                'request_id': request_id, 'trade_date': day, 'compute_sha': compute_sha,
                'store_start_sha': store_sha, 'fixed_selection': out['fixed_selection'],
                'budget_limits': {'per_day': 200, 'per_minute': 8},
                'rebase_limits': {'max_revisions': 3, 'tail_sessions': 3},
                'maximum_provider_requests': len(targets), 'overall_timeout_seconds': 600,
                'market_fact_authority': False, 'automatic_retry_allowed': False}
        r.source_guard(compute, compute_sha)
        a.validate_target(day, maturity, clock())
        head, plan_path = _publish_metadata(repo, head, day, request_id, config, plan, deadline=deadline)
        out.update(batch_manifest_commit_sha=head, batch_manifest_path=plan_path,
                   last_verified_commit_sha=head)
        for target in targets:
            if time.monotonic() > deadline:
                _fail('deadline_exceeded')
            r.source_guard(compute, compute_sha)
            a.validate_target(day, maturity, clock())
            _remote_exact(repo, head); _clean(repo)
            if a.git(repo, 'rev-parse', 'HEAD') != head:
                _fail('object_drift')
            # Preserve locks for all not-yet-successful targets, including quarantined ones.
            pending = [x for x in targets if x['symbol'] not in passed]
            resolve_fixed_targets(h, store_repo=repo, store_sha=head,
                                  selector={'schema_version': 1, 'targets': pending}, universe=universe)
            before = _budget(repo, day, config)
            symbol = target['symbol']; selector = {'schema_version': 1, 'targets': [dict(target)]}
            touched.add(symbol)
            try:
                result = r.recover(h, compute=compute, compute_sha=compute_sha, repo=repo,
                    store_sha=head, day=day, request_id=request_id, config=config, universe=universe,
                    access=access, key=key, clock=clock, fixed_selector=selector,
                    symbol_transaction=symbol, overall_deadline=deadline)
            except Exception as exc:
                summary = r.failure_summary(exc)
                out['requests_attempted'] += summary.get('requests_attempted', 0)
                out['budget_debits_confirmed'] += summary.get('budget_debits_confirmed', 0)
                out['budget_debits_unconfirmed'] += summary.get('budget_debits_unconfirmed', 0)
                if not _qualifying_rejection(exc, symbol):
                    raise
                receipt = exc.confirmed_terminal
                _verify_terminal(repo, start=head, sha=receipt.store_commit_sha,
                    path=receipt.result_state_path, day=day, request_id=request_id, symbol=symbol,
                    selector=selector, before=before, remaining=targets, passed=passed, compute_sha=compute_sha, rejection=summary)
                row = {'symbol': symbol, 'status': 'QUARANTINED', 'store_start_sha': head,
                       'store_commit_sha': receipt.store_commit_sha, 'result_state_path': receipt.result_state_path,
                       'replacement_rejection': summary['replacement_rejection']}
                out['quarantined_symbols'].append(symbol)
            else:
                out['requests_attempted'] += result.get('requests_attempted', 0)
                out['budget_debits_confirmed'] += result.get('budget_debits', 0)
                # recover returns PASS only after fact durability. Retain that observation
                # even when this controller's additional consistency check later fails.
                observed_row = {'symbol': symbol, 'status': 'PASS_UNRECONCILED',
                    'store_start_sha': head, 'store_commit_sha': result.get('store_commit_sha'),
                    'result_state_path': result.get('result_state_path')}
                if result.get('status') == 'PASS':
                    observed.append(observed_row)
                    out['transactions'].append(observed_row)
                    out['published_count'] += 1
                verified_report = _verify_terminal(repo, start=head, sha=result.get('store_commit_sha'),
                    path=result.get('result_state_path'), day=day, request_id=request_id, symbol=symbol,
                    selector=selector, before=before, remaining=targets, passed=passed | {symbol}, compute_sha=compute_sha, result=result)
                row = {'symbol': symbol, 'status': 'PASS', 'store_start_sha': head,
                       'store_commit_sha': result['store_commit_sha'], 'result_state_path': result['result_state_path']}
                row.update(_coverage(verified_report, symbol))
                passed.add(symbol)
                out['transactions'][-1] = row
            if row['status'] == 'QUARANTINED':
                out['transactions'].append(row)
            head = row['store_commit_sha']; out['last_verified_commit_sha'] = head
            _adopt(repo, head)
            if time.monotonic() > deadline:
                _fail('deadline_exceeded')
            r.source_guard(compute, compute_sha)
            a.validate_target(day, maturity, clock())
            checkpoint = {'schema_version': 1, 'mode': MODE, 'kind': 'batch_checkpoint',
                'status': 'PROGRESS_VALIDATED', 'request_id': request_id, 'trade_date': day,
                'compute_sha': compute_sha, 'store_start_sha': head,
                'batch_manifest_commit_sha': out['batch_manifest_commit_sha'],
                'batch_manifest_path': out['batch_manifest_path'],
                'fixed_selection': out['fixed_selection'],
                'transactions': json.loads(json.dumps(out['transactions'])),
                'requests_attempted': out['requests_attempted'],
                'budget_debits_confirmed': out['budget_debits_confirmed'],
                'budget_debits_unconfirmed': 0, 'automatic_retry_allowed': False,
                'market_fact_authority': False}
            head, checkpoint_path = _publish_metadata(repo, head, day, request_id, config,
                checkpoint, deadline=deadline, symbol=symbol)
            row['checkpoint_commit_sha'] = head
            row['checkpoint_path'] = checkpoint_path
            out['last_verified_commit_sha'] = head
    except Exception as exc:
        out['stop'] = r.failure_summary(exc)
    out['not_attempted_symbols'] = [x['symbol'] for x in targets if x['symbol'] not in touched]
    if out['stop'] is None and targets and len(passed) == len(targets):
        out['status'] = 'PASS'
    elif observed:
        out['status'] = 'PARTIAL'
    return out


def _verify_append_terminal(h,repo,*,start,sha,path,day,cutoff,request_id,symbol,selector,before,remaining,passed,compute_sha,result=None):
    """Append-only closure: no archive/rebase paths or revised old rows allowed."""
    import hashlib
    import backfill_market_data_tail as tail
    expected_path=f'{r.PREFIX}collector-state/{day[:7]}/{day}-daily-history-tail-backfill-{request_id}-{symbol}.json'
    if path!=expected_path: _fail()
    _immutable_blob(repo,sha,path)
    report=json.loads(a.git(repo,'show',f'{sha}:{path}'))
    expected=dict(mode=tail.MODE,compute_sha=compute_sha,request_id=request_id,trade_date=day,
                  execution_date=day,historical_cutoff=cutoff,store_start_sha=start,
                  fixed_selection=selector,target_count=1,requests_attempted=1,budget_debits=1)
    if any(report.get(k)!=v for k,v in expected.items()): _fail()
    budget_path=f'{r.PREFIX}collector-state/{day[:7]}/{day}-twelve-data-budget.json'
    budget=json.loads(a.git(repo,'show',f'{sha}:{budget_path}'));_one_debit(before,budget)
    required=[dict(path=path,blob_sha=_immutable_blob(repo,sha,path))]
    allowed={path,budget_path}
    old_index,old_rows,_=_immutable_series(repo,start,symbol,h.store)
    index,rows,paths=_immutable_series(repo,sha,symbol,h.store)
    if h.store.canonical_bytes(rows[:len(old_rows)])!=h.store.canonical_bytes(old_rows): _fail()
    prefix=f'{r.PREFIX}series/daily/twelve_data_basic/{symbol}/'
    for shard in old_index['shards']:
        if shard.get('sealed') and _immutable_blob(repo,start,prefix+shard['path'])!=_immutable_blob(repo,sha,prefix+shard['path']):_fail()
    if result is not None and result.get('status')=='PASS':
        if (report.get('status')!='CANDIDATE_VALIDATED' or len(rows)<=len(old_rows) or
            rows[-1]['trade_date']!=cutoff or result.get('appended_count')!=1): _fail()
        mutable={'record_count','last_trade_date','shards','lineage_segments'}
        if {k:v for k,v in index.items() if k not in mutable}!={k:v for k,v in old_index.items() if k not in mutable}: _fail()
        lineage=index.get('lineage_segments',[])
        if lineage[:-1]!=old_index.get('lineage_segments',[]) or len(lineage)!=len(old_index.get('lineage_segments',[]))+1:_fail()
        segment=lineage[-1]
        if (segment.get('ingest_kind')!='collector_price_only_revision_v1' or
            segment.get('volume_policy')!='BLOCKED_UNRESOLVED' or segment.get('start')!=len(old_rows) or segment.get('end')!=len(rows)-1):_fail()
        digest=segment.get('normalized_evidence_sha256')
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest):_fail()
        relative=f'collector-state/{day[:7]}/price-only-{digest}.json'
        if segment.get('normalized_evidence_path')!=relative:_fail()
        evidence_path=r.PREFIX+relative
        # git show returns text; canonical evidence uses no trailing transformation.
        evidence=json.loads(a.git(repo,'show',f'{sha}:{evidence_path}'))
        if hashlib.sha256(h.store.canonical_bytes(evidence)).hexdigest()!=digest:_fail()
        item=dict(symbol=symbol,existing=old_rows,effective=old_index.get('identity_effective_from'),
                  target_records=len(old_rows),outputsize=segment.get('history_outputsize'))
        tail.qualify_tail(h,item,evidence['records'],cutoff,price_only=True)
        if (item['rows']!=rows or item['price_only_evidence'][0]!=digest or
            tail.tail_lineage(item,day,cutoff)!={k:v for k,v in segment.items() if k not in ('start','end')}):_fail()
        required=[dict(path=p,blob_sha=_immutable_blob(repo,sha,p)) for p in sorted(paths|{path,r.INVENTORY,evidence_path})]
        if result.get('verified_required_outputs')!=required:_fail()
        allowed.update(p['path'] for p in required)
    else:
        if index!=old_index or rows!=old_rows:_fail()
        if result is not None:
            if result.get('status')!='NOOP' or report.get('status')!='NOOP':_fail()
        elif report.get('status')!='BLOCKED' or report.get('failure_code')!='replacement_rejected':_fail()
    r.validate_manifest(budget,required,request_id,day,mode=tail.MODE)
    for commit,parent in _linear_commits(repo,start,sha):
        changed=set(filter(None,a.git(repo,'diff','--name-only','-z',parent,commit,'--').split('\0')))
        if not changed<=allowed:_fail('unexpected_change')
    for target in remaining:
        if target['symbol'] not in passed:
            index_path=f"{r.PREFIX}series/daily/twelve_data_basic/{target['symbol']}/_index.json"
            if _immutable_blob(repo,sha,index_path)!=target['index_blob_sha']:_fail('object_drift')
    if a.git(repo,'ls-remote','origin',f'refs/tags/market-data-read/{sha}').split()!=[sha,f'refs/tags/market-data-read/{sha}']:_fail('durability_failed')
    return report


def maintain_append(h,*,compute,compute_sha,repo,store_sha,execution_date,historical_cutoff,
                    request_id,config,universe,access,key,fixed_selector,clock=r.now_utc):
    """Same fixed-target controller, append transactions only; never r.recover.

    Confirmed data rejection can quarantine; unknown request/publication stops.
    No replay, automatic resume, renewed deadline or selector refresh.
    """
    import backfill_market_data_tail as tail
    deadline=time.monotonic()+600; head=store_sha; targets=[];touched=set();passed=set()
    out=dict(mode=tail.MODE,status='BLOCKED',compute_sha=compute_sha,store_start_sha=store_sha,
             execution_date=execution_date,historical_cutoff=historical_cutoff,request_id=request_id,
             transactions=[],requests_attempted=0,budget_debits_confirmed=0,budget_debits_unconfirmed=0,
             published_count=0,quarantined_symbols=[],not_attempted_symbols=[],stop=None,
             last_verified_commit_sha=head,production_recovery_accepted=False,automatic_retry_allowed=False)
    try:
        if config['collector']['twelve_data_daily'].get('bounded_overlap_revision_policy')!='price_only_volume_blocked_v1':_fail('invalid_target')
        if not key or not re.fullmatch(r'\d+-\d+',request_id):_fail('invalid_target')
        maturity=config['collector']['twelve_data_daily']['settlement_maturity']['minimum_next_day_et']
        tail.validate_dates(execution_date,historical_cutoff,maturity,clock());r.source_guard(compute,compute_sha)
        _clean(repo);_remote_exact(repo,head)
        selected=resolve_fixed_targets(h,store_repo=repo,store_sha=head,selector=fixed_selector,universe=universe)
        targets=[dict(symbol=t['symbol'],index_blob_sha=t['index_blob_sha']) for t in selected]
        if not targets or len(targets)>29:_fail('incomplete_targets')
        budget=_budget(repo,execution_date,config)
        if len(targets)>200-budget['credits']:_fail('budget_exhausted')
        plan=dict(out,mode=MODE,trade_date=execution_date,kind='append_only_batch_plan',status='PLANNED',
                  fixed_selection={'schema_version':1,'targets':targets},maximum_provider_requests=len(targets),
                  overall_timeout_seconds=600,volume_policy='BLOCKED_UNRESOLVED')
        head,plan_path=_publish_metadata(repo,head,execution_date,request_id,config,plan,deadline=deadline)
        out.update(batch_manifest_commit_sha=head,batch_manifest_path=plan_path,last_verified_commit_sha=head)
        for target in targets:
            if time.monotonic()>deadline:_fail('deadline_exceeded')
            r.source_guard(compute,compute_sha);tail.validate_dates(execution_date,historical_cutoff,maturity,clock())
            _adopt(repo,head)
            pending=[t for t in targets if t['symbol'] not in passed]
            resolve_fixed_targets(h,store_repo=repo,store_sha=head,selector={'schema_version':1,'targets':pending},universe=universe)
            symbol=target['symbol'];selector={'schema_version':1,'targets':[dict(target)]}
            before=_budget(repo,execution_date,config);touched.add(symbol)
            result=None
            try:
                result=tail.backfill(h,compute=compute,compute_sha=compute_sha,repo=repo,store_sha=head,
                    execution_date=execution_date,historical_cutoff=historical_cutoff,request_id=request_id,
                    config=config,universe=universe,access=access,key=key,fixed_selector=selector,clock=clock,
                    symbol_transaction=symbol,overall_deadline=deadline)
            except Exception as exc:
                summary=r.failure_summary(exc)
                out['requests_attempted']+=summary.get('requests_attempted',0)
                out['budget_debits_confirmed']+=summary.get('budget_debits_confirmed',0)
                out['budget_debits_unconfirmed']+=summary.get('budget_debits_unconfirmed',0)
                receipt=getattr(exc,'confirmed_terminal',None)
                if (not isinstance(exc,r.RecoveryError) or exc.code!='replacement_rejected' or
                    not getattr(exc,'append_data_rejection',False) or type(receipt) is not r.ConfirmedTerminalReceipt or
                    exc.candidate_sha or exc.publication or getattr(exc,'terminal_report_failure',None) or
                    summary.get('requests_attempted')!=1 or summary.get('budget_debits_confirmed')!=1 or
                    summary.get('budget_debits_unconfirmed')!=0):raise
                sha,path=receipt.store_commit_sha,receipt.result_state_path
                status='QUARANTINED'
            else:
                out['requests_attempted']+=result.get('requests_attempted',0)
                out['budget_debits_confirmed']+=result.get('budget_debits',0)
                sha,path=result.get('store_commit_sha'),result.get('result_state_path')
                status=result.get('status')
                if status=='PASS':
                    out['published_count']+=1
                    # Preserve a real child publication even if our next verification fails.
                    out['transactions'].append(dict(symbol=symbol,status='PASS_UNRECONCILED',store_commit_sha=sha))
            _verify_append_terminal(h,repo,start=head,sha=sha,path=path,day=execution_date,cutoff=historical_cutoff,
                request_id=request_id,symbol=symbol,selector=selector,before=before,remaining=targets,
                passed=passed|({symbol} if status=='PASS' else set()),compute_sha=compute_sha,result=result)
            row=dict(symbol=symbol,status=status,store_start_sha=head,store_commit_sha=sha,result_state_path=path)
            if status=='PASS':passed.add(symbol);out['transactions'][-1]=row
            else:out['transactions'].append(row)
            if status=='QUARANTINED':out['quarantined_symbols'].append(symbol)
            head=sha;out['last_verified_commit_sha']=head;_adopt(repo,head)
            checkpoint=dict(out,mode=MODE,trade_date=execution_date,kind='append_only_batch_checkpoint',status='PROGRESS_VALIDATED')
            head,cp=_publish_metadata(repo,head,execution_date,request_id,config,checkpoint,deadline=deadline,symbol=symbol)
            row.update(checkpoint_commit_sha=head,checkpoint_path=cp);out['last_verified_commit_sha']=head
    except Exception as exc:
        out['stop']=r.failure_summary(exc)
    out['not_attempted_symbols']=[t['symbol'] for t in targets if t['symbol'] not in touched]
    if out['stop'] is None and targets:
        out['status']='PASS' if all(t['status'] in ('PASS','NOOP') for t in out['transactions']) else ('PARTIAL' if out['published_count'] else 'BLOCKED')
    elif out['published_count']:out['status']='PARTIAL'
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('trade-date', 'compute-sha', 'store-sha', 'store-repo', 'request-id', 'fixed-selection'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
            os.environ.get('GITHUB_REPOSITORY') != 'yuatom/stock-market-data' or
            os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_SHA') != args.compute_sha):
            _fail('wrong_execution_surface')
        h = a.load_helpers(); root = Path(__file__).resolve().parents[1]
        result = maintain(h, compute=root, compute_sha=args.compute_sha, repo=Path(args.store_repo),
            store_sha=args.store_sha, day=args.trade_date, request_id=args.request_id,
            config=h.collectors.load_yaml(root / 'config/market-data-store.yaml'),
            universe=h.collection.load_collection_universe(root / 'config/collection-universe.json'),
            access=h.collectors.load_yaml(root / 'config/market-data-collector-access.yaml'),
            key=os.environ.get('TWELVE_DATA_API_KEY', ''), fixed_selector=parse_fixed_selection(args.fixed_selection))
    except Exception as exc:
        result = dict(r.failure_summary(exc), mode=MODE, automatic_retry_allowed=False)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
