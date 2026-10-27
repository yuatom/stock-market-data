"""Offline admission tests: real temporary Git remotes, no provider/production I/O."""
from __future__ import annotations

import copy
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
MODE = 'daily_baseline_admission'
DAY = '2026-10-15'
NOW = datetime(2026, 10, 15, 12, 0, 10, tzinfo=timezone.utc)


def load_module():
    assert (ROOT / 'scripts/admit_market_data_baseline.py').is_file(), 'admission implementation is absent'
    return importlib.import_module('admit_market_data_baseline')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


class AdmissionWorkflowTests(unittest.TestCase):
    def workflow(self):
        return yaml.safe_load((ROOT / '.github/workflows/market-data-collector.yml').read_text())

    def test_manual_admission_mode_exists(self):
        w = self.workflow()
        self.assertIn(MODE, w[True]['workflow_dispatch']['inputs']['mode']['options'])

    def test_admission_cannot_enter_normal_writer_job(self):
        w = self.workflow()
        self.assertIn('if', w['jobs']['run'], 'normal writer must exclude admission')
        self.assertIn('daily_baseline_admission', w['jobs']['run']['if'])
        self.assertIn('admission', w['jobs'])

    def test_exact_inputs_and_common_serialization(self):
        w = self.workflow()
        inputs = w[True]['workflow_dispatch']['inputs']
        self.assertIn('expected_compute_sha', inputs)
        self.assertIn('expected_store_sha', inputs)
        job = w['jobs']['admission']
        self.assertEqual(job['concurrency'], {'group': 'market-data-collector', 'cancel-in-progress': False, 'queue': 'max'})
        self.assertIn('workflow_dispatch', job['if'])
        self.assertNotIn('strategy', job)

    def test_schedules_and_ordinary_runtime_are_unchanged(self):
        w = self.workflow()
        self.assertEqual([x['cron'] for x in w[True]['schedule']], [
            '5 7 * * 1-5', '10 7 * * 1-5', '20 7 * * 1-5', '40 9 * * 1-5',
            '55 9 * * 1-5', '25 10 * * 1-5', '6 16 * * 1-5', '21 16 * * 1-5',
            '36 16 * * 1-5', '45 16 * * 1-5', '51 16 * * 1-5'])
        self.assertTrue(all(x['timezone'] == 'America/New_York' for x in w[True]['schedule']))
        self.assertEqual(w['jobs']['run']['uses'], './.github/workflows/market-data-collector-runtime.yml')
        self.assertEqual(w['permissions'], {'contents': 'read'})

    def test_no_provider_in_ci_or_public_artifact_upload(self):
        w = self.workflow()
        job = w['jobs']['admission']
        text = json.dumps(job)
        self.assertNotIn('upload-artifact', text)
        self.assertNotIn('rebase_market_data_baseline.py', text)
        self.assertIn('admit_market_data_baseline.py', text)
        self.assertIn('secrets.TWELVE_DATA_API_KEY', text)
        self.assertIn('secrets.MARKET_DATA_STORE_TOKEN', text)
        self.assertIn('GITHUB_REF', text)
        self.assertIn('GITHUB_SHA', text)


class AdmissionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_target_maturity_and_calendar_validation(self):
        self.m.validate_target(DAY, '07:00', NOW)
        for day in ('2026-09-31', '../x', '2026-10-16', '2026-10-14'):
            with self.subTest(day=day), self.assertRaises(self.m.AdmissionError):
                self.m.validate_target(day, '07:00', NOW)
        with self.assertRaises(self.m.AdmissionError):
            self.m.validate_target(DAY, '07:00', NOW.replace(hour=10))
        self.m.validate_target(DAY, '07:00', NOW.replace(hour=11, minute=0, second=0))

    def test_public_summary_never_contains_private_values(self):
        report = {'status': 'FAIL', 'results': [{'symbol': 'TEST', 'revisions': [{'old': 12345}]}],
                  'failures': [{'symbol': 'TEST', 'code': 'replacement_rejected', 'detail': 'SECRET'}],
                  'compute_sha': 'a'*40, 'store_start_sha': 'b'*40, 'requests_attempted': 1,
                  'budget_debits': 1, 'target_count': 1, 'request_id': '12-1'}
        text = json.dumps(self.m.public_summary(report))
        for private in ('TEST', 'SECRET', '12345', 'revisions'):
            self.assertNotIn(private, text)
        self.assertEqual(json.loads(text)['status'], 'FAIL')

    def test_unknown_error_code_is_not_publicly_echoed(self):
        report = {'status': 'FAIL', 'failures': [{'code': 'https://x?apikey=secret'}]}
        self.assertNotIn('secret', json.dumps(self.m.public_summary(report)))


    def test_invalid_budget_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'budget.json'
            self.m.validate_budget_file(path, DAY, 200, 8)
            for credits in (-1, True, '1', 201):
                path.write_text(json.dumps({'trade_date': DAY, 'credits': credits, 'minutes': {}}))
                with self.subTest(credits=credits), self.assertRaises(self.m.AdmissionError):
                    self.m.validate_budget_file(path, DAY, 200, 8)
            path.write_text(json.dumps({'trade_date': DAY, 'credits': 1, 'minutes': {'bad': -1}}))
            with self.assertRaises(self.m.AdmissionError):
                self.m.validate_budget_file(path, DAY, 200, 8)

    def test_budget_persistence_is_before_network(self):
        calls = []
        writer = SimpleNamespace(publish=lambda paths: calls.append('durable'), budget_relative='budget.json')
        helpers = SimpleNamespace(collectors=SimpleNamespace(
            wait_for_twelve_budget=lambda *a, **k: calls.append('wait'),
            consume_twelve_credit=lambda *a, **k: calls.append('debit')))
        self.m.debit_before_request(helpers, writer, Path('/unused'), DAY,
            {'hard_credits_per_day': 200, 'hard_credits_per_minute': 8}, clock=lambda: NOW)
        calls.append('http')
        self.assertEqual(calls, ['wait', 'debit', 'durable', 'http'])

    def test_budget_push_failure_does_not_reach_request(self):
        def fail(paths):
            raise self.m.AdmissionError('publication_failed')
        h = SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=Mock(), consume_twelve_credit=Mock()))
        writer = SimpleNamespace(publish=fail, budget_relative='budget.json')
        with self.assertRaises(self.m.AdmissionError):
            self.m.debit_before_request(h, writer, Path('/unused'), DAY,
                {'hard_credits_per_day': 200, 'hard_credits_per_minute': 8}, clock=lambda: NOW)
        h.collectors.consume_twelve_credit.assert_called_once()

    def test_expired_debit_minute_stops_without_refund(self):
        h = SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=Mock(), consume_twelve_credit=Mock()))
        current = [NOW]
        def publish(paths):
            current[0] = NOW.replace(minute=1)
        writer = SimpleNamespace(publish=Mock(side_effect=publish), budget_relative='budget.json')
        with self.assertRaises(self.m.AdmissionError) as ctx:
            self.m.debit_before_request(h, writer, Path('/unused'), DAY,
                {'hard_credits_per_day': 200, 'hard_credits_per_minute': 8}, clock=lambda: current[0])
        self.assertEqual(ctx.exception.code, 'budget_slot_expired')
        writer.publish.assert_called_once()


class AdmissionPublisherTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.repo = self.root / 'store'
        git(self.root, 'init', '--bare', str(self.remote))
        git(self.root, 'clone', str(self.remote), str(self.repo))
        git(self.repo, 'config', 'user.name', 'offline-test')
        git(self.repo, 'config', 'user.email', 'offline@example.invalid')
        self.facts = self.repo / 'data/market-data/series/daily/test/TEST/_index.json'
        self.facts.parent.mkdir(parents=True)
        self.facts.write_text('{"synthetic":true}\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'synthetic baseline')
        git(self.repo, 'branch', '-M', 'main')
        git(self.repo, 'push', 'origin', 'main')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.verify = Mock()
        self.p = self.m.StorePublisher(self.repo, self.base, DAY, '12-1', verify=self.verify)
        self.budget = self.repo / self.p.budget_relative
        self.budget.parent.mkdir(parents=True, exist_ok=True)

    def test_budget_only_commit_and_exact_remote_readback(self):
        self.budget.write_text('{"credits":1}\n')
        sha = self.p.publish([self.p.budget_relative])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), sha)
        self.assertEqual(git(self.repo, 'diff', '--name-only', self.base, sha), self.p.budget_relative)
        self.verify.assert_called_once_with(self.repo, sha)
        self.assertEqual(self.facts.read_text(), '{"synthetic":true}\n')

    def test_unexpected_fact_edit_prevents_any_remote_push(self):
        self.budget.write_text('{}')
        self.facts.write_text('modified')
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish([self.p.budget_relative])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.base)

    def test_deleted_fact_is_not_ignored_by_allowlist(self):
        self.budget.write_text('{}')
        self.facts.unlink()
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish([self.p.budget_relative])

    def test_untracked_and_ignored_files_are_not_silent(self):
        self.budget.write_text('{}')
        other = self.repo / 'unexpected.json'
        other.write_text('{}')
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish([self.p.budget_relative])

    def test_symlink_budget_is_rejected(self):
        outside = self.root / 'outside'
        outside.write_text('secret')
        self.budget.symlink_to(outside)
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish([self.p.budget_relative])
        self.assertEqual(outside.read_text(), 'secret')

    def test_out_of_scope_path_cannot_be_explicitly_published(self):
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish(['README.md'])

    def test_remote_drift_prevents_budget_push(self):
        other = self.root / 'other'
        git(self.root, 'clone', '--branch', 'main', str(self.remote), str(other))
        git(other, 'config', 'user.name', 'other')
        git(other, 'config', 'user.email', 'other@example.invalid')
        (other / 'other.txt').write_text('concurrent')
        git(other, 'add', '.')
        git(other, 'commit', '-m', 'concurrent writer')
        git(other, 'push', 'origin', 'main')
        remote_sha = git(other, 'rev-parse', 'HEAD')
        self.budget.write_text('{}')
        with self.assertRaises(self.m.AdmissionError):
            self.p.publish([self.p.budget_relative])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), remote_sha)

    def test_wrong_start_pin_and_dirty_checkout_are_rejected(self):
        with self.assertRaises(self.m.AdmissionError):
            self.m.StorePublisher(self.repo, 'a'*40, DAY, '22-1', verify=self.verify)
        (self.repo / 'unexpected').write_text('dirty')
        with self.assertRaises(self.m.AdmissionError):
            self.m.StorePublisher(self.repo, self.base, DAY, '22-1', verify=self.verify)

    def test_unique_report_cannot_be_reused(self):
        report = self.repo / self.p.report_relative
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('{"status":"PASS"}')
        self.p.publish([self.p.report_relative])
        head = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaises(self.m.AdmissionError):
            self.m.StorePublisher(self.repo, head, DAY, '12-1', verify=self.verify)


