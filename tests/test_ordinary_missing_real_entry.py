from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from io import StringIO
import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import collect_market_data as entrypoint
import yaml
import market_data_collectors as c
import market_data_store as s
from build_store_publication_manifest import build_manifest
from verify_store_publication import _verify_required_manifest
from push_proof_store_commit import StorePushError, push_exact, verify_exact_output_closure

def bar(day,**kw):
    return dict(dict(trade_date=day,open=10.,high=12.,low=9.,close=11.,volume=100.),**kw)

def run_scenario(rows, incoming, make_partial=False, permitted=(), policy=True, record_limit=512, verify_publication=False, fail_index_write=False, fail_rollback=False, witnesses=None, trade_date="2026-11-25", actual_cli=False, remote_drift=False, response_symbol=None, fail_append=False, response_meta=None, identity_profile=None, fixture_symbol='AAA', fixture_asset_class=None):
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        repo=Path(temp).resolve()
        root=repo / "data/market-data" if verify_publication else repo
        asset_class = fixture_asset_class or ('etf' if identity_profile and identity_profile['type'] == 'ETF' else 'stocks')
        c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol=fixture_symbol,asset_class=asset_class,bars=rows,series_semantics='daily_regular_ohlcv',shard_record_limit=record_limit)
        for symbol, days in (witnesses or {}).items():
            c.append_daily_bars(root,provider=c.TWELVE_PROVIDER,symbol=symbol,asset_class='etf',
                                bars=[bar(day) for day in days],series_semantics='daily_regular_ohlcv')
        if make_partial:
            idx=c._series_index_path(root,c.TWELVE_PROVIDER,fixture_symbol)
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
            if actual_cli:
                bare = Path(stack.enter_context(tempfile.TemporaryDirectory()))/'origin.git'
                git('init','-q','--bare',str(bare))
                git('remote','add','origin',str(bare))
                git('push','origin','HEAD:refs/heads/main')
        replacements={'_daily_universe':[(fixture_symbol,asset_class)],'_load_daily_basis_owner':{},'_identity_effective_date':None,'wait_for_twelve_budget':None,'consume_twelve_credit':None,'fetch_twelve_daily':incoming}
        if response_symbol is not None:
            replacements.pop('fetch_twelve_daily')
            replacements['_http_json'] = {'meta': {'symbol': response_symbol, 'interval': '1day',
                'currency': 'USD', 'exchange_timezone': 'America/New_York', **(response_meta or {})},
                'values': [dict({k: v for k, v in row.items() if k != 'trade_date'},
                                datetime=row['trade_date']) for row in incoming]}
        stack.enter_context(patch.dict(os.environ,env))
        mocks = {name: stack.enter_context(patch.object(c,name,return_value=val)) for name,val in replacements.items()}
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
        if fail_append:
            stack.enter_context(patch.object(c, 'append_daily_bars',
                side_effect=c.MarketDataConflict('synthetic post-guard append rejection')))
        if actual_cli:
            source=Path(__file__).resolve().parents[1]/'config'
            # Exercise the same checked-in config the scheduled collector reads;
            # an enabled fixture cannot establish that the rollout is enabled.
            if not policy:
                raise AssertionError('actual CLI scenario uses production configuration')
            config_path=source/'market-data-store.yaml'
            if identity_profile is not None:
                candidate_config = yaml.safe_load(config_path.read_text())
                candidate_config['collector']['twelve_data_daily']['historical_revision_identity_profiles'] = {fixture_symbol: identity_profile}
                config_path = Path(stack.enter_context(tempfile.TemporaryDirectory()))/'market-data-store.yaml'
                config_path.write_text(yaml.safe_dump(candidate_config))
            now=datetime.fromisoformat(trade_date+'T07:05:00-05:00')
            gate=entrypoint._settlement_gate_result
            output=StringIO()
            with patch.object(entrypoint,'_settlement_gate_result',side_effect=lambda args:gate(args,now_et=now)), redirect_stdout(output):
                rc=entrypoint.cli(['--mode','previous_session_eod','--trade-date',trade_date,
                    '--store-root',str(root),'--config',str(config_path),
                    '--universe',str(source/'collection-universe.json'),
                    '--access-config',str(source/'market-data-collector-access.yaml')])
            if rc:
                raise AssertionError('the configured collection CLI returned an error')
            out=json.loads(output.getvalue().strip().splitlines()[-1])
        else:
            out=c.collect_previous_session_eod(trade_date=trade_date,store_root=root,watchlist={},completeness={},config=cfg,access=access,verified_gap_sessions=None if permitted is None else frozenset(permitted))
        if verify_publication:
            result_file=repo/'collector-result.txt'
            result_file.write_text(json.dumps(out))
            manifest=build_manifest(store_root=root,result_file=result_file,request_id='synthetic',
                                    trade_date=trade_date,stage='previous_session_eod')
            out['publication_files']={r['path'] for r in json.loads(manifest.read_text())['required_outputs']}
            git('add','data/market-data')
            git('commit','-qm','repair')
            candidate=git('rev-parse','HEAD')
            base=env['MARKET_DATA_STORE_INPUT_SHA']
            manifest_path=manifest.relative_to(repo).as_posix()
            _verify_required_manifest(repo,candidate,manifest_path)
            verify_exact_output_closure(repo,base,candidate,manifest_path)
            result_file.unlink()
            if actual_cli and remote_drift:
                tree=git('rev-parse',base+'^{tree}')
                concurrent=git('commit-tree',tree,'-p',base,'-m','concurrent advance')
                git('push','origin',concurrent+':refs/heads/main')
                try:
                    push_exact(repo,base,candidate)
                except StorePushError as exc:
                    if str(exc)!='STORE_REMOTE_MAIN_ADVANCED':
                        raise
                else:
                    raise AssertionError('repair was published despite a concurrent advance')
                if git('ls-remote','origin','refs/heads/main').split()[0]!=concurrent:
                    raise AssertionError('the concurrent remote HEAD changed')
            elif actual_cli:
                out['verified_local_remote_sha']=push_exact(repo,base,candidate)['remote_main_sha']
                _verify_required_manifest(repo,out['verified_local_remote_sha'],manifest_path)
            # An unmanifested output is disallowed, even if the original manifest is still intact.
            (root/'unexpected.json').write_text('{}')
            git('add','data/market-data')
            git('commit','-qm','unexpected unmanifested change')
            try:
                verify_exact_output_closure(repo,base,git('rev-parse','HEAD'),manifest_path)
            except StorePushError as exc:
                if str(exc)!='STORE_MANIFEST_OUTPUT_SCOPE_MISMATCH':
                    raise
            else:
                raise AssertionError('unmanifested output passed the exact closure')
        if response_symbol is not None:
            from urllib.parse import urlsplit, parse_qs
            fetch = mocks['_http_json']
            assert fetch.call_count == 1
            q = parse_qs(urlsplit(fetch.call_args.args[0]).query)
            out['request_identity'] = {k: q[k][0] for k in ('exchange','mic_code','adjust') if k in q}
        out['normalized_evidence'] = [json.loads(p.read_bytes()) for p in root.glob('collector-state/*/price-only-*.json')]
        out['lineage_segments'] = json.loads(c._series_index_path(root,c.TWELVE_PROVIDER,fixture_symbol).read_bytes())['lineage_segments']
        out['repair_audits']=[json.loads(p.read_bytes()) for p in root.glob('collector-state/*/ordinary-gap-*.json')]
        return c.read_daily_series(root,provider=c.TWELVE_PROVIDER,symbol=fixture_symbol),out

