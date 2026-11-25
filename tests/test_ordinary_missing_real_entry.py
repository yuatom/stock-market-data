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

def run_scenario(rows, incoming, make_partial=False, permitted=(), policy=True, record_limit=512, verify_publication=False):
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        repo=Path(temp).resolve()
        root=repo / "data/market-data" if verify_publication else repo
        c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol='AAA',asset_class='stocks',bars=rows,series_semantics='daily_regular_ohlcv',shard_record_limit=record_limit)
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
        out=c.collect_previous_session_eod(trade_date='2026-11-25',store_root=root,watchlist={},completeness={},config=cfg,access=access,verified_gap_sessions=frozenset(permitted))
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