class AdmissionExecutionTests(unittest.TestCase):
    def setUp(self):
        AdmissionPublisherTests.setUp(self)
        self.fetch = Mock(return_value=[])
        self.now = NOW
        self.compute_head = 'a'*40
        self.before_transport = lambda: None
        self.before_compute_read = lambda: None
        self.cfg = {'collector': {'twelve_data_daily': {'settlement_maturity': {'minimum_next_day_et': '07:00'}},
                                  'budgets': {'twelve_data_basic': {'hard_credits_per_day': 200, 'hard_credits_per_minute': 8}}},
                    'series': {'baseline_initialization': {'explicit_rebase': {
                        'maximum_revised_overlap_records': 3,
                        'revised_overlap_must_be_within_last_existing_sessions': 3}}}}
        def debit(*a, **k):
            state = json.loads(self.budget.read_text()) if self.budget.exists() else {'credits': 0}
            state['credits'] += 1
            state['trade_date'] = DAY
            minute = self.now.strftime('%Y-%m-%dT%H:%MZ')
            state.setdefault('minutes', {})[minute] = state.get('minutes', {}).get(minute, 0) + 1
            self.budget.write_text(json.dumps(state))
        class Exhausted(RuntimeError):
            pass
        def strict_fetch(*args, before_request=None, **kwargs):
            self.before_transport()
            if before_request is not None:
                before_request()
            return self.fetch(*args, **kwargs)
        self.h = SimpleNamespace(collectors=SimpleNamespace(wait_for_twelve_budget=Mock(),
                    consume_twelve_credit=Mock(side_effect=debit), BudgetExceeded=Exhausted),
                    rebaser=SimpleNamespace(_fetch_replacement_daily=strict_fetch))
        self.candidates = [{'symbol': 'TEST', 'outputsize': 300, 'effective': None}]

    def execute(self, key='unit-test-only'):
        real_git = self.m.git
        def wrapped_git(repo, *args):
            if repo == Path('/synthetic-compute'):
                if args == ('rev-parse', 'HEAD'):
                    return 'a'*40
                if args[0] == 'status':
                    return ''
                self.before_compute_read()
                return self.compute_head + '\trefs/heads/main'
            return real_git(repo, *args)
        with patch.object(self.m, 'prepare_series', return_value=self.candidates), \
             patch.object(self.m, 'compare_replacement', return_value={'symbol': 'TEST', 'status': 'PASS'}), \
             patch.object(self.m, 'git', side_effect=wrapped_git):
            return self.m.execute(self.h, self.p, compute=Path('/synthetic-compute'), compute_sha='a'*40,
                day=DAY, request_id='12-1', config=self.cfg, universe={}, access={}, key=key, clock=lambda: self.now)

    def test_secret_missing_causes_no_write_or_fetch(self):
        with self.assertRaises(self.m.AdmissionError) as ctx:
            self.execute(key='')
        self.assertEqual(ctx.exception.code, 'credential_missing')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), self.base)
        self.fetch.assert_not_called()

    def test_success_uses_durable_debit_and_only_diagnostic_paths(self):
        def response(*args, **kwargs):
            remote = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))
            self.assertEqual(remote['credits'], 1)
            return []
        self.fetch.side_effect = response
        result, sha = self.execute()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['budget_debits'], 1)
        self.assertEqual(result['requests_attempted'], 1)
        paths = set(git(self.repo, 'diff', '--name-only', self.base, sha).splitlines())
        self.assertEqual(paths, self.p.allowed)
        self.assertEqual(git(self.remote, 'show', 'refs/heads/main:'+str(self.facts.relative_to(self.repo))), '{"synthetic":true}')

    def test_provider_failure_is_nonpass_and_budget_remains_durable(self):
        self.fetch.side_effect = RuntimeError('https://provider.invalid/?apikey=SECRET')
        result, sha = self.execute()
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['requests_attempted'], 1)
        report = git(self.remote, 'show', sha+':'+self.p.report_relative)
        self.assertNotIn('SECRET', report)
        self.assertEqual(json.loads(git(self.remote, 'show', sha+':'+self.p.budget_relative))['credits'], 1)

    def test_interruption_leaves_predebit_and_no_final_pass(self):
        self.fetch.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        report = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.report_relative))
        self.assertEqual(report['status'], 'IN_PROGRESS')
        state = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))
        self.assertEqual(state['credits'], 1)

    def test_budget_exhaustion_does_not_issue_http(self):
        self.h.collectors.wait_for_twelve_budget.side_effect = self.h.collectors.BudgetExceeded()
        result, _ = self.execute()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['failures'][0]['code'], 'budget_exhausted')
        self.fetch.assert_not_called()

    def test_unexpected_mutation_never_publishes_market_facts(self):
        def bad_fetch(*a, **k):
            self.facts.write_text('bad mutation')
            return []
        self.fetch.side_effect = bad_fetch
        with self.assertRaises(self.m.AdmissionError):
            self.execute()
        self.assertEqual(git(self.remote, 'show', 'refs/heads/main:'+str(self.facts.relative_to(self.repo))), '{"synthetic":true}')



