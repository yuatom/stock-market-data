"""Real local Store with synthetic provider rows; no credentials or network."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from scripts import market_data_collectors as c

class DailyVolumeIsolationTests(unittest.TestCase):
    def run_case(self, changes=None, blocked=(), policy=True, no_new=False, other_price=False):
        changes=changes or {}
        old=dict(trade_date='2026-11-04',open=10.,high=12.,low=9.,close=11.,volume=100.)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp); symbols=('AAA','BBB','CCC'); before={}
            initial=([dict(old,trade_date='2026-11-03')] if other_price else [])+[old]
            for symbol in symbols:
                lineage={'ingest_kind':'collector_previous_session_eod'}
                if symbol in blocked:lineage.update(ingest_kind='collector_price_only_revision_v1',volume_policy='BLOCKED_UNRESOLVED')
                c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol=symbol,asset_class='stocks',bars=initial,series_semantics='daily_regular_ohlcv',lineage=lineage)
                before[symbol]=c.read_daily_series(root,provider=c.TWELVE_PROVIDER,symbol=symbol)
            config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},'twelve_data_daily':{'history_outputsize':64,'bootstrap_minimum_records':1}}}
            if policy:config['collector']['twelve_data_daily']['overlap_revision_policy']='price_only_volume_blocked_v1'
            stack.enter_context(patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}))
            stack.enter_context(patch.object(c,'_daily_universe',return_value=[(s,'stocks') for s in symbols]))
            stack.enter_context(patch.object(c,'_load_daily_basis_owner',return_value={}))
            stack.enter_context(patch.object(c,'_identity_effective_date',return_value=None))
            for name in ('wait_for_twelve_budget','consume_twelve_credit'):stack.enter_context(patch.object(c,name))
            def fetched(symbol,*args,**kwargs):
                previous=[dict(old,trade_date='2026-11-03',open=10.01 if symbol=='AAA' else 10.)] if other_price else []
                return previous+[dict(old,**changes.get(symbol,{}))]+([] if no_new else [dict(old,trade_date='2026-11-05')])
            stack.enter_context(patch.object(c,'fetch_twelve_daily',side_effect=fetched))
            result=c.collect_previous_session_eod(trade_date='2026-11-06',store_root=root,watchlist={},completeness={},config=config,access={'twelve_data_basic':{'base_url':'https://example.invalid','time_series_path':'/time_series','interval_daily':'1day'}})
            rows={s:c.read_daily_series(root,provider=c.TWELVE_PROVIDER,symbol=s) for s in symbols}
            indexes={s:json.loads(c._series_index_path(root,c.TWELVE_PROVIDER,s).read_text()) for s in symbols}
            for s in symbols:self.assertEqual(rows[s][:len(initial)],before[s])
            return result,rows,indexes

    def test_clean_symbols_keep_volume_and_ordinary_lineage(self):
        result,rows,indexes=self.run_case()
        self.assertEqual(result.get('volume_blocked_symbols'),[])
        for s in indexes:
            self.assertEqual(indexes[s]['lineage_segments'][-1]['ingest_kind'],'collector_previous_session_eod')
            self.assertNotIn('volume_policy',indexes[s]['lineage_segments'][-1])
            self.assertEqual(rows[s][-1]['volume'],100.)

    def test_volume_conflict_is_isolated_to_one_symbol(self):
        result,rows,indexes=self.run_case({'AAA':{'volume':101.}})
        self.assertEqual(result.get('volume_blocked_symbols'),['AAA'])
        self.assertEqual(indexes['AAA']['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        for s in ('BBB','CCC'):self.assertNotIn('volume_policy',indexes[s]['lineage_segments'][-1])
        self.assertEqual(result['changed_series'],3)

    def test_existing_block_is_not_released_on_equal_overlap(self):
        result,rows,indexes=self.run_case(blocked=('BBB',))
        self.assertEqual(result.get('volume_blocked_symbols'),['BBB'])
        self.assertEqual(indexes['BBB']['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        self.assertNotIn('volume_policy',indexes['CCC']['lineage_segments'][-1])

    def test_numeric_equivalence_does_not_block_volume(self):
        result,rows,indexes=self.run_case({'AAA':{'open':'10.000'}})
        self.assertEqual(result.get('volume_blocked_symbols'),[])
        self.assertNotIn('volume_policy',indexes['AAA']['lineage_segments'][-1])

    def test_price_and_metadata_conflicts_still_reject(self):
        for change in ({'open':10.01},{'source_timestamp':'untrusted'}):
            with self.subTest(change=change):
                result,rows,indexes=self.run_case({'AAA':change})
                self.assertEqual(len(rows['AAA']),1)
                self.assertEqual(len(rows['BBB']),2)
                self.assertEqual(result['failure_target_count'],1)

    def test_policy_absent_preserves_legacy_behavior(self):
        result,rows,indexes=self.run_case({'AAA':{'volume':101.}},policy=False)
        self.assertEqual(len(rows['AAA']),1)
        self.assertNotIn('volume_blocked_symbols',result)

    def test_volume_conflict_without_new_rows_is_durably_quarantined(self):
        result,rows,indexes=self.run_case({'AAA':{'volume':101.}},no_new=True)
        self.assertEqual(len(rows['AAA']),1)
        self.assertEqual(indexes['AAA']['lineage_segments'][-1].get('volume_policy'),'BLOCKED_UNRESOLVED')
        self.assertEqual(result['volume_quarantined_series'],1)
        self.assertEqual(result['changed_series'],0)

    def test_volume_conflict_is_quarantined_even_when_other_price_rejects(self):
        result,rows,indexes=self.run_case({'AAA':{'volume':101.}},other_price=True)
        self.assertEqual(len(rows['AAA']),2)
        self.assertEqual(indexes['AAA']['lineage_segments'][-1].get('volume_policy'),'BLOCKED_UNRESOLVED')
        self.assertEqual(result['failure_target_count'],1)

    def test_existing_block_does_not_create_duplicate_quarantine(self):
        result,rows,indexes=self.run_case({'BBB':{'volume':101.}},blocked=('BBB',),no_new=True)
        self.assertEqual(len(indexes['BBB']['lineage_segments']),1)
        self.assertEqual(result['volume_quarantined_series'],0)

    def test_same_row_volume_and_price_change_blocks_volume_and_rejects_price(self):
        result,rows,indexes=self.run_case({'AAA':{'volume':101.,'open':10.01}})
        self.assertEqual(len(rows['AAA']),1)
        self.assertEqual(indexes['AAA']['lineage_segments'][-1].get('volume_policy'),'BLOCKED_UNRESOLVED')
        self.assertEqual(result['failure_target_count'],1)
