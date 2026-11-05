"""Failure evidence uses local Git and synthetic verifier/provider responses only."""
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_guarded_history_recovery import RecoveryIntegrationFixture, DAY, git, module


class RecoveryDiagnosticTests(RecoveryIntegrationFixture, unittest.TestCase):
    def fail_publication(self, *, failure='verify', publish_number=3, reason='STORE_PUBLICATION_TAG_PUSH_REJECTED'):
        original_run = subprocess.run
        original_writer = self.m.make_writer
        captured = []
        pushes = 0
        report_calls = []

        def make_writer(*args, **kwargs):
            writer = original_writer(*args, **kwargs)
            original_report = writer.report
            def report(value):
                report_calls.append(value['status'])
                return original_report(value)
            writer.report = report
            captured.append(writer)
            return writer

        def injected(argv, *args, **kwargs):
            nonlocal pushes
            if argv[:3] == ['git', '-C', str(self.repo.resolve())] and 'push' in argv and argv[-1].endswith(':refs/heads/main'):
                pushes += 1
                if pushes == publish_number and failure.startswith('push'):
                    if failure == 'push_after':
                        original_run(argv, *args, **kwargs)
                    return subprocess.CompletedProcess(argv, 1, '', 'PRIVATE https://user:TOKEN@example.invalid')
            if pushes == publish_number:
                if failure == 'readback' and argv[:3] == ['git', '-C', str(self.repo.resolve())] and argv[3:5] == ['ls-remote', 'origin']:
                    return subprocess.CompletedProcess(argv, 1, '', 'PRIVATE remote failure')
                if failure == 'verify' and any(str(x).endswith('/verify_store_publication.py') for x in argv):
                    return subprocess.CompletedProcess(argv, 1, '', json.dumps({
                        'status': 'store_publication_failed', 'reason': reason, 'private': 'TOKEN'}))
            return original_run(argv, *args, **kwargs)

        with patch.object(self.m, 'make_writer', side_effect=make_writer), patch.object(subprocess, 'run', side_effect=injected):
            with self.assertRaises(Exception) as ctx:
                self.run_recovery()
        self.assert_no_remote_facts()
        self.assertEqual(report_calls, ['IN_PROGRESS'], 'Never publish a second diagnostic after uncertain publication')
        self.assertEqual(pushes, publish_number, 'Never retry the publication')
        return self.m.failure_summary(ctx.exception), captured[0], ctx.exception

    def test_second_predebit_keeps_exact_boundary_and_counts_without_writes(self):
        summary, writer, exc = self.fail_publication()
        candidate = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(summary['failure_code'], 'durability_failed')
        self.assertEqual(summary['verifier_reason'], 'STORE_PUBLICATION_TAG_PUSH_REJECTED')
        self.assertEqual(summary['publication_phase'], 'durability_verification')
        self.assertEqual(summary['publication_kind'], 'budget_predebit')
        self.assertEqual(summary['candidate_commit_sha'], candidate)
        self.assertEqual(summary['last_verified_commit_sha'], writer.head)
        self.assertEqual(summary['expected_durability_ref'], 'refs/tags/market-data-read/' + candidate)
        self.assertEqual(summary['main_publication_outcome'], 'CONFIRMED')
        self.assertEqual(summary['durability_outcome'], 'UNCONFIRMED')
        self.assertEqual(summary['budget_debits_attempted'], 2)
        self.assertEqual(summary['budget_debits_confirmed'], 1)
        self.assertEqual(summary['budget_debits_unconfirmed'], 1)
        self.assertEqual(summary['requests_attempted'], 1)
        self.assertEqual(summary['predebit_credits_before'], 1)
        self.assertEqual(summary['predebit_credits_after'], 2)
        self.assertEqual(summary['terminal_report_outcome'], 'NOT_ATTEMPTED')
        self.assertFalse(summary['retry_allowed'])
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), candidate)
        self.assertNotEqual(writer.head, candidate)
        self.assertEqual(git(self.store_remote, 'tag', '--list', 'market-data-read/' + candidate), '')
        retained = json.loads(git(self.repo, 'show', candidate + ':' + writer.report_relative))
        self.assertEqual((retained['status'], retained['budget_debits'], retained['requests_attempted']), ('IN_PROGRESS', 0, 0))
        self.assertEqual(self.http_count, 1)
        self.assertNotIn('TOKEN', json.dumps(summary))

    def test_first_predebit_failure_never_reaches_provider(self):
        summary, _, _ = self.fail_publication(publish_number=2)
        self.assertEqual((summary['budget_debits_attempted'], summary['budget_debits_confirmed'], summary['budget_debits_unconfirmed']), (1, 0, 1))
        self.assertEqual(summary['requests_attempted'], 0)
        self.assertEqual(self.http_count, 0)

    def test_initial_diagnostic_failure_keeps_zero_counters(self):
        summary, _, _ = self.fail_publication(publish_number=1)
        self.assertEqual(summary['verifier_reason'], 'STORE_PUBLICATION_TAG_PUSH_REJECTED')
        self.assertEqual((summary['budget_debits_attempted'], summary['budget_debits_confirmed'], summary['budget_debits_unconfirmed']), (0, 0, 0))
        self.assertEqual(summary['requests_attempted'], 0)
        self.assertEqual(summary['publication_kind'], 'diagnostic')
        self.assertEqual(self.http_count, 0)

    def test_push_rejection_remains_unconfirmed_without_guessing_remote_state(self):
        summary, writer, _ = self.fail_publication(failure='push_before')
        self.assertEqual(summary['failure_code'], 'publication_failed')
        self.assertEqual(summary['publication_phase'], 'main_push')
        self.assertEqual(summary['main_publication_outcome'], 'UNCONFIRMED')
        self.assertEqual(summary['budget_debits_unconfirmed'], 1)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), writer.head)

    def test_lost_push_acknowledgement_does_not_claim_rejection_or_retry(self):
        summary, writer, _ = self.fail_publication(failure='push_after')
        self.assertEqual(summary['main_publication_outcome'], 'UNCONFIRMED')
        self.assertEqual(summary['publication_phase'], 'main_push')
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), summary['candidate_commit_sha'])
        self.assertNotEqual(writer.head, summary['candidate_commit_sha'])

    def test_main_readback_failure_keeps_distinct_phase(self):
        summary, _, _ = self.fail_publication(failure='readback')
        self.assertEqual(summary['publication_phase'], 'main_readback')
        self.assertEqual(summary['main_publication_outcome'], 'UNCONFIRMED')
        self.assertEqual(summary['budget_debits_unconfirmed'], 1)

    def test_verifier_text_is_not_a_safe_reason_code(self):
        summary, _, _ = self.fail_publication(reason='STORE_PUBLICATION_TAG_PUSH_REJECTED https://user:TOKEN@example.invalid')
        self.assertEqual(summary['verifier_reason'], 'STORE_PUBLICATION_VALIDATION_FAILED')
        self.assertNotIn('TOKEN', json.dumps(summary))

    def test_main_prints_safe_terminal_summary(self):
        summary, _, exc = self.fail_publication()
        argv = ['--trade-date', DAY, '--compute-sha', self.compute_sha, '--store-sha', self.base,
                '--store-repo', str(self.repo), '--request-id', '123-1']
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_REPOSITORY': 'yuatom/stock-market-data', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_SHA': self.compute_sha, 'TWELVE_DATA_API_KEY': 'TOKEN'}
        output = io.StringIO()
        with patch.dict('os.environ', env), patch.object(self.m, 'recover', side_effect=exc), redirect_stdout(output):
            self.assertEqual(self.m.main(argv), 2)
        self.assertEqual(json.loads(output.getvalue()), summary)
        self.assertNotIn('TOKEN', output.getvalue())


