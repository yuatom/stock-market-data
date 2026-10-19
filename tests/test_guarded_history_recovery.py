"""Recovery publication: synthetic HTTP, real Store/Git/verifier in native CI."""
from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
DAY = '2026-10-18'
NOW = datetime(2026, 10, 18, 12, 0, 10, tzinfo=timezone.utc)


def module():
    assert (ROOT / 'scripts/recover_market_data_baseline.py').is_file(), 'guarded recovery implementation absent'
    return importlib.import_module('recover_market_data_baseline')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


class RecoveryPureTests(unittest.TestCase):
    def test_complete_unique_targets_required(self):
        m = module()
        m.require_complete(['A', 'B'], [{'symbol': 'A'}, {'symbol': 'B'}])
        for targets, rows in [([], []), (['A', 'A'], [{'symbol': 'A'}]),
                              (['A', 'B'], [{'symbol': 'A'}]),
                              (['A', 'B'], [{'symbol': 'A'}, {'symbol': 'A'}]),
                              (['A'], [{'symbol': 'B'}])]:
            with self.subTest(targets=targets, rows=rows), self.assertRaises(m.RecoveryError):
                m.require_complete(targets, rows)

    def test_paths_cannot_escape_or_follow_symlinks(self):
        m = module()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(m.safe_path(root, 'data/market-data/ok.json'), root / 'data/market-data/ok.json')
            for p in ('../outside', '/tmp/outside', 'data/market-data/../bad', 'README.md'):
                with self.subTest(path=p), self.assertRaises(m.RecoveryError):
                    m.safe_path(root, p)
            (root / 'data').symlink_to('/tmp', target_is_directory=True)
            with self.assertRaises(m.RecoveryError):
                m.safe_path(root, 'data/market-data/ok.json')

    def test_manifest_identity_and_nonempty_exact_outputs(self):
        m = module()
        outputs = [{'path': 'data/market-data/example.json', 'blob_sha': 'a' * 40}]
        value = {'schema_version': 1, 'request_id': '123-1', 'trade_date': DAY,
                 'stage': m.MODE, 'required_outputs': [{'path': 'example.json', 'blob_sha': 'a' * 40}]}
        m.validate_manifest(value, outputs, '123-1', DAY)
        for key, bad in [('request_id', 'other'), ('trade_date', '2026-10-17'),
                         ('stage', 'daily_baseline_admission'), ('required_outputs', []),
                         ('required_outputs', [{'path': 'other.json', 'blob_sha': 'a' * 40}])]:
            with self.subTest(key=key), self.assertRaises(m.RecoveryError):
                m.validate_manifest(dict(value, **{key: bad}), outputs, '123-1', DAY)
        with self.assertRaises(m.RecoveryError):
            m.validate_manifest(dict(value, required_outputs=[]), [], '123-1', DAY)

    def test_public_error_never_echoes_private_exception(self):
        m = module()
        result = m.failure_summary(RuntimeError('https://example/?apikey=SECRET'))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['status'], 'BLOCKED')


class RecoveryWorkflowTests(unittest.TestCase):
    def test_rebase_is_excluded_from_ordinary_publisher(self):
        w = yaml.safe_load((ROOT / '.github/workflows/market-data-collector.yml').read_text())
        self.assertIn('daily_baseline_rebase', w['jobs']['run']['if'])
        self.assertIn('daily_baseline_admission', w['jobs']['run']['if'])
        job = w['jobs']['recovery']
        self.assertIn('workflow_dispatch', job['if'])
        self.assertEqual(job['concurrency'], {'group': 'market-data-collector',
                                            'cancel-in-progress': False, 'queue': 'max'})
        text = json.dumps(job)
        self.assertIn('recover_market_data_baseline.py', text)
        for needle in ('EXPECTED_STORE_SHA', 'EXPECTED_COMPUTE_SHA', 'GITHUB_SHA', 'GITHUB_REF'):
            self.assertIn(needle, text)
        self.assertNotIn('upload-artifact', text)
        self.assertNotIn('rebase_market_data_baseline.py', text)

    def test_existing_schedule_and_admission_stay_unchanged(self):
        w = yaml.safe_load((ROOT / '.github/workflows/market-data-collector.yml').read_text())
        self.assertEqual(len(w[True]['schedule']), 11)
        self.assertTrue(all(x['timezone'] == 'America/New_York' for x in w[True]['schedule']))
        self.assertEqual(w['permissions'], {'contents': 'read'})
        self.assertIn('admit_market_data_baseline.py', json.dumps(w['jobs']['admission']))
        self.assertEqual(w[True]['workflow_dispatch']['inputs']['mode']['options'].count('daily_baseline_rebase'), 1)


