"""Synthetic replacement failures: no real provider, secrets or Store inputs."""
import copy
import json
import unittest
from unittest.mock import patch
from test_daily_baseline_admission import AdmissionCanonicalTests, DAY
from test_guarded_history_recovery import RecoveryIntegrationFixture, git


class ReplacementRuleTests(AdmissionCanonicalTests):
    def rejection(self, rows, *, candidate=None, limits=None):
        with self.assertRaises(self.m.AdmissionError) as caught:
            self.m.compare_replacement(self.h, candidate or self.candidate, rows, DAY, limits or self.cfg)
        return caught.exception

    def test_exact_executed_rules_without_values(self):
        cases = []
        cases.append(([], 'insufficient_replacement_records'))
        cases.append((self.rows + [self.rows[-1]], 'duplicate_replacement_dates'))
        cases.append((self.rows[1:], 'missing_existing_dates'))
        changed = copy.deepcopy(self.rows); changed[0]['close'] = 10.5
        cases.append((changed, 'revision_outside_tail'))
        bad = copy.deepcopy(self.rows); bad[0]['volume'] = -1
        cases.append((bad, 'invalid_replacement_bar'))
        for rows, rule in cases:
            with self.subTest(rule=rule):
                evidence = self.rejection(rows).evidence
                self.assertEqual(evidence.get('rejection_rule'), rule)
                self.assertFalse(set(evidence) & {'overlap_differences', 'changed_fields', 'open', 'volume'})
        changed = copy.deepcopy(self.rows)
        for row in changed[6:10]: row['close'] = 10.5
        evidence = self.rejection(changed, limits=dict(self.cfg, revised_overlap_must_be_within_last_existing_sessions=4)).evidence
        self.assertEqual(evidence.get('rejection_rule'), 'revised_overlap_limit')
        self.assertEqual(evidence.get('revised_overlap_records'), 4)

    def test_arbitrary_exception_never_claims_a_rule(self):
        with patch.object(self.h.rebaser, 'validate_replacement', side_effect=RuntimeError('TOKEN close=10.5')):
            evidence = self.rejection(self.rows).evidence
        self.assertEqual(evidence.get('rejection_rule'), 'unclassified')
        self.assertNotIn('TOKEN', json.dumps(evidence))

    def test_first_executed_gate_wins(self):
        rows = copy.deepcopy(self.rows[1:]); rows.append(rows[-1])
        self.assertEqual(self.rejection(rows).evidence.get('rejection_rule'), 'duplicate_replacement_dates')
        rows = copy.deepcopy(self.rows)
        for row in rows[:5]: row['close'] = 10.5
        self.assertEqual(self.rejection(rows).evidence.get('rejection_rule'), 'revision_outside_tail')


class ReplacementRecoveryTests(RecoveryIntegrationFixture, unittest.TestCase):
    def test_symbol_and_rule_reach_terminal_report_and_stdout(self):
        self.new.pop(0)
        with self.assertRaises(Exception) as caught: self.run_recovery()
        summary = self.m.failure_summary(caught.exception)
        self.assertEqual(summary.get('failed_symbol'), 'AAA')
        self.assertEqual(summary.get('replacement_rejection', {}).get('rejection_rule'), 'missing_existing_dates')
        self.assertEqual(summary['replacement_rejection']['missing_existing_dates'], ['2026-10-13'])
        path = 'data/market-data/collector-state/2026-10/2026-10-18-daily-baseline-recovery-123-1.json'
        terminal = json.loads(git(self.repo, 'show', 'HEAD:' + path))
        self.assertEqual(terminal.get('failed_symbol'), 'AAA')
        self.assertEqual(terminal.get('replacement_rejection'), summary['replacement_rejection'])
        self.assertEqual(self.http_count, 1)
        self.assert_no_remote_facts()
        for value in (summary, terminal):
            self.assertNotIn('changed_fields', json.dumps(value))
            self.assertNotIn('volume', json.dumps(value))

    def test_arbitrary_exception_attributes_cannot_inject_symbol(self):
        exc = RuntimeError('TOKEN')
        exc.code = 'replacement_rejected'
        exc.failed_symbol = 'TOKEN'
        exc.evidence = {'symbol': 'TOKEN', 'rejection_rule': 'TOKEN', 'missing_existing_dates': ['TOKEN']}
        exc.replacement_diagnostic = {'failed_symbol': 'TOKEN'}
        out = self.m.failure_summary(exc)
        self.assertNotIn('TOKEN', json.dumps(out))

