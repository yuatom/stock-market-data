from contextlib import ExitStack
import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import market_data_collectors as c
import market_data_store as s
from build_store_publication_manifest import build_manifest
from verify_store_publication import _verify_required_manifest

def bar(day,**kw):
    return dict(dict(trade_date=day,open=10.,high=12.,low=9.,close=11.,volume=100.),**kw)

def run_scenario(rows, incoming, make_partial=False, permitted=(), policy=True, record_limit=512, verify_publication=False, fail_index_write=False, fail_rollback=False, witnesses=None, trade_date="2026-11-25"):
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        repo=Path(temp).resolve()
        root=repo / "data/market-data" if verify_publication else repo
        c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol='AAA',asset_class='stocks',bars=rows,series_semantics='daily_regular_ohlcv',shard_record_limit=record_limit)
        for symbol, days in (witnesses or {}).items():
            c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol=symbol,asset_class='etf',
                                bars=[bar(day) for day in days],series_semantics='daily_regular_ohlcv')
        if make_partial:
            idx=c._series_index_path(root,c.TWELVE_PROVIDER,'AAA')
            index=json.loads(idx.read_bytes()); old=index['shards'][0]
            p=idx.parent/old['path']; doc=json.loads(p.read_bytes());doc['records'][0].pop('open')
            s._write_json(p,doc)
            index['shards'][0]=s._shard_meta(old['path'],old['start'],doc,old['sealed'])
            s._write_json(idx,index)
        env={'TWELVE_DATA_API_KEY':'SYNTHETIC'}
        if verify_publication:
            def git(*args):
                return subprocess.check_output(
                    ['git','-C',temp,'-c','user.name=Fixture','-c','user.email=test@example.invalid',
                     '-c','commit.gpgsign=false',*args],text=True).strip()
            git('init','-q')
            git('add','data/market-data')
            git('commit','-qm','seed')
            env.update(MARKET_DATA_COMPUTE_SHA='1'*40, MARKET_DATA_STORE_INPUT_SHA=git('rev-parse','HEAD'))
        replacements={'_daily_universe':[('AAA','stocks')],'_load_daily_basis_owner':{},'_identity_effective_date':None,'wait_for_twelve_budget':None,'consume_twelve_credit':None,'fetch_twelve_daily':incoming}
        stack.enter_context(patch.dict(os.environ,env))
        for name,val in replacements.items():stack.enter_context(patch.object(c,name,return_value=val))
        cfg={'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},'twelve_data_daily':{'history_outputsize':64,'bootstrap_minimum_records':1,'overlap_revision_policy':'price_only_volume_blocked_v1'}}}
        if policy: cfg['collector']['twelve_data_daily']['ordinary_missing_repair_policy']='active_shard_gap_fill_v1'
        access={'twelve_data_basic':{'base_url':'https://example.invalid','time_series_path':'/time_series','interval_daily':'1day'}}
        if fail_index_write:
            original_write=s._write_json
            def fail_only_repair_index(path,value):
                if path.name=='_index.json' and any(seg.get('ingest_kind')=='ordinary_missing_price_repair'
                                                   for seg in value.get('lineage_segments',[])):
                    raise OSError('synthetic repair index failure')
                if (fail_rollback and path.name=='000000.json'
                        and len(value.get('records',[]))==len(rows)):
                    raise OSError('synthetic preimage restoration failure')
                return original_write(path,value)
            stack.enter_context(patch.object(s,'_write_json',side_effect=fail_only_repair_index))
        out=c.collect_previous_session_eod(trade_date=trade_date,store_root=root,watchlist={},completeness={},config=cfg,access=access,verified_gap_sessions=None if permitted is None else frozenset(permitted))
        if verify_publication:
            result_file=repo/'collector-result.txt'
            result_file.write_text(json.dumps(out))
            manifest=build_manifest(store_root=root,result_file=result_file,request_id='synthetic',
                                    trade_date='2026-11-25',stage='previous_session_eod')
            out['publication_files']={r['path'] for r in json.loads(manifest.read_text())['required_outputs']}
            git('add','data/market-data')
            git('commit','-qm','repair')
            _verify_required_manifest(repo,git('rev-parse','HEAD'),manifest.relative_to(repo).as_posix())
        out['repair_audits']=[json.loads(p.read_bytes()) for p in root.glob('collector-state/*/ordinary-gap-*.json')]
        return c.read_daily_series(root,provider=c.TWELVE_PROVIDER,symbol='AAA'),out

class OrdinaryMissingProductionEntry(unittest.TestCase):
    def test_missing_internal_session(self):
        dates=['2026-11-20','2026-11-23','2026-11-24']
        old=[bar(dates[0]),bar(dates[2])]
        actual,out=run_scenario(old,[bar(d,volume=100. if d!=dates[1] else 125.) for d in dates], permitted={dates[1]})
        self.assertEqual([r['trade_date'] for r in actual],dates)
        self.assertEqual(actual[::2],old)
        self.assertNotIn('volume',actual[1])
        self.assertEqual(out['repair_audits'][0]['rows'][0]['trade_date'],dates[1])
    def test_missing_same_date_price_preserves_present_values(self):
        a=bar('2026-11-24')
        actual,_=run_scenario([a],[bar('2026-11-24',open=10.25,close=11.5)],make_partial=True)
        self.assertEqual(actual,[dict(a,open=10.25)])

    def test_unverified_date_does_not_enter_publication(self):
        old=[bar('2026-11-20'),bar('2026-11-24')]
        out, result=run_scenario(old,old+[bar('2026-11-23')])
        self.assertEqual(out,old)
        self.assertEqual(result['repair_audits'],[])
    def test_sealed_shard_not_rewritten(self):
        old=[bar('2026-11-18'),bar('2026-11-20'),bar('2026-11-24')]
        out, result=run_scenario(old,old+[bar('2026-11-19')],permitted={'2026-11-19'},record_limit=2)
        self.assertEqual(out,old)
        self.assertEqual(result['repair_audits'],[])
    def test_post_fill_invalid_ohlc_does_not_change_disk(self):
        a=bar('2026-11-24')
        out, result=run_scenario([a],[bar('2026-11-24',open=12.5,high=14.)],make_partial=True)
        self.assertNotIn('open',out[0])
        self.assertEqual(result['repair_audits'],[])
    def test_policy_opt_out_preserves_fail_closed(self):
        a=bar('2026-11-24')
        out, result=run_scenario([a],[bar('2026-11-24',open=10.25,close=11.5)],make_partial=True,policy=False)
        self.assertNotIn('open',out[0])
        self.assertEqual(result['repair_audits'],[])

    def test_gap_change_and_evidence_are_in_exact_publication_manifest(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        out, result=run_scenario([a,c],[a,b,c],permitted={b['trade_date']},verify_publication=True)
        self.assertEqual(len(out),3)
        paths=result['publication_files']
        self.assertIn('series/daily/twelve_data_basic/AAA/_index.json',paths)
        self.assertIn('series/daily/twelve_data_basic/AAA/000000.json',paths)
        self.assertTrue(any('/ordinary-gap-' in p for p in paths))

    def test_interrupted_index_write_discards_repair_and_leaves_index_readable(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        out, result=run_scenario([a,c],[a,b,c],permitted={b['trade_date']},fail_index_write=True)
        self.assertEqual(out,[a,c])
        self.assertEqual(result['repair_audits'],[])
        self.assertEqual(result['changed_series'],0)

    def test_failed_preimage_restoration_aborts_entire_collector(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        with self.assertRaises(s.GapRepairRollbackError):
            run_scenario([a,c],[a,b,c],permitted={b['trade_date']},fail_index_write=True,fail_rollback=True)

    def test_runtime_uses_schedule_and_preexisting_both_witnesses_without_date_input(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        out, _=run_scenario([a,c],[a,b,c],permitted=None,
                            witnesses={'SPY':[b['trade_date']],'QQQ':[b['trade_date']]})
        self.assertEqual([row['trade_date'] for row in out],
                         [row['trade_date'] for row in [a,b,c]])
        out, _=run_scenario([a,c],[a,b,c],permitted=None,witnesses={'SPY':[b['trade_date']]})
        self.assertEqual(out,[a,c])

    def test_market_holiday_not_filled_even_when_both_witnesses_have_false_row(self):
        a,b,c=[bar(day) for day in ('2026-11-25','2026-11-26','2026-11-27')]
        out, _=run_scenario([a,c],[a,b,c],permitted=None,trade_date='2026-11-30',
                            witnesses={'SPY':[b['trade_date']],'QQQ':[b['trade_date']]})
        self.assertEqual(out,[a,c])
