"""Value-free rejection persistence with fictional rows and local Git fixtures."""
import json
import unittest
from unittest.mock import patch

from test_guarded_history_recovery import RecoveryIntegrationFixture, git
import replacement_diagnostics as diagnostic


class ValueFreePersistenceTests(RecoveryIntegrationFixture, unittest.TestCase):
    def block_first_row(self):
        self.config['series']['baseline_initialization']['explicit_rebase'][
            'revised_overlap_must_be_within_last_existing_sessions'] = 1
        self.new[0]['close'] = 11.56789
        with self.assertRaises(Exception) as caught:
            self.run_recovery()
        summary = self.m.failure_summary(caught.exception)
        self.assertEqual(summary['failure_code'], 'replacement_rejected')
        self.assertEqual(summary['failed_symbol'], 'AAA')
        self.assertEqual(summary['replacement_rejection']['rejection_rule'], 'revision_outside_tail')
        self.assertEqual(summary['terminal_report_outcome'], 'CONFIRMED')
        self.assertEqual(summary['budget_debits_confirmed'], 1)
        self.assertEqual(summary['budget_debits_unconfirmed'], 0)
        self.assertEqual(summary['requests_attempted'], 1)
        self.assertEqual(self.http_count, 1)
        head = git(self.store_remote, 'rev-parse', 'refs/heads/main')
        report_path = 'data/market-data/collector-state/2026-10/2026-10-18-daily-baseline-recovery-123-1.json'
        report = json.loads(git(self.repo, 'show', head + ':' + report_path))
        budget = self.current_budget()
        self.assertEqual(budget['credits'], 1)
        self.assertEqual(budget['required_outputs'], [{
            'path': report_path.removeprefix('data/market-data/'),
            'blob_sha': git(self.repo, 'rev-parse', head + ':' + report_path)}])
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/tags/market-data-read/' + head), head)
        self.assertEqual(report['replacement_rejection'], summary['replacement_rejection'])
        self.assertEqual(report['status'], 'BLOCKED')
        self.assert_no_remote_facts()
        for symbol in ('AAA', 'BBB'):
            path = f'data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json'
            self.assertEqual(git(self.repo, 'rev-parse', head + ':' + path),
                             git(self.repo, 'rev-parse', self.base + ':' + path))
        for value in (report, summary):
            for secret in ('11.56789', 'SECRET_CANARY', 'old_canonical_sha256', 'changed_fields'):
                self.assertNotIn(secret, json.dumps(value))
        return summary

    def test_first_rejected_field_reaches_durable_report_without_fact_changes(self):
        summary = self.block_first_row()
        self.assertEqual(summary['replacement_rejection'].get('revision_difference'), {
            'scope': 'first_rejected_row', 'fields': [
                {'field': 'close', 'kind': 'value_changed', 'old_type': 'float', 'new_type': 'float'}],
            'unlisted_field_difference': False})

    def test_failed_diagnostic_keeps_durable_original_rejection(self):
        with patch.object(diagnostic, 'summarize_revision_difference', side_effect=RuntimeError('SECRET_CANARY')):
            summary = self.block_first_row()
        self.assertNotIn('revision_difference', summary['replacement_rejection'])