class SafeProjectionTests(unittest.TestCase):
    def test_projection_whitelists_types_dates_counts_and_trusted_symbols(self):
        import replacement_diagnostics as d
        raw = {'rejection_rule': 'missing_existing_dates', 'existing_records': True,
               'replacement_records': 5001, 'target_records': -1,
               'missing_existing_dates': ['TOKEN', '2026-02-30', '2026-01-02', '２０２６-01-02'],
               'revision_date': '20260102', 'open': 123.456, 'changed_fields': {'TOKEN': 1}}
        out = d.project_rejection(raw)
        self.assertEqual(out, {'rejection_rule': 'missing_existing_dates', 'missing_existing_dates': ['2026-01-02']})
        for symbol, targets in [('TOKEN', ('AAA',)), ('bad/key', ('bad/key',)), ('AAA\n', ('AAA\n',))]:
            value = d.ReplacementDiagnostic(symbol, tuple(targets), raw).as_dict()
            self.assertNotIn('failed_symbol', value)
        good = d.ReplacementDiagnostic('AAA', ('AAA',), raw).as_dict()
        self.assertEqual(good['failed_symbol'], 'AAA')
        for raw in (None, ['TOKEN'], {'rejection_rule': ['TOKEN']}, {'rejection_rule': 'TOKEN'}):
            self.assertEqual(d.project_rejection(raw), {'rejection_rule': 'unclassified'})

class AdditionalRecoveryTests(RecoveryIntegrationFixture, unittest.TestCase):
    def test_later_symbol_is_bound_to_actual_failure_not_debit_order(self):
        def response(url, **kwargs):
            result = self.payload(url, **kwargs)
            if self.http_count == 2:
                result['values'] = [x for x in result['values'] if x['datetime'] != '2026-10-13']
            return result
        with self.assertRaises(Exception) as caught: self.run_recovery(response)
        result = self.m.failure_summary(caught.exception)
        self.assertEqual(result['failed_symbol'], 'BBB')
        self.assertEqual(result['replacement_rejection']['rejection_rule'], 'missing_existing_dates')
        self.assertEqual(self.http_count, 2)
        self.assert_no_remote_facts()

    def test_hash_mismatch_has_own_rule_and_no_fact_write(self):
        original = self.a.compare_replacement
        def changed_hash(*args, **kwargs):
            result = original(*args, **kwargs)
            return dict(result, replacement_sha256='0' * 64)
        with patch.object(self.a, 'compare_replacement', side_effect=changed_hash), self.assertRaises(Exception) as caught:
            self.run_recovery()
        result = self.m.failure_summary(caught.exception)
        self.assertEqual(result['replacement_rejection'], {'rejection_rule': 'replacement_hash_mismatch'})
        self.assertEqual(result['failed_symbol'], 'AAA')
        self.assertEqual(self.http_count, 1)
        self.assert_no_remote_facts()

    def test_terminal_publication_failure_preserves_original_rejection_without_retry(self):
        self.new.pop(0)
        make = self.m.make_writer
        statuses = []
        def writer(*args, **kwargs):
            result = make(*args, **kwargs)
            report = result.report
            def fail_final(value):
                statuses.append(value['status'])
                if value['status'] == 'BLOCKED':
                    raise self.a.AdmissionError('durability_failed', verifier_reason='STORE_PUBLICATION_TAG_PUSH_REJECTED')
                return report(value)
            result.report = fail_final
            return result
        with patch.object(self.m, 'make_writer', side_effect=writer), self.assertRaises(Exception) as caught:
            self.run_recovery()
        result = self.m.failure_summary(caught.exception)
        self.assertEqual(result['failed_symbol'], 'AAA')
        self.assertEqual(result['replacement_rejection']['rejection_rule'], 'missing_existing_dates')
        self.assertEqual(result['terminal_report_failure']['failure_code'], 'durability_failed')
        self.assertEqual(result['terminal_report_outcome'], 'UNCONFIRMED')
        self.assertEqual(statuses, ['IN_PROGRESS', 'BLOCKED'])
        self.assertEqual(self.http_count, 1)
        self.assert_no_remote_facts()


class RejectionBoundaryTests(AdmissionCanonicalTests):
    def test_validator_empty_inputs_have_specific_rules(self):
        for existing, replacement, rule in (([], self.rows, 'existing_series_empty'),
                                            (self.rows, [], 'replacement_empty')):
            with self.assertRaises(self.h.rebaser.ReplacementValidationError) as caught:
                self.h.rebaser.validate_replacement(existing, replacement, **self.cfg)
            self.assertEqual(caught.exception.rejection_evidence['rejection_rule'], rule)

    def test_exact_limit_and_tail_boundary_preserve_acceptance(self):
        rows = copy.deepcopy(self.rows)
        for row in rows[7:10]: row['close'] = 10.5
        result = self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(len(result['revisions']), 3)
        rows[6]['close'] = 10.5
        with self.assertRaises(self.m.AdmissionError) as caught:
            self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(caught.exception.evidence['rejection_rule'], 'revision_outside_tail')

    def test_untrusted_field_names_never_appear_in_rejection(self):
        rows = copy.deepcopy(self.rows)
        self.candidate['existing'][0]['TOKEN'] = 'PRIVATE_VALUE'
        with self.assertRaises(self.m.AdmissionError) as caught:
            self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(caught.exception.evidence['rejection_rule'], 'revision_outside_tail')
        self.assertNotIn('TOKEN', json.dumps(caught.exception.evidence))
        self.assertNotIn('PRIVATE_VALUE', json.dumps(caught.exception.evidence))

