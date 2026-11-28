"""Exercise the actual workflow CLI wrapper, not the legacy collector parser."""
import contextlib
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import collect_market_data as cli


class LiveScheduledEodCLI(TestCase):
    def invoke(self, args):
        with TemporaryDirectory() as scratch, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                patch.object(cli,'_settlement_gate_result',return_value=None), \
                patch.object(cli,'_wait_until_regular_stage_cutoff'), \
                patch.object(cli,'_ensure_predecessors'), \
                patch.object(cli,'_build_metric_proof_if_applicable'), \
                patch.object(cli.runtime.base,'collect_previous_session_eod',return_value={'mode':'previous_session_eod'}) as collector:
            code=cli.cli(args+['--store-root',scratch,'--universe',str(ROOT/'config/collection-universe.json'),
                              '--config',str(ROOT/'config/market-data-store.yaml'),
                              '--access-config',str(ROOT/'config/market-data-collector-access.yaml')])
        self.assertEqual(code,0)
        return collector.call_args.kwargs

    def test_actual_workflow_arg_reaches_dated_eod_logic(self):
        args=['--mode','previous_session_eod','--trade-date','2026-11-30']
        observed=self.invoke(args+['--scheduled-eod-target-guard'])
        self.assertIs(observed['scheduled_target_guard'],True)
        self.assertEqual(observed['trade_date'],'2026-11-30')
        self.assertIs(self.invoke(args)['scheduled_target_guard'],False)

    def test_scheduled_guard_cannot_be_used_with_other_modes(self):
        with self.assertRaises(SystemExit) as exc:
            self.invoke(['--mode','premarket','--scheduled-eod-target-guard'])
        self.assertEqual(exc.exception.code,2)
