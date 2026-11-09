"""Invented inputs only; no provider access or production Store qualification."""
from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
ET = ZoneInfo('America/New_York')


def module():
    assert (ROOT / 'scripts/backfill_market_data_tail.py').is_file(), 'historical tail entry absent'
    return importlib.import_module('backfill_market_data_tail')


def bar(day):
    return dict(trade_date=day, open=10.0, high=12.0, low=9.0, close=11.0, volume=100.0)


class HistoricalTimeTests(unittest.TestCase):
    def test_mature_past_cutoff_before_execution_day_seven(self):
        m = module()
        now = datetime(2030, 6, 10, 4, tzinfo=ET)
        self.assertEqual(m.validate_dates('2030-06-10', '2030-06-07', '07:00', now), '2030-06-08')

    def test_cutoff_next_day_boundary_inclusive(self):
        m = module()
        with self.assertRaises(m.RecoveryError):
            m.validate_dates('2030-06-08', '2030-06-07', '07:00', datetime(2030, 6, 8, 6, 59, tzinfo=ET))
        self.assertEqual(m.validate_dates('2030-06-08', '2030-06-07', '07:00', datetime(2030, 6, 8, 7, tzinfo=ET)), '2030-06-08')

    def test_bad_dates_naive_clock_future_and_midnight_rollover(self):
        m = module()
        now = datetime(2030, 6, 10, 4, tzinfo=ET)
        for day, cutoff, maturity, clock in [
            ('2030-06-09', '2030-06-07', '07:00', now),
            ('2030-06-10', '2030-06-10', '07:00', now),
            ('2030-06-10', '2030-06-11', '07:00', now),
            ('2030-6-10', '2030-06-07', '07:00', now),
            ('2030-06-10', '2030-02-30', '07:00', now),
            ('2030-06-10', '2030-06-07', 'garbage', now),
            ('2030-06-10', '2030-06-07', '07:00', now.replace(tzinfo=None)),
            ('2030-06-10', '2030-06-07', '07:00', datetime(2030, 6, 11, 0, tzinfo=ET)),
        ]:
            with self.subTest(day=day, cutoff=cutoff, clock=clock), self.assertRaises(m.RecoveryError):
                m.validate_dates(day, cutoff, maturity, clock)

    def test_dst_uses_local_next_calendar_day_not_fixed_24_hours(self):
        m = module()
        self.assertEqual(m.validate_dates('2030-03-10', '2030-03-09', '07:00', datetime(2030, 3, 10, 7, tzinfo=ET)), '2030-03-10')


