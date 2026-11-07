"""Batch budget preflight against synthetic histories and temporary Git remotes."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_guarded_history_recovery as recovery


class BatchBudgetTests(recovery.RecoveryIntegrationFixture, unittest.TestCase):
    def test_thirteen_targets_with_eight_remaining_block_before_any_credit_or_http(self):
        for symbol in [f'C{i:02}' for i in range(11)]:
            self.universe['daily_series'].append({'symbol': symbol, 'asset_class': 'etf'})
            self.h.store.append_daily_bars(
                self.repo / 'data/market-data', provider='twelve_data_basic',
                symbol=symbol, asset_class='etf', bars=self.old,
                series_semantics='daily_regular_ohlcv', shard_record_limit=2,
            )
        self.seed_budget(192)
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 192)
        self.assertEqual(self.current_budget()['minutes'], {})
        progress = caught.exception.recovery_progress
        self.assertEqual(progress['budget_debits'], 0)
        self.assertEqual(progress['requests_attempted'], 0)
        self.assertEqual(progress['terminal_report_outcome'], 'CONFIRMED')
        head = recovery.git(self.store_remote, 'rev-parse', 'refs/heads/main')
        report_path = (f'data/market-data/collector-state/{recovery.DAY[:7]}/'
                       f'{recovery.DAY}-daily-baseline-recovery-123-1.json')
        report = json.loads(recovery.git(self.repo, 'show', f'{head}:{report_path}'))
        self.assertEqual((report['status'], report['failure_code']), ('BLOCKED', 'budget_exhausted'))
        self.assert_no_remote_facts()

    def test_exact_remaining_daily_credits_are_allowed(self):
        self.seed_budget(198)
        result = self.run_recovery()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['budget_debits'], 2)
        self.assertEqual(self.http_count, 2)
        self.assertEqual(self.current_budget()['credits'], 200)

    def test_absent_ledger_keeps_existing_zero_credit_initialization(self):
        result = self.run_recovery()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(self.current_budget()['credits'], 2)

    def test_retained_old_minute_bucket_does_not_become_batch_daily_shortage(self):
        # The existing debit helper owns waiting; an old full minute must not
        # be compared with the number of targets by the new daily preflight.
        self.seed_budget(198, {'2026-10-18T11:59Z': 8})
        result = self.run_recovery()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(self.current_budget()['credits'], 200)

    def test_malformed_minute_state_is_rejected_without_spending(self):
        self.seed_budget(190, {'2026-10-18T11:59Z': 9})
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_state_invalid')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 190)

    def test_over_limit_state_is_invalid_instead_of_batch_exhaustion(self):
        self.seed_budget(201)
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_state_invalid')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 201)

    def test_exhausted_daily_budget_spends_nothing(self):
        self.seed_budget(200)
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 200)

    def test_absent_ledger_and_batch_above_daily_limit_spends_nothing(self):
        self.config['collector']['budgets']['twelve_data_basic']['hard_credits_per_day'] = 1
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 0)

    def test_batch_may_exceed_per_minute_limit_and_use_separate_minutes(self):
        self.seed_budget(198)
        self.config['collector']['budgets']['twelve_data_basic']['hard_credits_per_minute'] = 1
        current = [recovery.NOW]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0] if tz is None else current[0].astimezone(tz)
        def payload_and_advance(url, **kwargs):
            value = self.payload(url, **kwargs)
            current[0] += timedelta(minutes=1)
            return value
        with patch.object(self.h.collectors, 'datetime', Clock), \
             patch.object(self.h.collectors, '_http_json', side_effect=payload_and_advance):
            result = self.m.recover(
                self.h, compute=self.compute, compute_sha=self.compute_sha,
                repo=self.repo, store_sha=self.base, day=recovery.DAY, request_id='123-1',
                config=self.config, universe=self.universe, access=self.access,
                key='SYNTHETIC', clock=lambda: current[0],
            )
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(self.current_budget()['credits'], 200)
        self.assertEqual(self.current_budget()['minutes'],
                         {'2026-10-18T12:00Z': 1, '2026-10-18T12:01Z': 1})

    def test_terminal_diagnostic_failure_does_not_replace_batch_exhaustion(self):
        self.seed_budget(199)
        original = self.m.make_writer
        calls = []
        def failing_writer(*args, **kwargs):
            writer = original(*args, **kwargs)
            original_report = writer.report
            def fail_terminal(report):
                calls.append(report['status'])
                if report['status'] == 'BLOCKED':
                    raise self.m.RecoveryError('publication_failed')
                return original_report(report)
            writer.report = fail_terminal
            return writer
        with patch.object(self.m, 'make_writer', failing_writer):
            with self.assertRaises(Exception) as caught:
                self.run_recovery()
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(caught.exception.recovery_progress['terminal_report_outcome'], 'UNCONFIRMED')
        self.assertEqual(caught.exception.terminal_report_failure['failure_code'], 'publication_failed')
        self.assertEqual(calls, ['IN_PROGRESS', 'BLOCKED'])
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 199)

    def test_provider_failure_retains_only_the_existing_single_predebit(self):
        self.seed_budget(198)
        def fail_provider(url, **kwargs):
            self.http_count += 1
            raise RuntimeError('synthetic provider failure')
        with self.assertRaises(Exception):
            self.run_recovery(fail_provider)
        self.assertEqual(self.http_count, 1)
        self.assertEqual(self.current_budget()['credits'], 199)
        self.assert_no_remote_facts()

    def test_first_target_source_drift_still_precedes_batch_shortage(self):
        self.seed_budget(199)
        original = self.m.source_guard
        calls = []
        def drift_after_initial_check(*args):
            calls.append(args)
            if len(calls) == 2:
                raise self.m.RecoveryError('object_drift')
            return original(*args)
        with patch.object(self.m, 'source_guard', drift_after_initial_check):
            with self.assertRaises(Exception) as caught:
                self.run_recovery()
        self.assertEqual(caught.exception.code, 'object_drift')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 199)

    def test_first_target_date_check_still_precedes_batch_shortage(self):
        self.seed_budget(199)
        calls = []
        def clock():
            calls.append(None)
            return recovery.NOW if len(calls) <= 2 else recovery.NOW + timedelta(days=1)
        with patch.object(self.h.collectors, '_http_json', return_value={'values': []}) as http, \
             self.assertRaises(Exception) as caught:
            self.m.recover(
                self.h, compute=self.compute, compute_sha=self.compute_sha,
                repo=self.repo, store_sha=self.base, day=recovery.DAY, request_id='123-1',
                config=self.config, universe=self.universe, access=self.access,
                key='SYNTHETIC', clock=clock,
            )
        self.assertEqual(caught.exception.code, 'invalid_target')
        http.assert_not_called()
        self.assertEqual(self.http_count, 0)
        self.assertEqual(self.current_budget()['credits'], 199)
