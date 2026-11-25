"""Normal EOD receipt locates a private price candidate even when old rows conflict."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import market_data_collectors as c
from scripts.build_store_publication_manifest import build_manifest

class NormalEodLocator(unittest.TestCase):
    def test_normal_run_locates_same_packet_no_extra_call_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as work:
            p = Path(work)
            data = p/'data/market-data'; data.mkdir(parents=True)
            subprocess.run(['git','init','-q',p],check=True)
            old = dict(trade_date='2026-11-19', open=10., high=12., low=9., close=11., volume=100.)
            new = [dict(old,open=10.25),dict(old,trade_date='2026-11-23')]
            c.append_daily_bars(data,provider=c.TWELVE_PROVIDER,symbol='ABC',asset_class='stocks',
                               bars=[old],series_semantics='daily_regular_ohlcv',adjustment_semantics='provider_reported')
            subprocess.run(['git','-C',p,'add','data'],check=True)
            subprocess.run(['git','-C',p,'-c','user.name=fixture','-c','user.email=none@example.invalid',
                            'commit','-qm','fixture'],check=True)
            config = {'collector':{'budgets':{'twelve_data_basic':{'hard_credits_per_day':200,'hard_credits_per_minute':8}},
                 'twelve_data_daily':{'history_outputsize':64,'bootstrap_minimum_records':1,'overlap_revision_policy':'price_only_volume_blocked_v1'}}}
            env = {'TWELVE_DATA_API_KEY':'fixture','MARKET_DATA_STORE_INPUT_SHA':'1'*40,'MARKET_DATA_COMPUTE_SHA':'2'*40,
                   'GITHUB_RUN_ID':'999','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'schedule','GITHUB_REPOSITORY':'yuatom/stock-market-data'}
            with (patch.dict(os.environ,env,clear=False),patch.object(c,'wait_for_twelve_budget'),
                  patch.object(c,'consume_twelve_credit') as debit,patch.object(c,'fetch_twelve_daily',return_value=new) as fetch,
                  patch.object(c,'_daily_universe',return_value=[('ABC','stocks')]),
                  patch.object(c,'_load_daily_basis_owner',return_value={}),patch.object(c,'_conflict_record',return_value={})):
                result=c.collect_previous_session_eod(trade_date='2026-11-24',store_root=data,watchlist={},
                                                       completeness={},config=config,access={})
            self.assertEqual((fetch.call_count,debit.call_count),(1,1))
            self.assertEqual(result['failures'][0]['reason_code'],'historical_provider_revision')
            self.assertEqual(c.read_daily_series(data,provider=c.TWELVE_PROVIDER,symbol='ABC'),[old])
            diag=json.loads((data/result['result_state_path']).read_bytes())
            candidates=diag['retained_price_packet_candidates']
            self.assertEqual(set(candidates),{'ABC'})
            packet=candidates['ABC']
            raw=(data/packet).read_bytes()
            self.assertTrue(packet.endswith('price-only-'+hashlib.sha256(raw).hexdigest()+'.json'))
            self.assertEqual(json.loads(raw)['records'],new)
            self.assertNotIn('retained_price_packet_candidates',result)
            out=p/'collector-result';out.write_text(json.dumps(result)+'\n')
            kwargs=dict(store_root=data,result_file=out,request_id='',trade_date='2026-11-24',stage='previous_session_eod')
            m=build_manifest(**kwargs)
            manifest=json.loads(m.read_bytes())
            self.assertIn(packet,{x['path'] for x in manifest['required_outputs']})
            m.unlink()
            good=data/'healthy.json';good.write_text('valid separate production output')
            dp=data/result['result_state_path']
            bad_digest='collector-state/2026-11/price-only-'+'e'*64+'.json'
            (data/bad_digest).write_bytes(raw)
            parser_traps = []
            for wire in (
                b'[' * 10_000 + b'0' + b']' * 10_000,
                b'{"value":' + b'9' * 5_000 + b'}',
            ):
                trap='collector-state/2026-11/price-only-'+hashlib.sha256(wire).hexdigest()+'.json'
                (data/trap).write_bytes(wire)
                parser_traps.append(trap)
            for candidate in ({'WRONG':packet}, {'ABC':packet,'MISSING':packet.replace('price-only-', 'price-only-0',1)[:-1]},
                              {'ABC':'collector-state/2026-11/price-only-'+'f'*64+'.json'},
                              {'ABC':bad_digest},
                              *({'ABC':path} for path in parser_traps),
                              {f'S{i}':packet for i in range(201)}, [], {'ABC':'../../outside'}):
                with self.subTest(candidate_type=type(candidate),size=len(candidate)):
                    bad=dict(diag,retained_price_packet_candidates=candidate)
                    dp.write_text(json.dumps(bad))
                    m=build_manifest(**kwargs)
                    published=json.loads(m.read_bytes())
                    current=json.loads(dp.read_bytes())
                    self.assertIn('healthy.json',{x['path'] for x in published['required_outputs']})
                    self.assertEqual(current['conflicts'],diag['conflicts'])
                    self.assertNotIn('WRONG',current.get('retained_price_packet_candidates',{}))
                    self.assertEqual(current.get('retained_price_packet_candidates'),
                                     {'ABC':packet} if isinstance(candidate,dict) and 'MISSING' in candidate else None)
                    self.assertEqual(current.get('retained_price_packet_locator_status'),'partial_or_unavailable')
                    m.unlink()

            # A path from a previous commit is not a same-run postimage, even
            # if its bytes happen to be a well-formed provider packet.
            subprocess.run(['git','-C',p,'add',str((data/packet).relative_to(p))],check=True)
            subprocess.run(['git','-C',p,'-c','user.name=fixture','-c','user.email=none@example.invalid',
                            'commit','-qm','prior retained packet'],check=True)
            dp.write_text(json.dumps(diag))
            m=build_manifest(**kwargs)
            now=json.loads(dp.read_bytes())
            self.assertNotIn('retained_price_packet_candidates',now)
            self.assertIn('healthy.json',{x['path'] for x in json.loads(m.read_bytes())['required_outputs']})