class OrdinaryMissingProductionEntry(unittest.TestCase):
    def test_enabled_production_policy_keeps_corporate_action_series_out_of_legacy_writes(self):
        source = Path(__file__).resolve().parents[1] / 'config' / 'market-data-store.yaml'
        config = yaml.safe_load(source.read_text())
        self.assertEqual(config['collector']['twelve_data_daily']['ordinary_missing_repair_policy'],
                         'active_shard_gap_fill_v1')
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            owner = root / 'series-active/daily/twelve_data_basic/SOXX.json'
            owner.parent.mkdir(parents=True)
            owner.write_text('{}')
            stack.enter_context(patch.dict(os.environ, {'TWELVE_DATA_API_KEY': 'SYNTHETIC'}))
            stack.enter_context(patch.object(c, '_daily_universe', return_value=[('SOXX','etf')]))
            stack.enter_context(patch.object(c, '_load_daily_basis_owner', return_value={}))
            wait, debit, fetch = [stack.enter_context(patch.object(c, name)) for name in
                                  ('wait_for_twelve_budget','consume_twelve_credit','fetch_twelve_daily')]
            result = c.collect_previous_session_eod(trade_date='2026-11-25', store_root=root,
                watchlist={}, completeness={}, config=config, access={}, verified_gap_sessions=frozenset())
            self.assertEqual(result['changed_series'], 0)
            self.assertEqual([row['symbol'] for row in result['failures']], ['SOXX'])
            self.assertEqual(owner.read_text(), '{}')
            for action in (wait, debit, fetch):
                action.assert_not_called()

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

    def test_configured_entrypoint_exact_local_remote_and_concurrent_base_rejection(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        common=dict(permitted=None,witnesses={'SPY':[b['trade_date']],'QQQ':[b['trade_date']]},
                    actual_cli=True,verify_publication=True)
        out,result=run_scenario([a,c],[a,b,c],**common)
        self.assertEqual(len(out),3)
        self.assertRegex(result['verified_local_remote_sha'],r'^[0-9a-f]{40}$')
        _,rejected=run_scenario([a,c],[a,b,c],remote_drift=True,**common)
        self.assertNotIn('verified_local_remote_sha',rejected)

    def test_common_feed_gap_is_not_inferred_from_weekday_or_new_batch(self):
        a,b,c=[bar(day) for day in ('2026-11-20','2026-11-23','2026-11-24')]
        out,result=run_scenario([a,c],[a,b,c],permitted=None,
                               witnesses={'SPY':[a['trade_date'],c['trade_date']],
                                          'QQQ':[a['trade_date'],c['trade_date']]})
        self.assertEqual(out,[a,c])
        self.assertEqual(result['repair_audits'],[])


STOCK_META = {'exchange':'NASDAQ', 'mic_code':'XNGS', 'type':'Common Stock'}
ETF_META = {'exchange':'CBOE', 'mic_code':'BATS', 'type':'ETF'}
def approved(meta):
    return {**meta, 'adjust':'splits', 'basis_mode':'witnessed_same_axis_only',
            'venue_sources':['https://example.invalid/source']}


def test_isolated_old_price_revision_does_not_block_reviewed_calendar_target_on_thanksgiving():
    days = ['2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24','2026-11-25']
    old = [bar(day) for day in days[:-1]]
    incoming = [bar(days[0], high=12.01, volume=105.), *old[1:], bar(days[-1], close=11.05, volume=123.)]
    actual, result = run_scenario(old, incoming, permitted=None,
        witnesses={'SPY': days[:-1], 'QQQ': days[:-1]}, verify_publication=True, actual_cli=True,
        response_symbol='AAA', trade_date='2026-11-26', response_meta=STOCK_META, identity_profile=approved(STOCK_META))
    assert actual[:-1] == old
    assert actual[-1]['trade_date'] == days[-1]
    assert actual[-1]['close'] == 11.05
    assert result['failure_target_count'] == 0

    assert result['deferred_historical_revision_count'] == 1
    assert result['conflict_diagnostics'][0]['disposition'] == 'deferred_preserve_prior_prices'
    assert result['conflict_diagnostics'][0]['normalized_evidence_path'] in result['publication_files']
    assert result['lineage_segments'][-1]['volume_policy'] == 'BLOCKED_UNRESOLVED'
    assert result['normalized_evidence'][0]['verified_response_identity']['symbol'] == 'AAA'
    assert result['normalized_evidence'][0]['verified_response_identity']['mic_code'] == 'XNGS'
    assert result['request_identity'] == {}
    assert result['normalized_evidence'][0]['verified_response_identity']['documented_default_adjust'] == 'splits'
    assert result['normalized_evidence'][0]['records'][0] == incoming[0]
    assert result['normalized_evidence'][0]['records'][-1] == incoming[-1]


def test_old_revision_never_admits_bad_metadata_gaps_or_cross_basis_targets():
    days = ['2026-11-17','2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24']
    old = [bar(day) for day in days[:-1]]
    small = bar(days[0], high=12.01)
    target = bar(days[-1])
    ordinary = [small, *old[1:], target]
    witnesses = {'SPY': days, 'QQQ': days}
    cases = [
        (ordinary, witnesses, 'OTHER'),
        ([small, *old[1:], bar(days[-1],open=5.,high=6.,low=4.5,close=5.5)], witnesses, 'AAA'),
        ([bar(days[0], close=11.01), *old[1:], target], witnesses, 'AAA'),
        ([small, bar(days[1], high=12.01), *old[2:], target], witnesses, 'AAA'),
        ([small, *old[1:-1], bar(days[-2],high=12.01), target], witnesses, 'AAA'),
        ([small, *old[1:], bar(days[-1], low=20.)], witnesses, 'AAA'),
        ([small, *old[1:-1], target], witnesses, 'AAA'),
        ([*ordinary, target], witnesses, 'AAA'),
    ]
    for incoming, session_witnesses, response_symbol in cases:
        actual, result = run_scenario(old, incoming, permitted=None, witnesses=session_witnesses,
                                       actual_cli=True, verify_publication=True, response_symbol=response_symbol, response_meta=STOCK_META, identity_profile=approved(STOCK_META))
        assert actual == old, (incoming, session_witnesses, response_symbol, result)
        assert result['failure_target_count'] == 1
        assert result['deferred_historical_revision_count'] == 0

    # 11/26 is a published closure: with an execution on 11/27, an allegedly
    # completed 11/26 row cannot be promoted along with the 11/25 target.
    thanksgiving_days = ['2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24']
    existing = [bar(day) for day in thanksgiving_days]
    incoming = [bar(thanksgiving_days[0], high=12.01), *existing[1:],
                bar('2026-11-25'), bar('2026-11-26')]
    actual, result = run_scenario(existing, incoming, permitted=None, actual_cli=True,
        verify_publication=True, response_symbol='AAA', trade_date='2026-11-27', response_meta=STOCK_META, identity_profile=approved(STOCK_META))
    assert actual == existing
    assert result['failure_target_count'] == 1

def test_append_rejected_after_deferral_guard_keeps_partial_and_unpromoted():
    days = ['2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24','2026-11-25']
    old = [bar(day) for day in days[:-1]]
    incoming = [bar(days[0],high=12.01), *old[1:], bar(days[-1])]
    actual, result = run_scenario(old, incoming, permitted=None, verify_publication=True,
        actual_cli=True, response_symbol='AAA', trade_date='2026-11-26', fail_append=True, response_meta=STOCK_META, identity_profile=approved(STOCK_META))
    assert actual == old
    assert result['status'] == 'partial'
    assert result['deferred_historical_revision_count'] == 0
    assert result['failures'][0]['reason_code'] == 'immutable_append_conflict'
    assert result['conflict_record_count'] == 1
    assert 'disposition' not in result['conflict_diagnostics'][0]
    assert result['normalized_evidence'][0]['records'][-1] == incoming[-1]


def test_explicit_identity_refuses_wrong_mic_exchange_basis_or_missing_venue_and_accepts_known_etf():
    days = ['2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24','2026-11-25']
    old = [bar(day) for day in days[:-1]]
    incoming = [bar(days[0],high=12.01), *old[1:], bar(days[-1])]
    def run(meta, profile=approved(ETF_META)):
        return run_scenario(old, incoming, permitted=None, verify_publication=True,
            actual_cli=True, response_symbol='AAA', trade_date='2026-11-26',
            response_meta=meta, identity_profile=profile)
    for meta in ({**ETF_META,'mic_code':'ARCX'}, {**ETF_META,'exchange':'NASDAQ'},
                 {**ETF_META,'adjust':'all'}, {**ETF_META,'adjustment':'none'},
                 {**ETF_META,'adjusted': True}, {'exchange':'CBOE','type':'ETF'}):
        actual, result = run(meta)
        assert actual == old
        assert result['deferred_historical_revision_count'] == 0
        assert result['failures'][0]['reason_code'] == 'daily_response_identity_or_basis_conflict'
    actual, result = run(ETF_META, profile=None)
    assert actual == old and result['request_identity'] == {}
    assert result['deferred_historical_revision_count'] == 0
    assert 'verified_response_identity' not in result['normalized_evidence'][0]
    actual, result = run({**ETF_META, 'adjust':'splits', 'type':'Etf'})
    assert actual[:-1] == old and actual[-1] == incoming[-1]
    assert result['request_identity'] == {}
    assert result['normalized_evidence'][0]['verified_response_identity']['returned_basis_fields'] == {'adjust':'splits'}

def test_ordinary_defer_scope_only_known_five_symbols_and_never_alias_profiles():
    source = Path(__file__).resolve().parents[1]/'config/market-data-store.yaml'
    profiles = yaml.safe_load(source.read_text())['collector']['twelve_data_daily']['historical_revision_identity_profiles']
    assert {symbol:(row['exchange'],row['mic_code']) for symbol, row in profiles.items()} == {
        'IBIT':('NASDAQ','XNMS'), 'VIXY':('CBOE','BATS'), 'META':('NASDAQ','XNGS'),
        'PLTR':('NASDAQ','XNGS'), 'TSLA':('NASDAQ','XNGS')}
    assert all(c._approved_daily_identity(symbol, profiles) is not None for symbol in profiles)
    assert c._approved_daily_identity('SOXX', profiles) is None

def test_checked_in_meta_profile_does_not_change_existing_no_conflict_append():
    day, target = '2026-11-24','2026-11-25'
    original = [bar(day)]
    incoming = original + [bar(target)]
    # Missing MIC would not qualify for deferral. The unchanged normal
    # no-conflict request and append must still work with the real config.
    actual, result = run_scenario(original, incoming, permitted=None, verify_publication=True,
        actual_cli=True, response_symbol='META', trade_date='2026-11-26', fixture_symbol='META')
    assert actual == incoming
    assert result['request_identity'] == {}
    assert result['deferred_historical_revision_count'] == 0
    assert 'verified_response_identity' not in result['normalized_evidence'][0]
    assert result['failure_target_count'] == 0

def test_checked_in_five_profiles_defer_via_original_single_call_each():
    source = Path(__file__).resolve().parents[1]/'config/market-data-store.yaml'
    profiles = yaml.safe_load(source.read_text())['collector']['twelve_data_daily']['historical_revision_identity_profiles']
    days = ['2026-11-18','2026-11-19','2026-11-20','2026-11-23','2026-11-24','2026-11-25']
    old = [bar(day) for day in days[:-1]]
    incoming = [bar(days[0],high=12.01), *old[1:], bar(days[-1])]
    for symbol, profile in profiles.items():
        meta = {key:profile[key] for key in ('exchange','mic_code','type')}
        actual, outcome = run_scenario(old, incoming, permitted=None, verify_publication=True,
            actual_cli=True, response_symbol=symbol, response_meta=meta, trade_date='2026-11-26',
            fixture_symbol=symbol, fixture_asset_class='etf' if meta['type']=='ETF' else 'stocks')
        assert actual == old + [incoming[-1]], symbol
        assert outcome['request_identity'] == {}  # Helper checks transport was called exactly once.
        assert outcome['deferred_historical_revision_count'] == 1
        assert outcome['normalized_evidence'][0]['verified_response_identity']['mic_code'] == meta['mic_code']