class AdmissionCanonicalTests(unittest.TestCase):
    """Real unchanged rebaser/Store/collector imports, required in full repository CI."""
    def setUp(self):
        self.m = load_module()
        self.h = self.m.load_helpers()
        self.rows = [dict(trade_date=f'2026-10-{d:02}', open=10., high=11., low=9., close=10., volume=1.) for d in range(1, 15)]
        self.candidate = {'symbol': 'TEST', 'existing': copy.deepcopy(self.rows[:10]), 'effective': None,
                          'outputsize': 100, 'target_records': 10}
        self.cfg = {'maximum_revised_overlap_records': 3, 'revised_overlap_must_be_within_last_existing_sessions': 3}

    def test_canonical_readonly_comparison_preserves_dates_and_counts(self):
        r = self.m.compare_replacement(self.h, self.candidate, self.rows, DAY, self.cfg)
        self.assertEqual(r['status'], 'PASS')
        self.assertEqual(r['new_records'], 4)
        self.assertEqual(r['last_trade_date'], '2026-10-14')
        self.assertEqual(self.candidate['existing'], self.rows[:10])

    def test_canonical_old_revision_and_missing_old_date_rejected(self):
        for change in ('revision', 'missing'):
            rows = copy.deepcopy(self.rows)
            if change == 'revision':
                rows[0]['close'] = 12.
            else:
                rows.pop(0)
            with self.subTest(change=change), self.assertRaises(self.m.AdmissionError):
                self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)

    def test_canonical_tail_revision_diff_is_private_result(self):
        rows = copy.deepcopy(self.rows)
        rows[9]['close'] = 10.5
        r = self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(len(r['revisions']), 1)
        self.assertEqual(r['revisions'][0]['changed_fields']['close'], {'old': 10., 'new': 10.5})

    def test_canonical_nonfinite_negative_and_duplicate_records_rejected(self):
        for value in ('nan', 'negative', 'duplicate'):
            rows = copy.deepcopy(self.rows)
            if value == 'nan':
                rows[-1]['close'] = float('nan')
            elif value == 'negative':
                rows[-1]['volume'] = -1.
            else:
                rows.append(copy.deepcopy(rows[-1]))
            with self.subTest(value=value), self.assertRaises(self.m.AdmissionError):
                self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)


    def test_canonical_request_guard_runs_after_preparation_before_http(self):
        access = {'twelve_data_basic': {'interval_daily': '1day', 'base_url': 'https://example.invalid',
                                      'time_series_path': '/time_series'},
                  'http': {'timeout_seconds': 5, 'user_agent': 'synthetic-test'}}
        calls = []
        urlencode = self.h.rebaser.urllib.parse.urlencode
        def prepare(value):
            calls.append('prepare')
            return urlencode(value)
        def guard():
            self.assertEqual(calls, ['prepare'])
            calls.append('guard')
        def http(*args, **kwargs):
            self.assertEqual(calls, ['prepare', 'guard'])
            calls.append('http')
            return {'values': []}
        with patch.object(self.h.rebaser.urllib.parse, 'urlencode', side_effect=prepare), \
             patch.object(self.h.collectors, '_http_json', side_effect=http):
            self.h.rebaser._fetch_replacement_daily('TEST', 'synthetic-key', access,
                outputsize=10, trade_date=DAY, effective=None, before_request=guard)
        self.assertEqual(calls, ['prepare', 'guard', 'http'])

    def test_canonical_request_guard_failure_never_enters_http(self):
        access = {'twelve_data_basic': {'interval_daily': '1day', 'base_url': 'https://example.invalid',
                                      'time_series_path': '/time_series'},
                  'http': {'timeout_seconds': 5, 'user_agent': 'synthetic-test'}}
        guard = Mock(side_effect=self.m.AdmissionError('object_drift'))
        with patch.object(self.h.collectors, '_http_json') as http, self.assertRaises(self.m.AdmissionError):
            self.h.rebaser._fetch_replacement_daily('TEST', 'synthetic-key', access,
                outputsize=10, trade_date=DAY, effective=None, before_request=guard)
        guard.assert_called_once_with()
        http.assert_not_called()

    def test_canonical_reader_without_guard_remains_compatible(self):
        access = {'twelve_data_basic': {'interval_daily': '1day', 'base_url': 'https://example.invalid',
                                      'time_series_path': '/time_series'},
                  'http': {'timeout_seconds': 5, 'user_agent': 'synthetic-test'}}
        with patch.object(self.h.collectors, '_http_json', return_value={'values': []}) as http:
            self.assertEqual(self.h.rebaser._fetch_replacement_daily('TEST', 'synthetic-key', access,
                outputsize=10, trade_date=DAY, effective=None), [])
        http.assert_called_once()


