"""Actual distinct checkouts and local bare Git; no provider or production traffic.

The injection only controls timing / loss of an acknowledgment after real push.
Git creates and arbitrates the candidate commits; no mocked push success.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import admit_market_data_baseline as P
import verify_store_publication as V
from test_dynamic_daily_owner_integration import git, init


class PublisherCompetitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        env=patch.dict(os.environ,{'GIT_ALLOW_PROTOCOL':'file','GIT_TERMINAL_PROMPT':'0'});env.start();self.addCleanup(env.stop)
        block=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'));block.start();self.addCleanup(block.stop)
        self.a,self.remote=init(self.root,'a',{'data/market-data/seed.json':b'{}\n'})
        self.b=self.root/'b'
        subprocess.run(['git','clone','-q','-b','main',str(self.remote),str(self.b)],check=True)
        git(self.b,'config','user.name','test');git(self.b,'config','user.email','test@example.invalid')
        self.start=git(self.a,'rev-parse','HEAD');self.verified=[]
        def verify(repo,sha):
            self.verified.append((repo,sha))
            return V.verify_publication(repo,sha,required_manifest='data/market-data/collector-state/2026-11/2026-11-14-twelve-data-budget.json')
        self.pa=P.StorePublisher(self.a,self.start,'2026-11-14','101-1',verify=verify)
        self.pb=P.StorePublisher(self.b,self.start,'2026-11-14','102-1',verify=verify)
    def prepare(self,p):
        target=p.safe_path(p.budget_relative);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(dict(schema_version=1,request_id=p.request_id,trade_date=p.day,
            stage='offline_synthetic_publication',required_outputs=[dict(path='seed.json',blob_sha=git(p.repo,'rev-parse','HEAD:data/market-data/seed.json'))]))+'\n')
    def head(self): return git(self.remote,'rev-parse','refs/heads/main')
    def retained(self,sha):return bool(git(self.remote,'for-each-ref','--format=%(objectname)',f'refs/tags/market-data-read/{sha}'))
    def test_two_distinct_checkouts_race_real_push_only_one_wins(self):
        self.assertNotEqual(self.pa.repo,self.pb.repo)
        for p in (self.pa,self.pb):self.prepare(p)
        barrier=threading.Barrier(2);original=P.git;pushes=[]
        def timed(repo,*args):
            if args[:2]==('push','origin'):
                pushes.append((repo,args));barrier.wait(timeout=15)
            return original(repo,*args)
        def publish(p):
            try:return ('ok',p.publish([p.budget_relative]))
            except Exception as exc:return ('error',exc)
        with patch.object(P,'git',side_effect=timed),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(publish,p) for p in (self.pa,self.pb)]
            results=[future.result(timeout=30) for future in futures]
        winners=[r for r in results if r[0]=='ok'];losers=[r for r in results if r[0]=='error']
        self.assertEqual(len(winners),1,results);self.assertEqual(len(losers),1,results)
        self.assertEqual(len(pushes),2);self.assertEqual(self.head(),winners[0][1])
        self.assertTrue(self.retained(winners[0][1]));self.assertEqual(len(self.verified),1)
        losing=self.pa if results[0][0]=='error' else self.pb
        self.assertEqual(losing.head,self.start)
        self.assertEqual(losers[0][1].publication['main_publication_outcome'],'UNCONFIRMED')
        self.assertFalse(self.retained(git(losing.repo,'rev-parse','HEAD')))
    def test_lost_ack_after_real_push_keeps_verified_head_unadvanced(self):
        self.prepare(self.pa);original=P.git;pushes=[]
        def lost(repo,*args):
            result=original(repo,*args)
            if args[:2]==('push','origin'):
                pushes.append(args);raise P.AdmissionError('synthetic_ack_lost')
            return result
        with patch.object(P,'git',side_effect=lost),self.assertRaises(P.AdmissionError) as ctx:
            self.pa.publish([self.pa.budget_relative])
        candidate=git(self.a,'rev-parse','HEAD')
        self.assertNotEqual(candidate,self.start);self.assertEqual(self.head(),candidate)
        self.assertEqual(self.pa.head,self.start);self.assertEqual(len(pushes),1)
        self.assertEqual(self.verified,[]);self.assertFalse(self.retained(candidate))
        self.assertEqual(ctx.exception.publication['main_publication_outcome'],'UNCONFIRMED')
        with self.assertRaises(P.AdmissionError):self.pa.publish([self.pa.budget_relative])
        self.assertEqual(self.head(),candidate)
    def test_remote_advances_after_push_before_readback_no_false_durability(self):
        self.prepare(self.pa);original=P.git;pushes=[]
        def advance(repo,*args):
            result=original(repo,*args)
            if args[:2]==('push','origin'):
                pushes.append(args)
                git(self.b,'fetch','origin','main');git(self.b,'reset','--hard','FETCH_HEAD')
                (self.b/'concurrent.txt').write_text('synthetic concurrent writer\n')
                git(self.b,'add','concurrent.txt');git(self.b,'commit','-qm','synthetic concurrent advance')
                git(self.b,'push','origin','HEAD:main')
            return result
        with patch.object(P,'git',side_effect=advance),self.assertRaises(P.AdmissionError) as ctx:
            self.pa.publish([self.pa.budget_relative])
        candidate=git(self.a,'rev-parse','HEAD')
        self.assertEqual(self.head(),git(self.b,'rev-parse','HEAD'));self.assertNotEqual(self.head(),candidate)
        self.assertEqual(self.pa.head,self.start);self.assertEqual(len(pushes),1)
        self.assertEqual(self.verified,[]);self.assertFalse(self.retained(candidate))
        self.assertEqual(ctx.exception.publication['publication_phase'],'main_readback')
        self.assertEqual(ctx.exception.publication['main_publication_outcome'],'UNCONFIRMED')
