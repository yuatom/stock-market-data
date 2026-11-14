"""Real local bare Git controller/tail/publisher chain; synthetic HTTP only."""
import copy
import importlib
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from unittest.mock import patch
from datetime import datetime

# Preload candidate modules before baseline fixture adjusts its import path.
import maintain_fixed_symbols as maintenance
import backfill_market_data_tail as tail
maintenance.a.load_helpers()
sys.path.append(str(Path(__file__).resolve().parent))
import test_guarded_history_recovery as fixture

class AppendGitChainTests(fixture.RecoveryIntegrationFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.h.store.append_daily_bars(self.repo/'data/market-data',provider='twelve_data_basic',symbol='CCC',asset_class='etf',bars=self.old,series_semantics='daily_regular_ohlcv',shard_record_limit=2)
        fixture.git(self.repo,'add','.');fixture.git(self.repo,'commit','-m','synthetic third target');fixture.git(self.repo,'push','origin','main')
        self.base=fixture.git(self.repo,'rev-parse','HEAD')
        self.universe['daily_series'].append(dict(symbol='CCC',asset_class='etf'))
        self.config['collector']['twelve_data_daily']['bounded_overlap_revision_policy']='price_only_volume_blocked_v1'
        self.selector={'schema_version':1,'targets':[dict(symbol=s,index_blob_sha=fixture.git(self.repo,'rev-parse',f'{self.base}:data/market-data/series/daily/twelve_data_basic/{s}/_index.json')) for s in ('AAA','BBB','CCC')]}

    def run_append(self,*,reject_middle=False,reject_first=False,unknown_middle=False):
        now=fixture.NOW
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return now if tz is None else now.astimezone(tz)
        def http(url,**kwargs):
            symbol=parse_qs(urlparse(url).query)['symbol'][0]
            result=self.payload(url,**kwargs)
            if (reject_middle and symbol=='BBB') or (reject_first and symbol=='AAA'):
                result['values'][-1]['open']='10.1'
            return result
        original_git=maintenance.a.git
        def git(root,*args,**kwargs):
            value=original_git(root,*args,**kwargs)
            if unknown_middle and len(args)>=3 and args[0]=='push' and args[1]=='origin' and args[2].endswith(':refs/heads/main'):
                message=fixture.git(root,'log','-1','--format=%s')
                if message=='data: publish guarded historical tail append' and self.http_count==2:
                    raise RuntimeError('SYNTHETIC_PUSH_REPLY_UNKNOWN_AFTER_LOCAL_APPLY')
            return value
        with patch.object(self.h.collectors,'datetime',Clock),patch.object(self.h.collectors,'_http_json',side_effect=http),patch.object(maintenance.a,'git',side_effect=git):
            return maintenance.maintain_append(self.h,compute=self.compute,compute_sha=self.compute_sha,repo=self.repo,store_sha=self.base,execution_date=fixture.DAY,historical_cutoff='2026-10-16',request_id='876-1',config=self.config,universe=self.universe,access=self.access,key='SYNTHETIC',fixed_selector=self.selector,clock=lambda:now)

    def test_append_batch_continues_after_old_ten_minute_limit(self):
        elapsed = [0.0]
        original = tail.backfill
        def slow_child(*args, **kwargs):
            result = original(*args, **kwargs)
            elapsed[0] += 400
            return result
        with patch.object(maintenance.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(tail, 'backfill', side_effect=slow_child):
            result = self.run_append()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(self.http_count, 3)
        plan = __import__('json').loads(fixture.git(self.repo, 'show', result['batch_manifest_commit_sha'] + ':' + result['batch_manifest_path']))
        self.assertEqual(plan['overall_timeout_seconds'], 1800)

    def test_real_append_rejection_stops_before_next_provider_request(self):
        result=self.run_append(reject_middle=True)
        self.assertEqual(result['status'],'PARTIAL',result)
        self.assertEqual(result['stop']['failure_code'],'replacement_rejected',result)
        self.assertEqual(result['stop']['failed_symbol'],'BBB',result)
        self.assertEqual(result['not_attempted_symbols'],['CCC'])
        self.assertEqual([r['status'] for r in result['transactions']],['PASS','QUARANTINED'],result)
        self.assertEqual(self.http_count,2)
        self.assertEqual(self.current_budget()['credits'],2)
        head=fixture.git(self.store_remote,'rev-parse','main')
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/tags/market-data-read/'+head),head)
        for symbol in ('AAA','BBB','CCC'):
            _,rows,_=maintenance._immutable_series(self.repo,head,symbol,self.h.store)
            self.assertEqual(rows[:len(self.old)],self.old)
            self.assertEqual(len(rows),len(self.old)+(symbol=='AAA'))

    def test_real_push_unknown_stops_next_request(self):
        result=self.run_append(unknown_middle=True)
        self.assertEqual(result['status'],'PARTIAL',result)
        self.assertIsNotNone(result['stop'],result)
        self.assertEqual(self.http_count,2,result)
        self.assertEqual(result['not_attempted_symbols'],['CCC'])
        self.assertFalse(result['automatic_retry_allowed'])

    def test_real_append_continues_after_minute_bucket_rolloff(self):
        self.seed_budget(158, {f'{fixture.DAY}T10:{minute:02d}Z': 8 for minute in range(16)})
        result=self.run_append()
        self.assertEqual(result['status'],'PASS',result)
        self.assertIsNone(result['stop'],result)
        self.assertEqual(self.http_count,3)
        self.assertEqual(self.current_budget()['credits'],161)
        self.assertEqual(len(self.current_budget()['minutes']),16)
        self.assertNotIn(f'{fixture.DAY}T10:00Z',self.current_budget()['minutes'])
        self.assertEqual([row['status'] for row in result['transactions']],['PASS']*3)

    def test_first_rejection_stops_without_spending_next_credit(self):
        result=self.run_append(reject_first=True)
        self.assertEqual(result['status'],'BLOCKED',result)
        self.assertEqual(result['stop']['failure_code'],'replacement_rejected')
        self.assertEqual(result['stop']['failed_symbol'],'AAA')
        self.assertEqual(result['not_attempted_symbols'],['BBB','CCC'])
        self.assertEqual(self.http_count,1)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assertEqual(result['published_count'],0)
        self.assertEqual(result['quarantined_symbols'],['AAA'])
        self.assertFalse(result['automatic_retry_allowed'])
