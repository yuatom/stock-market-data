"""Synthetic HTTP/clock with real local Store, Git and durability verifier."""
import copy
import importlib
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import yaml
import test_guarded_history_recovery as fixture

NOW = datetime(2026, 10, 18, 8, 0, 10, tzinfo=timezone.utc)  # 04:00 ET


class HistoricalWorkflowTests(unittest.TestCase):
    def test_new_mode_manual_only_and_separate_from_old_modes(self):
        w = yaml.safe_load((fixture.ROOT / '.github/workflows/market-data-collector.yml').read_text())
        self.assertIn('daily_history_tail_backfill', w[True]['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn('daily_history_tail_backfill', w['jobs']['run']['if'])
        job = w['jobs']['historical_tail']
        self.assertEqual(job['timeout-minutes'], 35)
        self.assertEqual(w['jobs']['recovery']['timeout-minutes'], 12)
        self.assertEqual(job['needs'], 'selector_guard')
        self.assertIn('workflow_dispatch', job['if'])
        self.assertEqual(job['concurrency'], w['jobs']['recovery']['concurrency'])
        self.assertIn('backfill_market_data_tail.py', json.dumps(job))
        self.assertEqual(len(w[True]['schedule']), 11)
        self.assertNotIn('push', w[True])
        self.assertNotIn('pull_request', w[True])

    def test_selector_guard_executes_new_input_scope_not_ignored(self):
        w = yaml.safe_load((fixture.ROOT / '.github/workflows/market-data-collector.yml').read_text())
        code = w['jobs']['selector_guard']['steps'][0]['run']
        good = dict(MODE='daily_history_tail_backfill', FIXED_SELECTION='{}', EXECUTION_DATE='2030-06-10', HISTORICAL_CUTOFF='2030-06-07')
        with patch.dict(os.environ,good,clear=True):
            exec(compile(code,'<workflow-selector-guard>','exec'),{})
        for change in ({'DYNAMIC_SELECTION':'{}'},{'FIXED_SELECTION':''},{'EXECUTION_DATE':''},{'HISTORICAL_CUTOFF':''},
                       {'TRADE_DATE':'2030-06-10'},{'MODE':'daily_baseline_rebase'},{'MODE':'previous_session_eod'}):
            with self.subTest(change=change),patch.dict(os.environ,dict(good,**change),clear=True),self.assertRaises(SystemExit):
                exec(compile(code,'<workflow-selector-guard>','exec'),{})


class HistoricalPublicationTests(fixture.RecoveryIntegrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tail = importlib.import_module('backfill_market_data_tail')
        self.new = copy.deepcopy(self.old) + [dict(self.old[-1], trade_date='2026-10-16')]

    def selector(self, symbols=('AAA', 'BBB')):
        return {'schema_version': 1, 'targets': [dict(symbol=symbol, index_blob_sha=fixture.git(self.repo,
            'rev-parse', f'{self.base}:data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json')) for symbol in symbols]}

    def run_tail(self, http=None, selector=None, now=NOW, cutoff='2026-10-16'):
        self.assertTrue(hasattr(self.tail, 'backfill'), 'transactional historical entry absent')
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)
        with patch.object(self.h.collectors, 'datetime', Clock), patch.object(self.h.collectors, '_http_json', side_effect=http or self.payload):
            return self.tail.backfill(self.h, compute=self.compute, compute_sha=self.compute_sha,
                repo=self.repo, store_sha=self.base, execution_date=fixture.DAY, historical_cutoff=cutoff,
                request_id='321-1', config=self.config, universe=self.universe, access=self.access,
                key='SYNTHETIC', clock=lambda: now, fixed_selector=self.selector() if selector is None else selector)

    def test_before_today_seven_success_preserves_old_and_one_fact_commit(self):
        result = self.run_tail()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['mode'], self.tail.MODE)
        final = result['store_commit_sha']
        self.assertEqual(self.current_budget()['credits'], 2)
        self.assertEqual(fixture.git(self.store_remote, 'rev-parse', 'refs/heads/main'), final)
        self.assertEqual(fixture.git(self.store_remote, 'rev-parse', 'refs/tags/market-data-read/'+final), final)
        self.assertEqual(len(fixture.git(self.repo, 'rev-list', self.base+'..'+final, '--', 'data/market-data/series').splitlines()), 1)
        self.assertFalse(any('/recovery/' in x['path'] for x in result['verified_required_outputs']))
        for symbol in ('AAA', 'BBB'):
            prefix = f'data/market-data/series/daily/twelve_data_basic/{symbol}/'
            self.assertEqual(fixture.git(self.repo,'rev-parse',final+':'+prefix+'000000.json'), fixture.git(self.repo,'rev-parse',self.base+':'+prefix+'000000.json'))
        self.assertEqual(self.h.store.read_daily_series(self.repo/'data/market-data', provider='twelve_data_basic', symbol='AAA'), self.old)

    def test_insufficient_batch_budget_spends_nothing(self):
        self.seed_budget(199)
        with self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        self.assertEqual(self.current_budget()['credits'], 199)
        self.assertEqual(self.http_count, 0)
        self.assert_no_remote_facts()

    def test_old_conflict_stops_first_without_fact_or_refund(self):
        self.new[0]['volume'] += 1
        with self.assertRaises(Exception):
            self.run_tail()
        self.assertEqual(self.http_count, 1)
        self.assertEqual(self.current_budget()['credits'], 1)
        self.assert_no_remote_facts()

    def test_late_provider_failure_never_publishes_partial_facts(self):
        def http(url, **kwargs):
            if self.http_count:
                raise RuntimeError('synthetic PRIVATE')
            return self.payload(url, **kwargs)
        with self.assertRaises(Exception):
            self.run_tail(http)
        self.assertEqual(self.current_budget()['credits'], 2)
        self.assert_no_remote_facts()

    def test_missing_or_drifted_selector_stops_before_spend(self):
        for selector in ({}, {'schema_version':1, 'targets': []}, {'schema_version':1, 'targets':[{'symbol':'AAA','index_blob_sha':'0'*40}]}):
            with self.subTest(selector=selector), self.assertRaises(Exception):
                self.run_tail(selector=selector)
            self.assertEqual(self.http_count, 0)
            self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/heads/main'), self.base)

    def test_selected_subset_does_not_modify_unselected(self):
        result = self.run_tail(selector=self.selector(('AAA',)))
        path='data/market-data/series/daily/twelve_data_basic/BBB'
        self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+path), fixture.git(self.repo,'rev-parse',result['store_commit_sha']+':'+path))
        self.assertEqual(self.current_budget()['credits'], 1)

    def test_noop_has_no_fact_commit(self):
        result = self.run_tail(cutoff='2026-10-15')
        self.assertEqual(result['status'], 'NOOP')
        self.assert_no_remote_facts()

    def test_future_cutoff_stops_before_write(self):
        with self.assertRaises(Exception):
            self.run_tail(cutoff=fixture.DAY)
        self.assertEqual(self.http_count, 0)
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/heads/main'),self.base)

    def test_lost_fact_ack_does_not_publish_followup_or_retry(self):
        self.assertTrue(hasattr(self.tail, 'backfill'), 'transactional historical entry absent')
        original=self.a.git
        pushes=[]
        def git(repo,*args):
            if args and args[0]=='push':
                pushes.append((repo,args))
                if 'candidate' in str(repo):
                    original(repo,*args)
                    raise RuntimeError('lost synthetic ack')
            return original(repo,*args)
        with patch.object(self.a,'git',side_effect=git), self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(caught.exception.code, 'publication_unconfirmed')
        self.assertEqual(caught.exception.recovery_progress['terminal_report_outcome'], 'NOT_ATTEMPTED')
        self.assertEqual(sum('candidate' in str(p[0]) for p in pushes),1)
        self.assertEqual(str(pushes[-1][0]).split('/')[-1], 'candidate')

    def test_unexpected_path_during_staging_stops_all_facts(self):
        original=self.tail.stage_tail
        def stage(h, root, *args):
            result=original(h,root,*args)
            (root/'data/market-data/unexpected.json').write_text('{}')
            return result
        with patch.object(self.tail,'stage_tail',side_effect=stage), self.assertRaises(Exception):
            self.run_tail()
        self.assert_no_remote_facts()

    def test_unsafe_shard_is_rejected_before_any_history_read(self):
        path=self.repo/'data/market-data/series/daily/twelve_data_basic/AAA/_index.json'
        index=json.loads(path.read_text())
        index['shards'][0]['path']='../../outside.json'
        path.write_text(json.dumps(index))
        fixture.git(self.repo,'add','.')
        fixture.git(self.repo,'commit','-m','synthetic unsafe index')
        fixture.git(self.repo,'push','origin','main')
        self.base=fixture.git(self.repo,'rev-parse','HEAD')
        with patch.object(self.h.store,'read_daily_series',side_effect=AssertionError('must not follow shard')) as read:
            with self.assertRaises(Exception):
                self.run_tail()
            self.assertEqual(read.call_count,0)
        self.assertEqual(self.http_count,0)

    def test_late_target_cannot_corrupt_already_staged_first_target(self):
        original=self.h.store.append_daily_bars
        def append(root, **kwargs):
            result=original(root,**kwargs)
            if kwargs['symbol']=='BBB':
                index_path=root/'series/daily/twelve_data_basic/AAA/_index.json'
                index=json.loads(index_path.read_text())
                index['last_trade_date']='2030-01-01'
                index['lineage_segments']=[]
                index_path.write_text(json.dumps(index))
            return result
        with patch.object(self.h.store,'append_daily_bars',side_effect=append), self.assertRaises(Exception):
            self.run_tail()
        self.assert_no_remote_facts()

    def test_midnight_after_durable_debit_blocks_http_without_refund(self):
        original=self.a.guard_provider_request
        def guard(*args, **kwargs):
            kwargs['clock']=lambda: datetime(2026,10,19,4,0,10,tzinfo=timezone.utc)
            return original(*args, **kwargs)
        with patch.object(self.a,'guard_provider_request',side_effect=guard), self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(caught.exception.code,'invalid_target')
        self.assertEqual(self.http_count,0)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assert_no_remote_facts()

    def test_expired_minute_after_debit_blocks_http_without_refund(self):
        original=self.a.guard_provider_request
        def guard(*args, **kwargs):
            kwargs['clock']=lambda: NOW.replace(minute=1)
            return original(*args, **kwargs)
        with patch.object(self.a,'guard_provider_request',side_effect=guard), self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(caught.exception.code,'budget_slot_expired')
        self.assertEqual(self.http_count,0)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assert_no_remote_facts()

    def test_compute_drift_after_first_request_stops_second(self):
        def http(url,**kwargs):
            value=self.payload(url,**kwargs)
            (self.compute/'README.md').write_text('synthetic dirty Compute')
            return value
        with self.assertRaises(Exception) as caught:
            self.run_tail(http)
        self.assertEqual(caught.exception.code,'object_drift')
        self.assertEqual(self.http_count,1)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assert_no_remote_facts()

    def test_lost_predebit_ack_stops_without_http_or_followup(self):
        original=self.a.git
        pushes=[]
        def git(repo,*args):
            if args and args[0]=='push':
                pushes.append((repo,args))
                if len(pushes)==2:
                    original(repo,*args)
                    raise RuntimeError('synthetic lost debit ack')
            return original(repo,*args)
        with patch.object(self.a,'git',side_effect=git),self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(len(pushes),2)
        self.assertEqual(self.http_count,0)
        self.assertEqual(caught.exception.recovery_progress['budget_debits_unconfirmed'],1)
        self.assertEqual(caught.exception.recovery_progress['terminal_report_outcome'],'NOT_ATTEMPTED')
        self.assert_no_remote_facts()

    def test_fact_closure_failure_is_uncertain_not_retryable(self):
        original=self.m.verify_outputs
        calls=[]
        def verify(repo,sha,manifest,outputs,*args,**kwargs):
            calls.append(sha)
            if len(outputs)>1:
                raise self.m.RecoveryError('closure_invalid')
            return original(repo,sha,manifest,outputs,*args,**kwargs)
        with patch.object(self.m,'verify_outputs',side_effect=verify),self.assertRaises(Exception) as caught:
            self.run_tail()
        self.assertEqual(caught.exception.code,'publication_unconfirmed')
        self.assertEqual(caught.exception.recovery_progress['terminal_report_outcome'],'NOT_ATTEMPTED')
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/heads/main'),calls[-1])
        self.assertEqual(self.http_count,2)

    def test_mixed_noop_and_append_preserves_noop_series(self):
        # BBB already reaches cutoff. Its candidate must still be independently compared.
        self.h.store.append_daily_bars(self.repo/'data/market-data',provider='twelve_data_basic',symbol='BBB',
            asset_class='etf',bars=[self.new[-1]],series_semantics='daily_regular_ohlcv',shard_record_limit=2)
        fixture.git(self.repo,'add','.')
        fixture.git(self.repo,'commit','-m','synthetic BBB already complete')
        fixture.git(self.repo,'push','origin','main')
        self.base=fixture.git(self.repo,'rev-parse','HEAD')
        path='data/market-data/series/daily/twelve_data_basic/BBB'
        before=fixture.git(self.repo,'rev-parse',self.base+':'+path)
        result=self.run_tail()
        self.assertEqual(result['appended_count'],1)
        self.assertEqual(fixture.git(self.repo,'rev-parse',result['store_commit_sha']+':'+path),before)
        self.assertEqual(self.http_count,2)
