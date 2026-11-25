"""Production candidate routing seams only, no network or publication."""
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
import collect_market_data as entry
import market_data_collection as runtime
from fixed_collection_publication import execute
ROOT=Path(__file__).resolve().parents[1]

class WiringTests(unittest.TestCase):
    def args(self,mode):return ['--mode',mode,'--trade-date','2026-10-07','--universe',str(ROOT/'config/collection-universe.json'),'--config',str(ROOT/'config/market-data-store.yaml'),'--access-config',str(ROOT/'config/market-data-collector-access.yaml')]
    def test_guarded_route_never_calls_legacy(self):
        factory=Mock();finalize=Mock();legacy=Mock()
        with patch.object(entry,'cli',return_value=0) as cli:
            self.assertEqual(execute(self.args('open_15m'),owner_factory=factory,stage_publish=finalize,legacy_publish=legacy),0)
        cli.assert_called_once();self.assertIs(cli.call_args.kwargs['owner_factory'],factory);legacy.assert_not_called()
    def test_guarded_unknown_never_falls_back(self):
        legacy=Mock()
        with patch.object(entry,'cli',side_effect=RuntimeError('unknown')),self.assertRaisesRegex(RuntimeError,'unknown'):
            execute(self.args('open_15m'),owner_factory=Mock(),stage_publish=Mock(),legacy_publish=legacy)
        legacy.assert_not_called()
    def test_missing_guarded_publisher_stops_before_collection(self):
        with patch.object(entry,'cli') as cli,self.assertRaises(ValueError):execute(self.args('open_15m'),owner_factory=Mock(),legacy_publish=Mock())
        cli.assert_not_called()
    def test_legacy_path_unchanged_when_not_injected(self):
        legacy=Mock()
        with patch.object(entry,'cli',return_value=0) as cli:execute(self.args('open_15m'),legacy_publish=legacy)
        cli.assert_called_once_with(self.args('open_15m'));legacy.assert_called_once()
    def test_predecessor_receives_factory_and_is_published_before_next_stage(self):
        events=[];factory=Mock()
        def run(args,**kw):
            self.assertIs(kw['owner_factory'],factory);events.append(('collect',entry._arg_value(args,'--mode')));return 0
        def finish(args):events.append(('publish',entry._arg_value(args,'--mode')))
        with patch.object(entry,'_snapshot_materialized',return_value=False),patch.object(runtime,'main',side_effect=run),patch.object(entry,'_build_metric_proof_if_applicable'):
            entry._ensure_predecessors(self.args('open_60m'),owner_factory=factory,stage_publish=finish)
        self.assertEqual(events,[('collect','open_15m'),('publish','open_15m'),('collect','open_30m'),('publish','open_30m')])
    def test_close_retry_keeps_missing_only_selection_and_factory(self):
        factory=Mock()
        with patch.object(entry,'_live_close_retry_symbols',return_value=['XLE']),patch.object(runtime,'collect_regular_window',return_value={}) as collect:
            entry._run_live_close(self.args('close_retry'),owner_factory=factory)
        self.assertEqual(collect.call_args.kwargs['symbols_override'],['XLE'])
        self.assertEqual(collect.call_args.kwargs['stage'],'close');self.assertIs(collect.call_args.kwargs['owner_factory'],factory)
    def test_close_nothing_missing_never_builds_owner(self):
        factory=Mock()
        with patch.object(entry,'_live_close_retry_symbols',return_value=[]),patch.object(runtime,'collect_regular_window') as collect:
            self.assertEqual(entry._run_live_close(self.args('close_final'),owner_factory=factory),0)
        collect.assert_not_called();factory.assert_not_called()
    def test_fixed_runtime_receives_owner_factory(self):
        factory=Mock()
        with patch.object(runtime,'collect_regular_window',return_value={}) as collect:runtime.main(self.args('open_30m'),owner_factory=factory)
        self.assertIs(collect.call_args.kwargs['owner_factory'],factory)
        self.assertEqual(collect.call_args.kwargs['start_et'],'09:45')
    def test_factory_sees_actual_selected_scope_before_provider(self):
        factory=Mock(side_effect=RuntimeError('stop before provider'))
        with self.assertRaisesRegex(RuntimeError,'stop before provider'):
            runtime.collect_regular_window(mode='close_retry',stage='close',trade_date='2026-10-07',start_et='15:45',end_et='16:00',store_root=Path('/tmp/not-used'),universe_config={},config={},access={},eligible_universe=[('AAA','stocks'),('BBB','stocks')],symbols_override=['BBB'],owner_factory=factory)
        self.assertEqual(factory.call_args.kwargs['symbols'],['BBB'])
        self.assertEqual(factory.call_args.kwargs['mode'],'close_retry')
    def test_eod_with_candidate_dependencies_stays_explicit_legacy(self):
        factory=Mock();publish=Mock();legacy=Mock()
        with patch.object(runtime.base,'collect_previous_session_eod',return_value={}) as provider:
            result=execute(self.args('previous_session_eod'),owner_factory=factory,stage_publish=publish,legacy_publish=legacy)
        self.assertEqual(result,0);provider.assert_called_once();factory.assert_not_called();publish.assert_not_called();legacy.assert_called_once()
    def test_direct_guarded_eod_rejected_before_provider(self):
        with patch.object(runtime.base,'collect_previous_session_eod') as provider,self.assertRaisesRegex(ValueError,'unsupported guarded mode'):
            entry.cli(self.args('previous_session_eod'),owner_factory=Mock(),stage_publish=Mock())
        provider.assert_not_called()
    def test_direct_runtime_guarded_history_repair_rejected_before_provider(self):
        with patch.object(runtime,'collect_regular_window') as provider,self.assertRaisesRegex(ValueError,'unsupported guarded mode'):
            runtime.main(self.args('historical_context_repair')+['--maintenance-authorized'],owner_factory=Mock())
        provider.assert_not_called()
    def test_history_repair_with_candidate_dependencies_keeps_legacy(self):
        factory=Mock();publish=Mock();legacy=Mock()
        with patch.object(runtime,'collect_regular_window',return_value={}) as provider:
            result=execute(self.args('historical_context_repair')+['--maintenance-authorized'],owner_factory=factory,stage_publish=publish,legacy_publish=legacy)
        self.assertEqual(result,0);provider.assert_called_once();self.assertNotIn('owner_factory',provider.call_args.kwargs)
        factory.assert_not_called();publish.assert_not_called();legacy.assert_called_once()
    def test_equals_mode_uses_guarded_actual_runtime_path(self):
        args=['--mode=open_15m']+self.args('open_15m')[2:]
        factory=Mock();publish=Mock();legacy=Mock()
        with patch.object(runtime,'collect_regular_window',return_value={}) as collect:
            self.assertEqual(execute(args,owner_factory=factory,stage_publish=publish,legacy_publish=legacy),0)
        self.assertIs(collect.call_args.kwargs['owner_factory'],factory)
        publish.assert_called_once();legacy.assert_not_called()
    def test_duplicate_modes_rejected_before_any_collector(self):
        for first,second in [('previous_session_eod','open_15m'),('open_15m','previous_session_eod'),('open_15m','open_15m')]:
            args=['--mode',first,'--mode='+second]+self.args('open_15m')[2:]
            with self.subTest(first=first,second=second),patch.object(runtime,'collect_regular_window') as regular,patch.object(runtime.base,'collect_previous_session_eod') as eod,self.assertRaisesRegex(ValueError,'repeated --mode'):
                execute(args,owner_factory=Mock(),stage_publish=Mock(),legacy_publish=Mock())
            regular.assert_not_called();eod.assert_not_called()
    def test_mode_abbreviation_rejected_before_collector(self):
        args=['--mo=open_15m']+self.args('open_15m')[2:]
        with patch.object(runtime,'collect_regular_window') as collect,self.assertRaisesRegex(ValueError,'abbreviated --mode'):
            execute(args,owner_factory=Mock(),stage_publish=Mock(),legacy_publish=Mock())
        collect.assert_not_called()
    def test_equals_mode_without_injection_preserves_legacy(self):
        args=['--mode=open_15m']+self.args('open_15m')[2:];legacy=Mock()
        with patch.object(runtime,'collect_regular_window',return_value={}) as collect:execute(args,legacy_publish=legacy)
        self.assertNotIn('owner_factory',collect.call_args.kwargs);legacy.assert_called_once()
    def test_direct_runtime_duplicate_mode_rejected_before_collection(self):
        args=['--mode','open_15m','--mode','previous_session_eod']+self.args('open_15m')[2:]
        with patch.object(runtime.base,'collect_previous_session_eod') as collect,self.assertRaisesRegex(ValueError,'repeated --mode'):
            runtime.main(args)
        collect.assert_not_called()
