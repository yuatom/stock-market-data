"""Real runner/CLI/predecessor control flow, simulated clocks and no I/O."""
import contextlib,io,unittest
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
import run_fixed_guarded_candidate as runner
import collect_market_data as entry
class DeadlineTimingTests(unittest.TestCase):
    def exercise(self,mode,clock,*,cutoff_before_runner=False):
        wall=[datetime.fromisoformat('2026-11-17T'+clock).replace(tzinfo=ZoneInfo('America/New_York'))]
        mono=[1000.];events=[];constructed=[]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return wall[0] if tz is None else wall[0].astimezone(tz)
        def sleep(seconds):wall[0]+=timedelta(seconds=seconds);mono[0]+=seconds
        class Factory:
            def __init__(self,**kwargs):self.deadline=kwargs['deadline'];constructed.append((mono[0],self.deadline))
            def __call__(self,**kwargs):events.append((kwargs['mode'],wall[0].isoformat(),mono[0],self.deadline));return self
        def collect(args,*,owner_factory):
            owner_factory(mode=entry._arg_value(args,'--mode'));sleep(20);return 0
        args=['--mode',mode,'--trade-date','2026-11-17','--store-root','/synthetic/data/market-data','--universe','synthetic.json']
        metadata=dict(mode=mode,trade_date='2026-11-17',market_data_contract_sha='a'*40,logical_trigger='schedule' if clock<'10:30' else 'push')
        with patch.object(entry,'datetime',Clock),patch.object(runner.time,'monotonic',side_effect=lambda:mono[0]),patch.object(entry.time,'sleep',side_effect=sleep),patch.object(runner,'git_environment',return_value={}),patch.object(runner,'git',return_value='a'*40),patch.object(runner,'check_store_remote'),patch.object(runner,'WorkflowFixedOwnerFactory',Factory),patch.object(runner,'FixedStagePublisher',return_value=lambda args:None),patch.object(entry.collection,'load_collection_universe',return_value={}),patch.object(entry,'_live_close_universe',return_value=[]),patch.object(entry,'_settlement_gate_result',return_value=None),patch.object(entry,'_snapshot_materialized',return_value=False),patch.object(entry,'_run_live_close',return_value=None),patch.object(entry,'_build_metric_proof_if_applicable'),patch.object(entry.runtime,'main',side_effect=collect),contextlib.redirect_stdout(io.StringIO()):
            if cutoff_before_runner:entry._wait_until_regular_stage_cutoff(args)
            result=runner.run(args,metadata,repo=Path('/synthetic'),compute=runner.ROOT,run_id='1-1',env=dict(MARKET_DATA_STORE_TOKEN='SYNTHETIC',MARKET_DATA_STORE_INPUT_SHA='a'*40))
        self.assertEqual(result,0)
        return constructed,events
    def test_current_prewarm_consumes_shared_owner_budget(self):
        for mode,start,cutoff in [('open_15m','09:40:00','09:45:00'),('open_30m','09:55:00','10:00:00'),('open_60m','10:25:00','10:30:00')]:
            with self.subTest(mode=mode):
                made,events=self.exercise(mode,start)
                self.assertEqual(made,[(1000.,1600.)]);self.assertEqual(events[0][2],1300.)
                self.assertIn('T'+cutoff,events[0][1]);self.assertEqual(events[0][3]-events[0][2],300.)
                self.assertEqual({event[3] for event in events},{1600.})
    def test_ondemand_after_cutoff_has_no_prewarm_deduction(self):
        made,events=self.exercise('open_60m','10:30:05')
        self.assertEqual(made,[(1000.,1600.)]);self.assertEqual(events[0][2],1000.)
        self.assertEqual([x[0] for x in events],['open_15m','open_30m','open_60m'])
    def test_cutoff_first_preserves_windows_and_one_deadline_but_exceeds_job(self):
        made,events=self.exercise('open_60m','10:25:00',cutoff_before_runner=True)
        self.assertEqual(made,[(1300.,1900.)])
        self.assertEqual([x[0] for x in events],['open_15m','open_30m','open_60m'])
        self.assertEqual({x[3] for x in events},{1900.})
        self.assertEqual(events[0][2],1300.)
        self.assertGreater(made[0][1],1000.+720.)