class HistoricalQualificationTests(unittest.TestCase):
    def setUp(self):
        self.m = module()
        self.h = importlib.import_module('admit_market_data_baseline').load_helpers()
        self.old = [bar('2030-06-03'), bar('2030-06-04'), bar('2030-06-05')]
        self.item = dict(symbol='AAA', existing=copy.deepcopy(self.old), target_records=3, effective=None, outputsize=10)
        self.fetched = self.old + [bar('2030-06-06'), bar('2030-06-07')]

    def test_zero_revision_strict_tail_and_inclusive_cutoff(self):
        self.m.qualify_tail(self.h, self.item, [bar('2030-06-02')] + self.fetched + [bar('2030-06-08')], '2030-06-07')
        self.assertEqual(self.item['rows'], self.fetched)
        self.assertEqual(self.item['new_rows'], self.fetched[3:])
        self.assertEqual(self.item['summary']['new_records'], 2)
        self.assertFalse(self.item['summary']['exchange_calendar_completeness_established'])
        self.assertEqual(self.item['existing'], self.old)

    def test_any_old_numeric_revision_even_final_row_blocks(self):
        for offset in (0, 2):
            changed = copy.deepcopy(self.fetched)
            changed[offset]['volume'] += 1
            with self.subTest(offset=offset), self.assertRaises(Exception):
                self.m.qualify_tail(self.h, self.item, changed, '2030-06-07')

    def test_missing_old_duplicate_internal_gap_and_invalid_numeric_block(self):
        cases = [self.fetched[1:], self.fetched + [self.fetched[0]],
                 self.fetched[:-1], self.fetched[:-1] + [dict(self.fetched[-1], close=float('nan'))]]
        for fetched in cases:
            with self.subTest(fetched=fetched), self.assertRaises(Exception):
                self.m.qualify_tail(self.h, self.item, fetched, '2030-06-07')
        item = dict(self.item, existing=[self.old[0], self.old[2]])
        with self.assertRaises(self.m.RecoveryError):
            self.m.qualify_tail(self.h, item, self.fetched, '2030-06-07')

    def test_cutoff_behind_existing_never_truncates_and_no_new_is_noop(self):
        with self.assertRaises(self.m.RecoveryError):
            self.m.qualify_tail(self.h, self.item, self.fetched, '2030-06-04')
        self.m.qualify_tail(self.h, self.item, self.old, '2030-06-05')
        self.assertEqual(self.item['new_rows'], [])
        self.assertEqual(self.item['rows'], self.old)

    def test_strict_source_timestamp_or_type_difference_not_normalized_away(self):
        for update in ({'close': 11}, {'source_timestamp': '2030-06-03T16:00:00-04:00'}):
            changed = copy.deepcopy(self.fetched)
            changed[0].update(update)
            with self.subTest(update=update), self.assertRaises(Exception):
                self.m.qualify_tail(self.h, self.item, changed, '2030-06-07')

    def test_discarded_prefix_cannot_satisfy_retained_history_minimum(self):
        self.item['target_records']=6
        fetched=[bar('2030-06-01'),bar('2030-06-02')]+self.fetched
        with self.assertRaises(self.m.RecoveryError):
            self.m.qualify_tail(self.h,self.item,fetched,'2030-06-07')

    def test_shared_missing_session_is_not_a_calendar_completeness_claim(self):
        self.item['existing']=[self.old[0],self.old[2]]
        self.m.qualify_tail(self.h,self.item,[self.old[0],self.old[2],bar('2030-06-07')],'2030-06-07')
        self.assertFalse(self.item['summary']['exchange_calendar_completeness_established'])

    def test_nested_failure_keeps_new_mode_and_no_private_text(self):
        exc=self.m.RecoveryError('publication_failed')
        exc.terminal_report_failure={'failure_code':'publication_failed','private':'SECRET'}
        value=self.m.failure_summary(exc)
        self.assertEqual(value['mode'],self.m.MODE)
        self.assertEqual(value['terminal_report_failure']['mode'],self.m.MODE)
        self.assertNotIn('SECRET',json.dumps(value))


class HistoricalStoreTests(unittest.TestCase):
    def test_real_append_keeps_old_rows_sealed_shards_and_adds_lineage(self):
        m = module()
        h = importlib.import_module('admit_market_data_baseline').load_helpers()
        old = [bar('2030-06-03'), bar('2030-06-04'), bar('2030-06-05')]
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            root = stage / 'data/market-data'
            h.store.append_daily_bars(root, provider='twelve_data_basic', symbol='AAA', asset_class='etf', bars=old,
                series_semantics='daily_regular_ohlcv', shard_record_limit=2)
            prefix = 'data/market-data/series/daily/twelve_data_basic/AAA/'
            index = json.loads((stage / (prefix + '_index.json')).read_text())
            item = dict(symbol='AAA', asset_class='etf', existing=old, target_records=3, effective=None, outputsize=10,
                        old_index=index, index_relative=prefix+'_index.json',
                        old_files={str(p.relative_to(stage)): h.store.git_blob_sha_bytes(p.read_bytes()) for p in (stage/prefix).iterdir()})
            sealed = (stage / (prefix + '000000.json')).read_bytes()
            m.qualify_tail(h, item, old + [bar('2030-06-06'), bar('2030-06-07')], '2030-06-07')
            outputs = m.stage_tail(h, stage, [item], '2030-06-10', '2030-06-07')
            self.assertEqual(h.store.read_daily_series(root, provider='twelve_data_basic', symbol='AAA'), item['rows'])
            self.assertEqual((stage / (prefix + '000000.json')).read_bytes(), sealed)
            result_index = json.loads((stage / (prefix + '_index.json')).read_text())
            self.assertEqual(result_index['lineage_segments'][-1]['execution_date'], '2030-06-10')
            self.assertEqual(result_index['lineage_segments'][-1]['historical_cutoff'], '2030-06-07')
            self.assertIn(prefix+'_index.json', outputs)
            self.assertFalse((root/'recovery').exists())
