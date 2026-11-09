"""Fictional receipt faults only: real local Git and no provider network."""
import json
import unittest
from unittest.mock import patch
import test_isolated_symbol_maintenance as base


class ReceiptClosureTests(unittest.TestCase):
    def fixture(self):
        f = base.SymbolMaintenanceTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        return f

    def broken_manifest(self, f, omit):
        original_stage = f.m.stage_and_publish
        original_validate = f.m.validate_manifest
        def stage(*args, **kwargs):
            injected = []
            def incomplete(value, outputs, request_id, day, **extra):
                if len(outputs) > 1 and not injected:
                    removed = [x['path'] for x in outputs if omit(x['path'])]
                    self.assertTrue(removed, 'fixture must actually omit a required output')
                    injected.extend(removed)
                    outputs[:] = [x for x in outputs if not omit(x['path'])]
                    value['required_outputs'] = [dict(path=x['path'][len(f.m.PREFIX):], blob_sha=x['blob_sha']) for x in outputs]
                return original_validate(value, outputs, request_id, day, **extra)
            # Fault injection is confined to fixture publication. Both outer
            # transaction checker and read_progress run unmodified afterwards.
            with patch.object(f.m, 'validate_manifest', side_effect=incomplete):
                result = original_stage(*args, **kwargs)
            self.assertTrue(injected)
            return result
        with patch.object(f.m, 'stage_and_publish', side_effect=stage):
            return f.run_maintenance()

    def test_missing_required_output_stops_after_confirmed_first_publication(self):
        cases = {
            'everything_except_report': lambda p: 'daily-baseline-recovery-' not in p,
            'archive': lambda p: '/recovery/' in p,
            'inventory': lambda p: p.endswith('market-data-inventory.json'),
            'index': lambda p: p.endswith('/_index.json'),
            'one_shard': lambda p: p.endswith('/000000.json'),
        }
        for name, omit in cases.items():
            with self.subTest(name=name):
                f = self.fixture()
                result = self.broken_manifest(f, omit)
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['stop']['failure_code'], 'closure_invalid')
                self.assertEqual(result['published_count'], 1)
                self.assertEqual(result['transactions'][0]['status'], 'PASS_UNRECONCILED')
                self.assertEqual(f.http_count, 1)
                self.assertEqual(result['not_attempted_symbols'], ['BBB'])

    def test_reader_refuses_report_only_manifest_even_with_durable_checkpoint(self):
        f = self.fixture()
        result = self.broken_manifest(f, lambda p: 'daily-baseline-recovery-' not in p)
        m = base.maintenance()
        first = result['transactions'][0]
        if 'checkpoint_commit_sha' not in first:
            # Materialize a malformed *historical* checkpoint as a fixture,
            # not by bypassing or mocking the reader under test.
            report = json.loads(base.git(f.repo, 'show', f"{first['store_commit_sha']}:{first['result_state_path']}"))
            row = dict(first, status='PASS', **m._coverage(report, 'AAA'))
            m._adopt(f.repo, first['store_commit_sha'])
            checkpoint = dict(schema_version=1, mode=m.MODE, kind='batch_checkpoint', status='PROGRESS_VALIDATED',
                request_id='789-1', trade_date=base.DAY, compute_sha=f.compute_sha,
                store_start_sha=first['store_commit_sha'], batch_manifest_commit_sha=result['batch_manifest_commit_sha'],
                batch_manifest_path=result['batch_manifest_path'], fixed_selection=f.selector,
                transactions=[row], requests_attempted=1, budget_debits_confirmed=1,
                budget_debits_unconfirmed=0, automatic_retry_allowed=False, market_fact_authority=False)
            m._publish_metadata(f.repo, first['store_commit_sha'], base.DAY, '789-1', f.config,
                                checkpoint, deadline=m.time.monotonic()+60, symbol='AAA')
        before = base.git(f.store_remote, 'rev-parse', 'refs/heads/main')
        observed = m.read_progress(f.repo, day=base.DAY, request_id='789-1', manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(observed['status'], 'UNKNOWN')
        self.assertEqual(observed['published_symbols'], [])
        self.assertFalse(observed['automatic_retry_allowed'])
        self.assertEqual(base.git(f.store_remote, 'rev-parse', 'refs/heads/main'), before)

    def test_later_checkpoint_corruption_preserves_verified_prefix(self):
        f = self.fixture()
        result = f.run_maintenance()
        path = result['transactions'][-1]['checkpoint_path']
        value = json.loads((f.repo/path).read_text()); value['compute_sha'] = '0'*40
        f.commit_tamper(path, value)
        observed = base.maintenance().read_progress(f.repo, day=base.DAY, request_id='789-1', manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(observed['status'], 'UNKNOWN')
        self.assertEqual(observed['published_symbols'], ['AAA'])
        self.assertEqual(observed['not_attempted_symbols'], [])
        self.assertFalse(observed['automatic_retry_allowed'])

    def test_archive_must_retain_exact_prior_records(self):
        f = self.fixture()
        original = f.m.validate_candidate
        def corrupt(h, stage, entries, day):
            outputs = original(h, stage, entries, day)
            archive = stage / entries[0]['archive_relative']
            doc = json.loads(archive.read_text()); doc['old_records'] = []
            f.m.write_json(archive, doc)
            return outputs
        with patch.object(f.m, 'validate_candidate', side_effect=corrupt):
            result = f.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop']['failure_code'], 'closure_invalid')
        self.assertEqual(f.http_count, 1)

    def test_success_summary_must_match_actual_new_series(self):
        f = self.fixture()
        original = f.m.write_json
        def corrupt(path, value):
            if value.get('status') == 'CANDIDATE_VALIDATED':
                value = json.loads(json.dumps(value))
                value['results'][0]['last_trade_date'] = '2026-10-17'
            return original(path, value)
        with patch.object(f.m, 'write_json', side_effect=corrupt):
            result = f.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop']['failure_code'], 'closure_invalid')
        self.assertEqual(f.http_count, 1)