class UnexpectedFailureTests(AdmissionCanonicalTests):
    def test_unexpected_normalizer_and_canonical_failures_are_unclassified(self):
        for helper in ('_normalize_bar', 'canonical_bytes'):
            with patch.object(self.h.store, helper, side_effect=RuntimeError('TOKEN')), self.assertRaises(self.m.AdmissionError) as caught:
                self.m.compare_replacement(self.h, self.candidate, self.rows, DAY, self.cfg)
            self.assertEqual(caught.exception.evidence['rejection_rule'], 'unclassified')
            self.assertNotIn('TOKEN', json.dumps(caught.exception.evidence))

    def test_original_diff_helper_failure_remains_closed(self):
        rows = copy.deepcopy(self.rows)
        rows[9]['close'] = 10.5
        with patch.object(self.h.rebaser, '_field_diff', side_effect=RuntimeError('TOKEN')), self.assertRaises(self.m.AdmissionError) as caught:
            self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(caught.exception.evidence['rejection_rule'], 'unclassified')

    def test_diff_helper_call_order_matches_original_double_comparison(self):
        rows = copy.deepcopy(self.rows); rows[9]['close'] = 10.5
        original = self.h.rebaser._field_diff
        calls = []
        def reject_second(*args):
            calls.append(1)
            if len(calls) == 2: raise RuntimeError('TOKEN')
            return original(*args)
        with patch.object(self.h.rebaser, '_field_diff', side_effect=reject_second), self.assertRaises(self.m.AdmissionError):
            self.m.compare_replacement(self.h, self.candidate, rows, DAY, self.cfg)
        self.assertEqual(len(calls), 2)

class MissingEvidenceTests(RecoveryIntegrationFixture, unittest.TestCase):
    def test_missing_evidence_does_not_overwrite_original_rejection(self):
        exc = self.m.RecoveryError('replacement_rejected')
        with patch.object(self.m, 'qualify', side_effect=exc), self.assertRaises(self.m.RecoveryError) as caught:
            self.run_recovery()
        summary = self.m.failure_summary(caught.exception)
        self.assertEqual(summary['failure_code'], 'replacement_rejected')
        self.assertEqual(summary['replacement_rejection'], {'rejection_rule': 'unclassified'})
        self.assertEqual(summary['failed_symbol'], 'AAA')
        self.assert_no_remote_facts()

    def test_cli_prints_the_projected_replacement_failure(self):
        import io
        from contextlib import redirect_stdout
        self.new.pop(0)
        with self.assertRaises(Exception) as caught: self.run_recovery()
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': 'yuatom/stock-market-data', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_SHA': self.compute_sha, 'TWELVE_DATA_API_KEY': 'TOKEN'}
        argv = ['--trade-date', '2026-10-18', '--compute-sha', self.compute_sha,
                '--store-sha', self.base, '--store-repo', str(self.repo), '--request-id', '123-1']
        output = io.StringIO()
        with patch.dict('os.environ', env), patch.object(self.m, 'recover', side_effect=caught.exception), redirect_stdout(output):
            self.assertEqual(self.m.main(argv), 2)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['failed_symbol'], 'AAA')
        self.assertEqual(summary['replacement_rejection']['rejection_rule'], 'missing_existing_dates')
        self.assertNotIn('TOKEN', output.getvalue())

class QualificationContextTests(RecoveryIntegrationFixture, unittest.TestCase):
    def test_unknown_qualification_failure_keeps_code_and_trusted_context(self):
        with patch.object(self.m, 'qualify', side_effect=RuntimeError('TOKEN')), self.assertRaises(RuntimeError) as caught:
            self.run_recovery()
        summary = self.m.failure_summary(caught.exception)
        self.assertEqual(summary['failure_code'], 'internal_error')
        self.assertEqual(summary.get('failed_symbol'), 'AAA')
        self.assertEqual(summary.get('replacement_rejection'), {'rejection_rule': 'unclassified'})
        self.assertNotIn('TOKEN', json.dumps(summary))
        self.assert_no_remote_facts()

    def test_provider_failure_does_not_claim_qualification_context(self):
        with patch.object(self.h.rebaser, '_fetch_replacement_daily', side_effect=RuntimeError('TOKEN')), self.assertRaises(RuntimeError) as caught:
            self.run_recovery()
        summary = self.m.failure_summary(caught.exception)
        self.assertEqual(summary['failure_code'], 'internal_error')
        self.assertNotIn('failed_symbol', summary)
        self.assertNotIn('replacement_rejection', summary)

class MalformedExistingTests(AdmissionCanonicalTests):
    def test_malformed_existing_retains_original_replacement_rejection(self):
        candidate = dict(self.candidate, existing=None)
        with self.assertRaises(self.m.AdmissionError) as caught:
            self.m.compare_replacement(self.h, candidate, self.rows, DAY, self.cfg)
        self.assertEqual(caught.exception.code, 'replacement_rejected')
        self.assertEqual(caught.exception.evidence['rejection_rule'], 'unclassified')