class AdmissionRequestRaceTests(unittest.TestCase):
    setUp = AdmissionExecutionTests.setUp
    execute = AdmissionExecutionTests.execute

    def assert_unused_debit(self, result):
        self.fetch.assert_not_called()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['budget_debits'], 1)
        self.assertEqual(result['requests_attempted'], 0)
        ledger = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))
        self.assertEqual(ledger['credits'], 1)
        self.assertEqual(ledger['minutes'][NOW.strftime('%Y-%m-%dT%H:%MZ')], 1)
        self.assertEqual(set(git(self.repo, 'diff', '--name-only', self.base, self.p.head).splitlines()), self.p.allowed)
        self.assertEqual(self.facts.read_text(), '{"synthetic":true}\n')

    def test_actual_debit_minute_is_taken_from_ledger_delta(self):
        original = self.h.collectors.consume_twelve_credit.side_effect
        def debit(*args, **kwargs):
            self.now = NOW.replace(minute=1)
            return original(*args, **kwargs)
        self.h.collectors.consume_twelve_credit.side_effect = debit
        result, _ = self.execute()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['budget_debits'], 1)
        self.assertEqual(result['requests_attempted'], 1)
        ledger = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))
        self.assertEqual(ledger['minutes'], {self.now.strftime('%Y-%m-%dT%H:%MZ'): 1})

    def test_p1_source_drift_after_durable_predebit_blocks_http(self):
        original = self.m.debit_before_request
        def debit(*args, **kwargs):
            proof = original(*args, **kwargs)
            self.compute_head = 'b'*40
            return proof
        with patch.object(self.m, 'debit_before_request', side_effect=debit):
            result, _ = self.execute()
        self.assert_unused_debit(result)
        self.assertEqual(result['failures'][0]['code'], 'object_drift')

    def test_p1_source_drift_during_request_preparation_blocks_http(self):
        self.before_transport = lambda: setattr(self, 'compute_head', 'b'*40)
        result, _ = self.execute()
        self.assert_unused_debit(result)
        self.assertEqual(result['failures'][0]['code'], 'object_drift')

    def test_p1_minute_rollover_during_request_preparation_blocks_http(self):
        self.before_transport = lambda: setattr(self, 'now', NOW.replace(minute=1))
        result, _ = self.execute()
        self.assert_unused_debit(result)
        self.assertEqual(result['failures'][0]['code'], 'budget_slot_expired')

    def test_p1_slow_final_source_read_cannot_reuse_expired_slot(self):
        def delay():
            if self.budget.exists() and json.loads(self.budget.read_text())['credits'] == 1:
                self.now = NOW.replace(minute=1)
        self.before_compute_read = delay
        result, _ = self.execute()
        self.assert_unused_debit(result)
        self.assertEqual(result['failures'][0]['code'], 'budget_slot_expired')

    def test_p1_dirty_debit_after_predebit_is_not_published_or_used(self):
        def tamper():
            state = json.loads(self.budget.read_text())
            state['credits'] = 99
            self.budget.write_text(json.dumps(state))
        self.before_transport = tamper
        with self.assertRaises(self.m.AdmissionError):
            self.execute()
        self.fetch.assert_not_called()
        ledger = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))
        self.assertEqual(ledger['credits'], 1)
        report = json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.report_relative))
        self.assertEqual(report['status'], 'IN_PROGRESS')

    def test_p1_remote_store_drift_stops_http_and_final_publication(self):
        remote_sha = []
        def drift():
            other = self.root / 'concurrent'
            git(self.root, 'clone', '--branch', 'main', str(self.remote), str(other))
            git(other, 'config', 'user.name', 'synthetic-writer')
            git(other, 'config', 'user.email', 'synthetic@example.invalid')
            (other / 'concurrent.txt').write_text('synthetic')
            git(other, 'add', '.')
            git(other, 'commit', '-m', 'synthetic concurrent advance')
            git(other, 'push', 'origin', 'main')
            remote_sha.append(git(other, 'rev-parse', 'HEAD'))
        self.before_transport = drift
        with self.assertRaises(self.m.AdmissionError):
            self.execute()
        self.fetch.assert_not_called()
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/main'), remote_sha[0])
        self.assertEqual(json.loads(git(self.remote, 'show', 'refs/heads/main:'+self.p.budget_relative))['credits'], 1)