class RecoveryIntegrationTests(unittest.TestCase):
    """No skipped helpers: native CI must run these against the complete source."""
    def setUp(self):
        self.m = module()
        self.a = importlib.import_module('admit_market_data_baseline')
        self.h = self.a.load_helpers()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_remote, self.repo = self.new_repo('store')
        self.compute_remote, self.compute = self.new_repo('compute')
        self.old = [{'trade_date': f'2026-10-{day:02}', 'open': 10.0, 'high': 12.0,
                     'low': 9.0, 'close': 11.0, 'volume': 100.0} for day in (13, 14, 15)]
        self.new = copy.deepcopy(self.old)
        self.new[-1]['volume'] = 101.0
        self.new.append(dict(self.old[-1], trade_date='2026-10-16'))
        self.universe = {'daily_series': [{'symbol': x, 'asset_class': 'etf'} for x in ('AAA', 'BBB')]}
        for symbol in ('AAA', 'BBB'):
            self.h.store.append_daily_bars(self.repo / 'data/market-data', provider='twelve_data_basic',
                symbol=symbol, asset_class='etf', bars=self.old,
                series_semantics='daily_regular_ohlcv', shard_record_limit=2)
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'seed histories')
        git(self.repo, 'push', 'origin', 'main')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.compute_sha = git(self.compute, 'rev-parse', 'HEAD')
        self.config = {'series': {'analysis_windows_may_read_last_sessions': [3],
            'baseline_initialization': {'explicit_rebase': {'maximum_revised_overlap_records': 3,
                'revised_overlap_must_be_within_last_existing_sessions': 3}}},
            'collector': {'twelve_data_daily': {'settlement_maturity': {'minimum_next_day_et': '07:00'}},
                'budgets': {'twelve_data_basic': {'hard_credits_per_day': 200, 'hard_credits_per_minute': 8}}}}
        self.access = {'twelve_data_basic': {'base_url': 'https://example.invalid',
                       'time_series_path': '/time_series', 'interval_daily': '1day'},
                       'http': {'timeout_seconds': 2, 'user_agent': 'offline-test'}}
        self.http_count = 0

    def new_repo(self, name):
        remote, repo = self.root / (name + '.git'), self.root / name
        git(self.root, 'init', '--bare', str(remote))
        git(self.root, 'clone', str(remote), str(repo))
        git(repo, 'config', 'user.name', 'offline-test')
        git(repo, 'config', 'user.email', 'offline@example.invalid')
        (repo / 'README.md').write_text('offline')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'seed')
        git(repo, 'branch', '-M', 'main')
        git(repo, 'push', 'origin', 'main')
        return remote, repo

    def payload(self, url, **kwargs):
        self.http_count += 1
        # Actual budget push and retention must have completed before HTTP.
        head = git(self.store_remote, 'rev-parse', 'refs/heads/main')
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/tags/market-data-read/' + head), head)
        return {'values': [dict(datetime=r['trade_date'], **{k: str(v) for k, v in r.items()
                           if k != 'trade_date'}) for r in reversed(self.new)]}

    def run_recovery(self, http=None):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)
        with patch.object(self.h.collectors, 'datetime', Clock), \
             patch.object(self.h.collectors, '_http_json', side_effect=http or self.payload):
            return self.m.recover(self.h, compute=self.compute, compute_sha=self.compute_sha,
                repo=self.repo, store_sha=self.base, day=DAY, request_id='123-1', config=self.config,
                universe=self.universe, access=self.access, key='SYNTHETIC', clock=lambda: NOW)

    def assert_no_remote_facts(self):
        head = git(self.store_remote, 'rev-parse', 'refs/heads/main')
        changes = git(self.repo, 'diff', '--name-only', self.base, head).splitlines()
        self.assertTrue(all(p.startswith('data/market-data/collector-state/') for p in changes), changes)

    def test_success_real_helpers_full_closure_and_one_fact_commit(self):
        result = self.run_recovery()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['recovered_count'], 2)
        self.assertEqual(result['requests_attempted'], 2)
        final = result['store_commit_sha']
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), final)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/tags/market-data-read/' + final), final)
        self.assertEqual(len(git(self.repo, 'rev-list', self.base + '..' + final, '--',
                                 'data/market-data/series').splitlines()), 1)
        archive = f'data/market-data/recovery/{DAY}/twelve_data_basic/daily-baseline-rebase/AAA.json'
        value = json.loads(git(self.repo, 'show', final + ':' + archive))
        self.assertEqual(value['old_records'], self.old)
        self.assertTrue(result['verified_required_outputs'])
        # Original budget checkout never receives partial rebuilt facts.
        self.assertEqual(self.h.store.read_daily_series(self.repo / 'data/market-data',
                         provider='twelve_data_basic', symbol='AAA'), self.old)

    def test_late_provider_failure_cannot_publish_partial_history(self):
        def http(url, **kwargs):
            if self.http_count:
                raise RuntimeError('PRIVATE provider failure')
            return self.payload(url, **kwargs)
        with self.assertRaises(Exception):
            self.run_recovery(http)
        self.assert_no_remote_facts()

    def test_late_invalid_replacement_cannot_publish_partial_history(self):
        def http(url, **kwargs):
            value = self.payload(url, **kwargs)
            if self.http_count == 2:
                value['values'][0]['volume'] = '-1'
            return value
        with self.assertRaises(Exception):
            self.run_recovery(http)
        self.assert_no_remote_facts()

    def test_corrupt_archive_prevents_fact_publication(self):
        original = self.h.rebaser._archive_old_series
        def corrupt(**kwargs):
            path = original(**kwargs)
            value = json.loads(path.read_text())
            value['old_records'][0]['close'] = 999.0
            path.write_text(json.dumps(value))
            return path
        with patch.object(self.h.rebaser, '_archive_old_series', side_effect=corrupt), self.assertRaises(Exception):
            self.run_recovery()
        self.assert_no_remote_facts()

    def test_corrupt_rebuilt_shard_prevents_fact_publication(self):
        original = self.h.rebaser._replace_series
        def corrupt(**kwargs):
            original(**kwargs)
            (kwargs['index_path'].parent / '000000.json').write_text('{}')
        with patch.object(self.h.rebaser, '_replace_series', side_effect=corrupt), self.assertRaises(Exception):
            self.run_recovery()
        self.assert_no_remote_facts()

    def test_interrupted_worktree_never_publishes_partial_history(self):
        with patch.object(self.h.rebaser, '_replace_series', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_recovery()
        self.assert_no_remote_facts()

    def test_unexpected_staged_path_blocks_publish(self):
        original = self.h.rebaser._replace_series
        def corrupt(**kwargs):
            original(**kwargs)
            (kwargs['store_root'].parents[1] / 'UNRELATED').write_text('not allowed')
        with patch.object(self.h.rebaser, '_replace_series', side_effect=corrupt), self.assertRaises(Exception):
            self.run_recovery()
        self.assert_no_remote_facts()

    def test_archive_collision_stops_before_http(self):
        path = self.repo / f'data/market-data/recovery/{DAY}/twelve_data_basic/daily-baseline-rebase/AAA.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'prior archive')
        git(self.repo, 'push', 'origin', 'main')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaises(Exception):
            self.run_recovery()
        self.assertEqual(self.http_count, 0)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), self.base)

    def test_failure_after_fact_push_is_unconfirmed_not_pass_or_retry(self):
        original = self.m.verify_outputs
        def reject(repo, sha, manifest, outputs, request_id, day):
            if any('/series/' in x['path'] for x in outputs):
                raise self.m.RecoveryError('publication_unconfirmed')
            return original(repo, sha, manifest, outputs, request_id, day)
        with patch.object(self.m, 'verify_outputs', side_effect=reject), self.assertRaises(self.m.RecoveryError) as ctx:
            self.run_recovery()
        self.assertTrue(ctx.exception.candidate_sha)
        self.assertEqual(git(self.store_remote, 'rev-parse', 'refs/heads/main'), ctx.exception.candidate_sha)
        self.assertEqual(self.http_count, 2)
        self.assertEqual(self.m.failure_summary(ctx.exception)['status'], 'BLOCKED')

    def test_source_drift_after_acquisition_stops_before_fact_push(self):
        original = self.h.rebaser._replace_series
        def drift(**kwargs):
            original(**kwargs)
            (self.compute / 'README.md').write_text('source drift')
        with patch.object(self.h.rebaser, '_replace_series', side_effect=drift), self.assertRaises(Exception):
            self.run_recovery()
        self.assert_no_remote_facts()
