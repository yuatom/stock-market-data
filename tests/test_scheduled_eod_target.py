from datetime import datetime,timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import unittest
from scripts import market_data_collectors as C
from scripts.reviewed_equity_calendar import previous_eligible_eod_session

class ScheduledEod(unittest.TestCase):
    def test_friday_to_monday_and_calendar_failure(self):
        self.assertEqual(previous_eligible_eod_session('2026-11-30'),'2026-11-27')
        self.assertIsNone(previous_eligible_eod_session('2026-11-26'))
        self.assertIsNone(previous_eligible_eod_session('2027-01-04'))

    def test_qualified_target_skips_before_debit_and_stale_is_partial(self):
        config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                    'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':1}}}
        now=datetime(2026,11,30,12,5,tzinfo=timezone.utc)
        complete=dict(trade_date='2026-11-27',open='3',high='4',low='2',close='3',volume='10')
        stale=complete|{'trade_date':'2026-11-25'}
        with TemporaryDirectory() as path, patch.dict(C.os.environ,{'TWELVE_DATA_API_KEY':'synthetic'}), \
             patch.object(C,'_daily_universe',return_value=[('READY','etf'),('MISSING','etf'),('SOXX','etf')]), \
             patch.object(C,'_load_daily_basis_owner',return_value={'SOXX':'OWNED_IMMUTABLE_DAILY'}), \
             patch.object(C,'assert_legacy_basis'),patch.object(C,'_identity_effective_date',return_value=None), \
             patch.object(C,'read_affine_ordinary_daily_series',side_effect=lambda *args,**kw:([complete] if kw['symbol']=='READY' else [stale],[])), \
             patch.object(C,'_series_index_path',return_value=Path(path)), \
             patch.object(C,'wait_for_twelve_budget') as wait,patch.object(C,'consume_twelve_credit') as debit, \
             patch.object(C,'fetch_twelve_daily',return_value=[stale]) as fetch, \
             patch.object(C,'append_daily_bars') as append,patch.object(C,'_write_private_eod_diagnostic'):
            result=C.collect_previous_session_eod(trade_date='2026-11-30',store_root=Path(path),
                     watchlist={},completeness={},config=config,access={},scheduled_target_guard=True,now_et=now)
        self.assertEqual(result['target_eod_session'],'2026-11-27')
        self.assertEqual(result['accounting_et_date'],'2026-11-30')
        self.assertEqual(result['target_already_qualified_count'],1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['target_eod_status'],'partial')
        self.assertEqual(result['failures'],[{'symbol':'MISSING','reason':'target_eod_session_missing'}])
        self.assertEqual(wait.call_count,1)
        self.assertEqual(debit.call_args.args[1],'2026-11-30')
        self.assertEqual(fetch.call_args.args[0],'MISSING')
        append.assert_not_called()

    def test_no_volume_requires_exact_blocked_lineage_and_manual_not_skipped(self):
        config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                    'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':1}}}
        now=datetime(2026,11,30,12,5,tzinfo=timezone.utc)
        complete=dict(trade_date='2026-11-27',open='3',high='4',low='2',close='3')
        for mark,scheduled,should_skip in ((True,True,True),(False,True,False),(True,False,False)):
            with self.subTest(mark=mark,scheduled=scheduled), TemporaryDirectory() as path, \
                 patch.dict(C.os.environ,{'TWELVE_DATA_API_KEY':'synthetic'}), \
                 patch.object(C,'_daily_universe',return_value=[('READY','etf')]), \
                 patch.object(C,'_load_daily_basis_owner',return_value={}),patch.object(C,'assert_legacy_basis'), \
                 patch.object(C,'_identity_effective_date',return_value=None), \
                 patch.object(C,'read_affine_ordinary_daily_series',return_value=([complete],[{'start':0,'end':0,'volume_policy':'BLOCKED_UNRESOLVED'}] if mark else [])), \
                 patch.object(C,'wait_for_twelve_budget'),patch.object(C,'consume_twelve_credit') as debit, \
                 patch.object(C,'fetch_twelve_daily',return_value=[]) as fetch, \
                 patch.object(C,'_write_private_eod_diagnostic'):
                index=Path(path)/'_index.json'
                index.write_text('{"lineage_segments":[{"start":0,"end":0,"volume_policy":"BLOCKED_UNRESOLVED"}]}' if mark else '{}')
                with patch.object(C,'_series_index_path',return_value=index):
                    C.collect_previous_session_eod(trade_date='2026-11-30',store_root=Path(path),
                        watchlist={},completeness={},config=config,access={},scheduled_target_guard=scheduled,now_et=now)
                self.assertEqual(fetch.call_count,0 if should_skip else 1)
                self.assertEqual(debit.call_count,0 if should_skip else 1)

    def test_workflow_flag_only_on_existing_eod_schedule(self):
        import yaml
        wf=Path(__file__).resolve().parents[1]/'.github/workflows/market-data-collector-runtime.yml'
        steps=yaml.safe_load(wf.read_text())['jobs']['collect']['steps']
        selected=next(s for s in steps if s.get('name','').startswith('Collect qualified facts'))
        self.assertIn("inputs.trigger_kind == 'schedule' && inputs.schedule == '5 7 * * 1-5'",selected['env']['SCHEDULED_ORDINARY_EOD'])
        self.assertIn('extra+=(--scheduled-eod-target-guard)',selected['run'])

    def test_self_consistent_wrong_series_never_counts_as_qualified(self):
        import shutil, subprocess
        from scripts import market_data_store as S
        config={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                    'twelve_data_daily':{'history_outputsize':500,'bootstrap_minimum_records':1}}}
        now=datetime(2026,11,30,12,5,tzinfo=timezone.utc)
        with TemporaryDirectory() as scratch:
            root=Path(scratch)
            S.append_daily_bars(root,provider=C.TWELVE_PROVIDER,symbol='OTHER',asset_class='etf',
                bars=[dict(trade_date='2026-11-27',open=3,high=4,low=2,close=3,volume=6)],
                series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported')
            series=root/'series'/'daily'/C.TWELVE_PROVIDER
            shutil.copytree(series/'OTHER',series/'READY')
            def git(*args):
                return subprocess.check_output(['git','-C',scratch,'-c','user.name=Test',
                    '-c','user.email=test@example.invalid',*args])
            git('init','-q');git('add','.');git('commit','-qm','Synthetic retained wrong series')
            self.assertEqual(S.read_daily_exact(root,provider=C.TWELVE_PROVIDER,symbol='READY',
                trade_date='2026-11-27')['close'],3)
            with patch.dict(C.os.environ,{'TWELVE_DATA_API_KEY':'synthetic'}), \
                 patch.object(C,'_daily_universe',return_value=[('READY','etf')]), \
                 patch.object(C,'wait_for_twelve_budget') as wait,patch.object(C,'consume_twelve_credit') as debit, \
                 patch.object(C,'fetch_twelve_daily') as fetch,patch.object(C,'_write_private_eod_diagnostic'):
                result=C.collect_previous_session_eod(trade_date='2026-11-30',store_root=root,
                    watchlist={},completeness={},config=config,access={},scheduled_target_guard=True,now_et=now)
            self.assertEqual(result['target_already_qualified_count'],0)
            self.assertEqual(result['target_eod_unqualified_count'],1)
            self.assertEqual(result['target_eod_status'],'partial')
            self.assertIn('stored_series_integrity_conflict',str(result['failures']))
            self.assertEqual(git('status','--porcelain'),b'')
            for call in (wait,debit,fetch):call.assert_not_called()