class SafeVerifierReasonTests(unittest.TestCase):
    def test_untrusted_error_attributes_cannot_break_safe_summary(self):
        exc = RuntimeError('TOKEN')
        exc.code = ['TOKEN']
        exc.publication = {'candidate_commit_sha': 'TOKEN', 'publication_phase': 'TOKEN'}
        exc.recovery_progress = {'requests_attempted': True, 'budget_debits_confirmed': 'TOKEN'}
        result = module().failure_summary(exc)
        self.assertEqual(result['failure_code'], 'internal_error')
        self.assertNotIn('TOKEN', json.dumps(result))
        self.assertNotIn('requests_attempted', result)

    def test_verifier_wrapper_preserves_only_exact_fixed_codes(self):
        a = module().admission()
        cases = [
            (json.dumps({'status': 'store_publication_failed', 'reason': 'STORE_PUBLICATION_REMOTE_READ_EXHAUSTED'}), 'STORE_PUBLICATION_REMOTE_READ_EXHAUSTED'),
            (json.dumps({'status': 'wrong', 'reason': 'STORE_PUBLICATION_REMOTE_READ_EXHAUSTED'}), 'STORE_PUBLICATION_VALIDATION_FAILED'),
            ('PRIVATE TOKEN', 'STORE_PUBLICATION_VALIDATION_FAILED'),
            (json.dumps({'status': 'store_publication_failed', 'reason': 'TOKEN' * 2000}), 'STORE_PUBLICATION_VALIDATION_FAILED'),
        ]
        for stderr, expected in cases:
            with self.subTest(expected=expected), patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(['synthetic'], 1, 'TOKEN', stderr)):
                with self.assertRaises(a.AdmissionError) as ctx:
                    a.verify_durability(__import__('pathlib').Path('.'), 'a' * 40)
                result = module().failure_summary(ctx.exception)
                self.assertEqual(result['failure_code'], 'durability_failed')
                self.assertEqual(result['verifier_reason'], expected)
                self.assertNotIn('TOKEN', json.dumps(result))

    def test_verifier_cli_never_prints_arbitrary_error_message(self):
        import verify_store_publication as v
        output = io.StringIO()
        with patch('sys.argv', ['verify', '--expected-commit', 'a' * 40]), patch.object(v, 'verify_publication', side_effect=v.StorePublicationError('SECRET https://user:TOKEN@example.invalid')), patch('sys.stderr', output):
            self.assertEqual(v.main(), 1)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'STORE_PUBLICATION_VALIDATION_FAILED')
        self.assertNotIn('TOKEN', output.getvalue())


