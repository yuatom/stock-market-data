"""Offline evidence boundaries for the fixed early-close scheduled route."""
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from close_publication_reuse import ReuseEvidenceError, decide, main

ROOT=Path(__file__).resolve().parents[1]
EARLY='2026-11-27'; NORMAL='2026-11-30'

class TestScope(unittest.TestCase):
    def test_non_early_does_not_even_read_store(self):
        with patch('close_publication_reuse._git', side_effect=AssertionError()):
            self.assertFalse(decide(Path('/unused'),NORMAL,'a'*40,'b'*40,ROOT/'config/collection-universe.json'))

    def test_absent_exact_snapshot_keeps_original_collection(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t).resolve()
            def run(*args):
                return subprocess.check_output(['git','-C',str(p),'-c','user.name=Test','-c','user.email=test@example.invalid',*args],text=True).strip()
            run('init','-q');run('commit','-q','--allow-empty','-m','empty Store')
            pin=run('rev-parse','HEAD')
            self.assertFalse(decide(p,EARLY,'a'*40,pin,ROOT/'config/collection-universe.json'))
            with self.assertRaises(ReuseEvidenceError):
                decide(p,EARLY,'a'*40,'b'*40,ROOT/'config/collection-universe.json')

    def test_every_writer_is_gated_after_read_only_probe(self):
        w=yaml.safe_load((ROOT/'.github/workflows/market-data-collector-runtime.yml').read_text())
        steps=w['jobs']['collect']['steps']
        gate=next(s for s in steps if s.get('id')=='close_reuse')
        self.assertIn("inputs.trigger_kind == 'schedule'",gate['if'])
        self.assertIn("inputs.schedule == '6 16 * * 1-5'",gate['if'])
        self.assertIn("steps.invocation.outputs.mode == 'close'",gate['if'])
        for name in ('Collect qualified facts into private Store checkout','Rebuild private Store inventory',
                     'Build Store publication manifest','Persist private Store only','Verify durable Store publication'):
            step=next(s for s in steps if s.get('name')==name)
            self.assertGreater(steps.index(step),steps.index(gate))
            self.assertIn("steps.close_reuse.outputs.reuse != 'true'",step['if'])

class TestRealGitEarlyClose(unittest.TestCase):
    def test_complete_retained_publication_only(self):
        import hashlib
        import json
        from datetime import datetime
        import close_publication_reuse as reuse
        import build_deterministic_metric_proof as proof
        import build_store_publication_manifest as manifest
        import market_data_collection as runtime
        import collection_universe as owner

        compute='a'*40
        day=EARLY
        window={'start':'12:45','end':'13:00'}
        generated=f'{day}T13:08:00-05:00'
        universe_path=ROOT/'config/collection-universe.json'
        universe=owner.load_collection_universe(universe_path)
        stamp=f'{day}T12:59:00-05:00'
        now=datetime.fromisoformat(f'{day}T16:06:00-05:00')
        with tempfile.TemporaryDirectory() as t:
            base=Path(t).resolve(); repo=base/'repo'; repo.mkdir(); remote=base/'origin.git'
            subprocess.run(['git','init','-q','--bare',str(remote)],check=True)
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.invalid',*args],text=True).strip()
            git('init','-q','-b','main')
            git('remote','add','origin',str(remote))
            data=repo/'data/market-data'; data.mkdir(parents=True)
            refs=[]
            for symbol,asset in reuse._live_close_universe(universe):
                facts=[]
                for minute in range(45,60):
                    ts=f'{day}T12:{minute}:00-05:00'
                    facts.append(owner.decorate_fact(universe,
                        dict(symbol=symbol,asset_class=asset,session='regular',event_time=ts,source_timestamp=ts,
                             last_sale=10.0,reported_volume=1.0,source_contract='nasdaq_public_chart_last_sale_volume_v1')))
                rel,blob=runtime.write_capture(data,trade_date=day,session='regular',provider=runtime.NASDAQ,
                    capture_id='synthetic-'+symbol.lower(),generated_at=generated,actual_data_cutoff=stamp,window=window,
                    feed_scope='nasdaq_public_chart_last_sale_volume_v1',qualified_facts=facts,missing_symbols=[])
                k='cross_asset_proxy_capture' if symbol in owner.context_by_symbol(universe) else 'sector_context_capture' if symbol in owner.sector_symbols(universe) else 'regular_intraday_capture'
                refs.append(dict(path=rel,blob_sha=blob,kind=k,window='12:45-13:00',
                                 provider=runtime.NASDAQ,symbol=symbol,reason_class='success'))
            snap,_=runtime.write_snapshot(data,stage='close',trade_date=day,snapshot_id='synthetic-close',
                generated_at=generated,data_refs=refs,coverage={},missing=[],target_window=window,actual_data_cutoff=stamp)
            expected,relative=proof.build_proof(data,trade_date=day,stage='close',data_plane_commit_sha=compute)
            blob=proof._write_json(data/relative,expected)
            proof._write_json((data/relative).parent.parent/'latest.json',proof._proof_pointer(expected,rel=relative,blob=blob))
            mutable=data/'collector-state/2026-11/mutable.json';mutable.parent.mkdir(parents=True,exist_ok=True)
            mutable.write_text('{"budget":1}\n')
            cmd=base/'result'; cmd.write_text(json.dumps({'snapshot_path':snap})+'\n')
            output=manifest.build_manifest(store_root=data,result_file=cmd,request_id='synthetic-close-base',trade_date=day,stage='close')
            self.assertTrue(output.exists())
            git('add','--all');git('commit','-qm','synthetic producer')
            initial=git('rev-parse','HEAD');git('tag','market-data-read/'+initial)
            mutable.write_text('{"budget":2}\n')
            git('add','--all');git('commit','-qm','ordinary separate bookkeeping')
            pin=git('rev-parse','HEAD'); git('tag','market-data-read/'+pin);git('push','-q','origin','main','--tags')
            files_before={p.relative_to(data):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
            tags_before=git('ls-remote','--tags','origin')
            self.assertTrue(decide(repo,day,compute,pin,universe_path,now=now))
            self.assertFalse(decide(repo,day,'b'*40,pin,universe_path,now=now))
            with self.assertRaises(ReuseEvidenceError):
                decide(repo,day,compute,pin,universe_path,now=datetime.fromisoformat(f'{day}T12:58:00-05:00'))
            with patch.object(reuse,'_full_window_from_one_provider',return_value=False):
                self.assertFalse(decide(repo,day,compute,pin,universe_path,now=now))
            lookup=reuse._one_remote_ref
            with patch.object(reuse,'_one_remote_ref',
                              side_effect=lambda repo,remote,ref: None if ref.startswith('refs/tags/') else lookup(repo,remote,ref)):
                self.assertFalse(decide(repo,day,compute,pin,universe_path,now=now))
            with patch.object(reuse,'_one_remote_ref',side_effect=TimeoutError()):
                with self.assertRaises(TimeoutError):
                    decide(repo,day,compute,pin,universe_path,now=now)
            self.assertEqual(files_before,{p.relative_to(data):hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()})
            self.assertEqual(tags_before,git('ls-remote','--tags','origin'))
            self.assertEqual(git('status','--porcelain'),'')
            git('rm',str(output.relative_to(repo))); git('commit','-qm','fixture confirms prior manifest absent')
            without=git('rev-parse','HEAD'); git('push','-q','origin','main')
            self.assertFalse(decide(repo,day,compute,without,universe_path,now=now))