class AdmissionRequiredClosureTests(unittest.TestCase):
    setUp = AdmissionExecutionTests.setUp
    execute = AdmissionExecutionTests.execute

    def test_p1_real_verifier_receives_nonempty_exact_diagnostic_closure(self):
        self.p.verify = self.m.verify_durability
        outputs = []
        real_run = self.m._run
        def capture(argv, **kwargs):
            value = real_run(argv, **kwargs)
            if 'verify_store_publication.py' in ' '.join(argv):
                outputs.append((argv, json.loads(value)))
            return value
        with patch.object(self.m, '_run', side_effect=capture):
            result, sha = self.execute()
        self.assertEqual(result['status'], 'PASS')
        argv, receipt = outputs[-1]
        expected_blob = git(self.remote, 'rev-parse', sha+':'+self.p.report_relative)
        self.assertIn('--required-manifest', argv)
        self.assertEqual(receipt['verified_required_outputs'], [{'path': self.p.report_relative, 'blob_sha': expected_blob}])
        self.assertEqual(receipt['store_commit_sha'], sha)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/tags/market-data-read/'+sha), sha)
        manifest = json.loads(git(self.remote, 'show', sha+':'+self.p.budget_relative))
        self.assertEqual((manifest['request_id'], manifest['trade_date'], manifest['stage']), ('12-1', DAY, MODE))
        self.assertEqual(manifest['credits'], 1)
        self.assertEqual(manifest['minutes'][NOW.strftime('%Y-%m-%dT%H:%MZ')], 1)
        self.assertEqual(set(git(self.repo, 'diff', '--name-only', self.base, sha).splitlines()), self.p.allowed)

    def corrupt_manifest(self, kind):
        self.p.verify = self.m.verify_durability
        result, sha = self.execute()
        manifest = json.loads(self.budget.read_text())
        if kind == 'missing':
            manifest['required_outputs'][0]['path'] = 'collector-state/missing.json'
        elif kind == 'wrong_blob':
            manifest['required_outputs'][0]['blob_sha'] = '0'*40
        elif kind == 'wrong_path':
            manifest['required_outputs'][0] = {'path': str(self.facts.relative_to(self.repo / 'data/market-data')),
                                               'blob_sha': git(self.repo, 'rev-parse', sha+':'+str(self.facts.relative_to(self.repo)))}
        elif kind == 'wrong_request':
            manifest['request_id'] = '99-1'
        elif kind == 'wrong_date':
            manifest['trade_date'] = '2026-10-14'
        elif kind == 'wrong_stage':
            manifest['stage'] = 'other_mode'
        elif kind == 'wrong_report_identity':
            report_path = self.repo / self.p.report_relative
            doc = json.loads(report_path.read_text())
            doc['request_id'] = '99-1'
            report_path.write_text(json.dumps(doc))
            manifest['required_outputs'][0]['blob_sha'] = git(self.repo, 'hash-object', str(report_path))
        self.budget.write_text(json.dumps(manifest))
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'synthetic invalid closure')
        candidate = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'push', 'origin', 'main')
        return candidate

    def test_required_closure_missing_output_rejected_by_real_helper(self):
        self.assert_bad_closure('missing')

    def test_required_closure_wrong_blob_rejected_by_real_helper(self):
        self.assert_bad_closure('wrong_blob')

    def test_required_closure_wrong_existing_path_rejected(self):
        self.assert_bad_closure('wrong_path')

    def test_required_closure_wrong_request_rejected(self):
        self.assert_bad_closure('wrong_request')

    def test_required_closure_wrong_date_rejected(self):
        self.assert_bad_closure('wrong_date')

    def test_required_closure_wrong_stage_rejected(self):
        self.assert_bad_closure('wrong_stage')

    def test_required_closure_wrong_report_identity_rejected(self):
        self.assert_bad_closure('wrong_report_identity')

    def assert_bad_closure(self, kind):
        candidate = self.corrupt_manifest(kind)
        expected_blob = git(self.repo, 'rev-parse', candidate+':'+self.p.report_relative)
        with self.assertRaises(self.m.AdmissionError) as ctx:
            self.m.verify_durability(self.repo, candidate, required_manifest=self.p.budget_relative,
                expected_report={'path': self.p.report_relative, 'blob_sha': expected_blob,
                                 'request_id': '12-1', 'trade_date': DAY, 'mode': MODE})
        self.assertEqual(ctx.exception.code, 'durability_failed')

    def test_required_closure_failure_never_returns_final_pass(self):
        real_verify = self.m.verify_durability
        def fail_final(repo, sha, **kwargs):
            report = json.loads(git(repo, 'show', sha+':'+self.p.report_relative))
            if report['status'] == 'PASS':
                raise self.m.AdmissionError('durability_failed')
            return real_verify(repo, sha, **kwargs)
        self.p.verify = fail_final
        with self.assertRaises(self.m.AdmissionError) as ctx:
            self.execute()
        self.assertEqual(ctx.exception.code, 'durability_failed')
        self.assertEqual(self.fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