class RecoveryFactDiagnosticTests(RecoveryIntegrationFixture, unittest.TestCase):
    def test_fact_verifier_failure_preserves_reason_and_phase(self):
        original = self.m.verify_outputs
        def fail(repo, sha, manifest, outputs, request_id, day):
            if any('/series/' in x['path'] for x in outputs):
                raise self.a.AdmissionError('durability_failed', verifier_reason='STORE_PUBLICATION_REMOTE_READ_EXHAUSTED')
            return original(repo, sha, manifest, outputs, request_id, day)
        with patch.object(self.m, 'verify_outputs', side_effect=fail), self.assertRaises(self.m.RecoveryError) as ctx:
            self.run_recovery()
        summary = self.m.failure_summary(ctx.exception)
        self.assertEqual(summary['failure_code'], 'publication_unconfirmed')
        self.assertEqual(summary['publication_failure_code'], 'durability_failed')
        self.assertEqual(summary['verifier_reason'], 'STORE_PUBLICATION_REMOTE_READ_EXHAUSTED')
        self.assertEqual(summary['publication_kind'], 'market_facts')
        self.assertEqual(summary['publication_phase'], 'durability_verification')
        self.assertEqual(summary['budget_debits_confirmed'], 2)
        self.assertEqual(summary['budget_debits_unconfirmed'], 0)
        self.assertEqual(summary['requests_attempted'], 2)
        self.assertEqual(summary['terminal_report_outcome'], 'NOT_ATTEMPTED')
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), summary['candidate_commit_sha'])

    def test_failed_terminal_report_does_not_replace_original_failure(self):
        original = self.m.make_writer
        def make(*args, **kwargs):
            writer = original(*args, **kwargs)
            report = writer.report
            def fail(value):
                if value['status'] == 'BLOCKED':
                    raise self.a.AdmissionError('durability_failed', verifier_reason='STORE_PUBLICATION_TAG_PUSH_REJECTED')
                return report(value)
            writer.report = fail
            return writer
        with patch.object(self.m, 'make_writer', side_effect=make), self.assertRaises(Exception) as ctx:
            self.run_recovery(http=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('TOKEN')))
        summary = self.m.failure_summary(ctx.exception)
        self.assertEqual(summary['failure_code'], 'internal_error')
        self.assertEqual(summary['terminal_report_failure']['failure_code'], 'durability_failed')
        self.assertEqual(summary['terminal_report_failure']['verifier_reason'], 'STORE_PUBLICATION_TAG_PUSH_REJECTED')
        self.assertEqual(summary['terminal_report_outcome'], 'UNCONFIRMED')
        self.assertNotIn('TOKEN', json.dumps(summary))
