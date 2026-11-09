"""Fictional AAA/BBB/CCC only; real local Git, ledger, archives and verifier."""
import copy
import importlib
import json
from pathlib import Path
from datetime import datetime
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import yaml
from test_guarded_history_recovery import RecoveryIntegrationFixture, DAY, NOW, git

ROOT = Path(__file__).resolve().parents[1]


def maintenance():
    assert (ROOT / 'scripts/maintain_fixed_symbols.py').is_file(), 'isolated maintenance implementation absent'
    return importlib.import_module('maintain_fixed_symbols')


class SymbolMaintenanceTests(RecoveryIntegrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.universe['daily_series'].append({'symbol': 'CCC', 'asset_class': 'etf'})
        self.h.store.append_daily_bars(self.repo / 'data/market-data', provider='twelve_data_basic',
            symbol='CCC', asset_class='etf', bars=self.old, series_semantics='daily_regular_ohlcv', shard_record_limit=2)
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', 'synthetic unselected canary')
        git(self.repo, 'push', 'origin', 'main'); self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.selector = {'schema_version': 1, 'targets': [
            {'symbol': symbol, 'index_blob_sha': git(self.repo, 'rev-parse',
             f'{self.base}:data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json')}
            for symbol in ('AAA', 'BBB')]}
        self.seen = []

    def subtree(self, commit, symbol):
        prefix = f'data/market-data/series/daily/twelve_data_basic/{symbol}'
        return git(self.repo, 'rev-parse', f'{commit}:{prefix}')

    def run_maintenance(self, *, reject=(), selector=None, http=None):
        m = maintenance()
        def payload(url, **kwargs):
            symbol = parse_qs(urlparse(url).query)['symbol'][0]
            self.seen.append(symbol)
            value = self.payload(url, **kwargs)
            if symbol in reject:
                value['values'] = [r for r in value['values'] if r['datetime'] != self.old[0]['trade_date']]
            return value
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)
        with patch.object(self.h.collectors, 'datetime', Clock), \
             patch.object(self.h.collectors, '_http_json', side_effect=http or payload):
            return m.maintain(self.h, compute=self.compute, compute_sha=self.compute_sha,
                repo=self.repo, store_sha=self.base, day=DAY, request_id='789-1', config=self.config,
                universe=self.universe, access=self.access, key='SYNTHETIC',
                fixed_selector=self.selector if selector is None else selector, clock=lambda: NOW)

    def test_two_successes_chain_exact_receipts_keep_request_id_and_full_archives(self):
        result = self.run_maintenance()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(self.seen, ['AAA', 'BBB'])
        self.assertEqual(result['published_count'], 2)
        self.assertEqual(result['requests_attempted'], 2)
        self.assertEqual(result['budget_debits_confirmed'], 2)
        self.assertEqual(result['transactions'][1]['store_start_sha'], result['transactions'][0]['checkpoint_commit_sha'])
        final = result['last_verified_commit_sha']
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), final)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), final)
        self.assertEqual(self.subtree(final, 'CCC'), self.subtree(self.base, 'CCC'))
        self.assertEqual(self.current_budget()['credits'], 2)
        self.assertEqual(sum(self.current_budget()['minutes'].values()), 2)
        for row in result['transactions']:
            self.assertEqual(row['status'], 'PASS')
            sha = row['store_commit_sha']
            self.assertEqual(git(self.store_remote, 'rev-parse', f'refs/tags/market-data-read/{sha}'), sha)
            report = json.loads(git(self.repo, 'show', f"{sha}:{row['result_state_path']}"))
            self.assertEqual(report['request_id'], '789-1')
            self.assertIn('-' + row['symbol'] + '.json', row['result_state_path'])
            archive = f"data/market-data/recovery/{DAY}/twelve_data_basic/daily-baseline-rebase/{row['symbol']}.json"
            self.assertEqual(json.loads(git(self.repo, 'show', f'{sha}:{archive}'))['old_records'], self.old)

    def test_confirmed_rejection_quarantines_first_without_touching_facts_then_publishes_next(self):
        result = self.run_maintenance(reject=('AAA',))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual([x['status'] for x in result['transactions']], ['QUARANTINED', 'PASS'])
        self.assertEqual(self.seen, ['AAA', 'BBB'])
        self.assertEqual(result['quarantined_symbols'], ['AAA'])
        self.assertEqual(result['not_attempted_symbols'], [])
        self.assertEqual(result['transactions'][1]['store_start_sha'], result['transactions'][0]['checkpoint_commit_sha'])
        final = result['last_verified_commit_sha']
        self.assertEqual(self.subtree(final, 'AAA'), self.subtree(self.base, 'AAA'))
        self.assertNotEqual(self.subtree(final, 'BBB'), self.subtree(self.base, 'BBB'))
        self.assertEqual(self.subtree(final, 'CCC'), self.subtree(self.base, 'CCC'))
        self.assertEqual(self.current_budget()['credits'], 2)
        first = result['transactions'][0]
        report = json.loads(git(self.repo, 'show', f"{first['store_commit_sha']}:{first['result_state_path']}"))
        self.assertEqual(report['failed_symbol'], 'AAA')
        self.assertEqual(report['replacement_rejection']['rejection_rule'], 'missing_existing_dates')

    def test_later_rejection_preserves_and_reports_prior_success(self):
        result = self.run_maintenance(reject=('BBB',))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['published_count'], 1)
        self.assertNotEqual(self.subtree(result['last_verified_commit_sha'], 'AAA'), self.subtree(self.base, 'AAA'))
        self.assertEqual(self.subtree(result['last_verified_commit_sha'], 'BBB'), self.subtree(self.base, 'BBB'))

    def test_all_rejections_are_blocked_not_success(self):
        result = self.run_maintenance(reject=('AAA', 'BBB'))
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['published_count'], 0)
        self.assertEqual(result['quarantined_symbols'], ['AAA', 'BBB'])
        self.assertEqual(self.http_count, 2)
        self.assert_no_remote_facts()

    def test_unclassified_wrapped_error_stops_after_one_request(self):
        with patch.object(self.h.rebaser, 'validate_replacement', side_effect=RuntimeError('PRIVATE SECRET')):
            result = self.run_maintenance()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.http_count, 1)
        self.assertEqual(result['not_attempted_symbols'], ['BBB'])
        self.assertEqual(result['quarantined_symbols'], [])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assert_no_remote_facts()

    def test_hash_mismatch_stops_not_quarantines(self):
        def mismatch(*a, **kw):
            exc = self.m.RecoveryError('replacement_rejected')
            exc.evidence = {'rejection_rule': 'replacement_hash_mismatch'}
            raise exc
        with patch.object(self.m, 'qualify', side_effect=mismatch): result = self.run_maintenance()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['quarantined_symbols'], [])
        self.assertEqual(self.http_count, 1)

    def test_transport_failure_stops_without_next_request(self):
        calls = []
        def fail(url, **kw): calls.append(url); raise RuntimeError('PRIVATE transport')
        result = self.run_maintenance(http=fail)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.current_budget()['credits'], 1)
        self.assertEqual(result['not_attempted_symbols'], ['BBB'])

    def test_whole_subset_headroom_rejects_without_requests_or_metadata(self):
        self.seed_budget(199)
        before = self.base
        result = self.run_maintenance()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['stop']['failure_code'], 'budget_exhausted')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), before)
        self.assertEqual(self.current_budget()['credits'], 199)

    def test_bad_second_selector_blocks_before_first_request(self):
        bad = copy.deepcopy(self.selector); bad['targets'][1]['index_blob_sha'] = '0' * 40
        result = self.run_maintenance(selector=bad)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.http_count, 0)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), self.base)

    def test_terminal_report_lost_acknowledgement_stops_even_if_remote_durable(self):
        original = self.m.make_writer
        def writer(*a, **kw):
            w = original(*a, **kw); report = w.report
            def lose(value):
                sha = report(value)
                if value['status'] == 'BLOCKED':
                    raise self.m.RecoveryError('publication_unconfirmed', candidate_sha=sha)
                return sha
            w.report = lose; return w
        with patch.object(self.m, 'make_writer', side_effect=writer):
            result = self.run_maintenance(reject=('AAA',))
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['quarantined_symbols'], [])
        self.assertEqual(self.http_count, 1)
        self.assertEqual(self.current_budget()['credits'], 1)
        self.assertEqual(result['not_attempted_symbols'], ['BBB'])

    def test_remote_drift_after_success_does_not_follow_new_main(self):
        original = self.m.recover
        observed = []
        def drift(*a, **kw):
            result = original(*a, **kw)
            observed.append(result['store_commit_sha'])
            # Simulate a separate writer, not a permitted controller update.
            other = self.root / 'concurrent'
            git(self.root, 'clone', str(self.store_remote), str(other))
            git(other, 'checkout', 'main'); git(other, 'config', 'user.name', 'test'); git(other, 'config', 'user.email', 'test@example.invalid')
            (other / 'OTHER').write_text('external writer')
            git(other, 'add', 'OTHER'); git(other, 'commit', '-m', 'synthetic concurrent write'); git(other, 'push', 'origin', 'main')
            return result
        with patch.object(self.m, 'recover', side_effect=drift): result = self.run_maintenance()
        self.assertEqual(self.http_count, 1)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['published_count'], 1)
        self.assertEqual(result['stop']['failure_code'], 'object_drift')
        self.assertEqual(result['transactions'][0]['store_commit_sha'], observed[0])

    def test_global_deadline_is_not_reset_after_first_success(self):
        m = maintenance(); current = [100.0]; original = self.m.recover
        def finish(*a, **kw):
            self.assertEqual(kw['overall_deadline'], 700.0)
            result = original(*a, **kw); current[0] = 701.0; return result
        with patch.object(m.time, 'monotonic', side_effect=lambda: current[0]), patch.object(self.m, 'recover', side_effect=finish):
            result = self.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(self.http_count, 1)
        self.assertEqual(result['stop']['failure_code'], 'deadline_exceeded')

    def test_prior_fact_commit_is_visible_if_next_publication_ack_is_lost(self):
        original = self.m.stage_and_publish; calls = []
        def lose(*a, **kw):
            result = original(*a, **kw); calls.append(result['store_commit_sha'])
            if len(calls) == 2:
                raise self.m.RecoveryError('publication_unconfirmed', candidate_sha=result['store_commit_sha'])
            return result
        with patch.object(self.m, 'stage_and_publish', side_effect=lose): result = self.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['published_count'], 1)
        self.assertEqual(result['stop']['candidate_commit_sha'], calls[1])
        self.assertEqual(result['transactions'][0]['store_commit_sha'], calls[0])
        self.assertEqual(self.http_count, 2)

    def test_no_default_or_duplicate_selection(self):
        for selector in ({}, {'schema_version': 1, 'targets': []},
                         {'schema_version': 1, 'targets': [self.selector['targets'][0]] * 2}):
            with self.subTest(selector=selector):
                result = self.run_maintenance(selector=selector)
                self.assertEqual(result['status'], 'BLOCKED'); self.assertEqual(self.http_count, 0)

    def test_changed_quality_or_budget_configuration_blocks(self):
        self.config['series']['baseline_initialization']['explicit_rebase']['maximum_revised_overlap_records'] = 4
        result = self.run_maintenance()
        self.assertEqual(result['status'], 'BLOCKED'); self.assertEqual(self.http_count, 0)

    def test_wrapper_postcheck_failure_never_hides_child_confirmed_publication(self):
        m = maintenance()
        with patch.object(m, '_verify_terminal', side_effect=self.m.RecoveryError('closure_invalid')):
            result = self.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['published_count'], 1)
        self.assertEqual(result['transactions'][0]['status'], 'PASS_UNRECONCILED')
        self.assertEqual(self.http_count, 1)
        remote = git(self.store_remote, 'rev-parse', 'refs/heads/main')
        self.assertNotEqual(self.subtree(remote, 'AAA'), self.subtree(self.base, 'AAA'))
        self.assertEqual(result['not_attempted_symbols'], ['BBB'])

    def test_child_compute_identity_must_match_batch_pin(self):
        original = self.m.recover
        def wrong(*a, **kw):
            result = original(*a, **kw); result['compute_sha'] = '0' * 40; return result
        with patch.object(self.m, 'recover', side_effect=wrong): result = self.run_maintenance()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(self.http_count, 1)
        self.assertEqual(result['stop']['failure_code'], 'closure_invalid')
        self.assertEqual(result['published_count'], 1)

    def test_persistent_batch_plan_and_checkpoints_reconstruct_progress_read_only(self):
        result = self.run_maintenance(reject=('AAA',))
        plan = json.loads(git(self.repo, 'show', f"{result['batch_manifest_commit_sha']}:{result['batch_manifest_path']}"))
        self.assertEqual(plan['fixed_selection'], self.selector)
        self.assertEqual(plan['store_start_sha'], self.base)
        self.assertEqual(plan['compute_sha'], self.compute_sha)
        self.assertEqual(plan['budget_limits'], {'per_day': 200, 'per_minute': 8})
        self.assertEqual(plan['maximum_provider_requests'], 2)
        for row in result['transactions']:
            sha = row['checkpoint_commit_sha']
            self.assertEqual(git(self.store_remote, 'rev-parse', f'refs/tags/market-data-read/{sha}'), sha)
        before = git(self.store_remote, 'rev-parse', 'refs/heads/main')
        recovered = maintenance().read_progress(self.repo, day=DAY, request_id='789-1',
            manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(recovered['status'], 'PARTIAL')
        self.assertEqual(recovered['published_symbols'], ['BBB'])
        self.assertEqual(recovered['quarantined_symbols'], ['AAA'])
        self.assertFalse(recovered['automatic_retry_allowed'])
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), before)
        self.assertEqual(self.current_budget()['credits'], 2)

    def test_unknown_fact_publication_never_writes_later_checkpoint(self):
        original = self.m.stage_and_publish; calls=[]
        def lose(*a, **kw):
            result=original(*a, **kw); calls.append(result['store_commit_sha'])
            if len(calls)==2: raise self.m.RecoveryError('publication_unconfirmed', candidate_sha=result['store_commit_sha'])
            return result
        with patch.object(self.m, 'stage_and_publish', side_effect=lose): result=self.run_maintenance()
        final=git(self.store_remote,'rev-parse','refs/heads/main')
        names=git(self.repo,'ls-tree','-r','--name-only',final).splitlines()
        self.assertTrue(any('daily-symbol-maintenance-789-1-AAA.json' in p for p in names))
        self.assertFalse(any('daily-symbol-maintenance-789-1-BBB.json' in p for p in names))
        recovered=maintenance().read_progress(self.repo,day=DAY,request_id='789-1',manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(recovered['status'],'PARTIAL')
        self.assertEqual(recovered['published_symbols'],['AAA'])
        self.assertEqual(recovered['unreconciled_symbols'],['BBB'])

    def test_pass_does_not_claim_cutoff_completion_when_no_new_session(self):
        self.new = copy.deepcopy(self.old)
        result = self.run_maintenance()
        self.assertEqual(result['status'], 'PASS')
        self.assertFalse(result['completeness_proven'])
        for row in result['transactions']:
            self.assertEqual(row['last_trade_date'], self.old[-1]['trade_date'])
            self.assertEqual(row['new_records'], 0)
            self.assertIs(row['latest_date_advanced'], False)
            self.assertFalse(row['completeness_proven'])
        last = result['transactions'][-1]
        saved = json.loads(git(self.repo, 'show', f"{last['checkpoint_commit_sha']}:{last['checkpoint_path']}"))
        self.assertEqual(saved['transactions'][0]['new_records'], 0)
        self.assertIs(saved['transactions'][0]['latest_date_advanced'], False)

    def test_read_progress_never_accepts_empty_plan_as_complete(self):
        result=self.run_maintenance()
        path=self.repo/result['batch_manifest_path']
        plan=json.loads(path.read_text());plan['fixed_selection']['targets']=[]
        path.write_text(json.dumps(plan));git(self.repo,'add',str(path));git(self.repo,'commit','-m','synthetic malformed plan')
        head=git(self.repo,'rev-parse','HEAD');git(self.repo,'push','origin','main')
        git(self.repo,'tag','market-data-read/'+head);git(self.repo,'push','origin','refs/tags/market-data-read/'+head)
        observed=maintenance().read_progress(self.repo,day=DAY,request_id='789-1',manifest_commit_sha=head)
        self.assertEqual(observed['status'],'UNKNOWN')
        self.assertFalse(observed['automatic_retry_allowed'])

    def commit_tamper(self, path, value):
        (self.repo/path).write_text(json.dumps(value))
        git(self.repo,'add',path);git(self.repo,'commit','-m','synthetic tamper')
        sha=git(self.repo,'rev-parse','HEAD');git(self.repo,'push','origin','main')
        git(self.repo,'tag','market-data-read/'+sha);git(self.repo,'push','origin','refs/tags/market-data-read/'+sha)

    def test_reader_rejects_rewritten_checkpoint_claiming_false_pass(self):
        result=self.run_maintenance(reject=('AAA',))
        path=result['transactions'][-1]['checkpoint_path']
        value=json.loads((self.repo/path).read_text());value['transactions'][0]['status']='PASS'
        self.commit_tamper(path,value)
        observed=maintenance().read_progress(self.repo,day=DAY,request_id='789-1',manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(observed['status'],'UNKNOWN')

    def test_reader_rejects_child_report_rewrite(self):
        result=self.run_maintenance()
        path=result['transactions'][0]['result_state_path']
        value=json.loads((self.repo/path).read_text());value['compute_sha']='0'*40
        self.commit_tamper(path,value)
        observed=maintenance().read_progress(self.repo,day=DAY,request_id='789-1',manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(observed['status'],'UNKNOWN')

    def test_reader_rejects_rolled_back_remote_main(self):
        result=self.run_maintenance()
        git(self.store_remote,'update-ref','refs/heads/main',self.base)
        observed=maintenance().read_progress(self.repo,day=DAY,request_id='789-1',manifest_commit_sha=result['batch_manifest_commit_sha'])
        self.assertEqual(observed['status'],'UNKNOWN')
        self.assertFalse(observed['automatic_retry_allowed'])

    def test_source_drift_during_initial_validation_blocks_before_batch_metadata(self):
        original=self.m.original_entries;calls=[]
        def drift(*a,**kw):
            rows=original(*a,**kw);calls.append(True)
            if len(calls)==1: (self.compute/'README.md').write_text('synthetic dirty Compute')
            return rows
        with patch.object(self.m,'original_entries',side_effect=drift): result=self.run_maintenance()
        self.assertEqual(result['status'],'BLOCKED')
        self.assertEqual(result['stop']['failure_code'],'object_drift')
        self.assertEqual(self.http_count,0)
        self.assertEqual(git(self.store_remote,'rev-parse','refs/heads/main'),self.base)


class MaintenanceWorkflowTests(unittest.TestCase):
    def test_opt_in_official_entry_is_fixed_only_and_has_same_lock(self):
        w = yaml.safe_load((ROOT / '.github/workflows/market-data-collector.yml').read_text())
        mode = 'daily_symbol_maintenance'
        self.assertIn(mode, w[True]['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn(mode, w['jobs']['run']['if'])
        job = w['jobs']['symbol_maintenance']
        self.assertIn("inputs.mode == 'daily_symbol_maintenance'", job['if'])
        self.assertEqual(job['concurrency'], {'group': 'market-data-collector', 'cancel-in-progress': False, 'queue': 'max'})
        self.assertEqual(job['needs'], 'selector_guard')
        serialized = json.dumps(job)
        self.assertIn('maintain_fixed_symbols.py', serialized)
        self.assertNotIn('strategy', job)
        self.assertNotIn('continue-on-error', serialized)
        guard = json.dumps(w['jobs']['selector_guard'])
        self.assertIn('fixed symbol maintenance requires explicit fixed targets', guard)
