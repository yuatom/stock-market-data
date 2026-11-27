from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml
import reconcile_retained_soxx_dispatch as D

SHA='1'*40
COMMIT='2'*64
def env():
    return dict(GITHUB_REPOSITORY=D.COMPUTE,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
                GITHUB_ACTOR='yuatom',GITHUB_ACTOR_ID='10399225',GITHUB_TRIGGERING_ACTOR='yuatom',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='19',
                GITHUB_SHA=SHA,GITHUB_WORKFLOW_SHA=SHA,GITHUB_WORKFLOW_REF=D.WORKFLOW_REF,
                EXPECTED_COMPUTE_SHA=SHA,
                REQUEST_JSON=json.dumps({'kind':'soxx_web_appendix_recovery_v1','commitment':COMMIT}))

class Dispatch(unittest.TestCase):
    def test_strict_owner_and_no_input_leak(self):
        self.assertEqual(D.public_request(env()),(SHA,'19',COMMIT))
        for k,v in (('GITHUB_ACTOR','reconciler[bot]'),('GITHUB_TRIGGERING_ACTOR','other'),('GITHUB_ACTOR_ID','10399226'),
                    ('GITHUB_RUN_ATTEMPT','2'),('GITHUB_SHA','3'*40),('EXPECTED_COMPUTE_SHA','3'*40),
                    ('REQUEST_JSON','{"kind":"soxx_web_appendix_recovery_v1","commitment":"'+COMMIT+'","private":{}}')):
            with self.subTest(k=k),self.assertRaises(ValueError):
                D.public_request(dict(env(),**{k:v}))

    def test_bound_private_receipt_and_expiry(self):
        raw=json.dumps(dict(schema_version=1,kind='soxx_retained_reconciliation_receipt',compute_sha=SHA,
                            workflow_ref=D.WORKFLOW_REF,action='exact_price_appendix_only',
                            valid_from='2035-01-24T00:00:00Z',valid_until='2035-01-24T00:10:00Z',profile=json.dumps({'schema_version':3,'salt':'a'*64})),
                       separators=(',',':'),sort_keys=True).encode()
        commit=hashlib.sha256(D.DOMAIN+raw).hexdigest()
        at=datetime(2035,1,24,0,5,tzinfo=timezone.utc)
        with patch.object(D,'load_profile',return_value='fixture') as load:
            self.assertEqual(D.bound_receipt(raw,SHA,commit,at),'fixture')
            load.assert_called_once_with(json.dumps({'schema_version':3,'salt':'a'*64}).encode())
        for b,c,t in ((raw,b'0'*64,at),(raw+b' ',commit,at),(raw,commit,at.replace(minute=12))):
            with self.subTest(t=t),self.assertRaises(ValueError):
                D.bound_receipt(b,SHA,c,t)

    def test_workflow_keeps_gld_and_shared_lock(self):
        base=Path(__file__).resolve().parents[1]
        new=yaml.load((base/'.github/workflows/reconcile-retained-daily.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(new['permissions'],{'contents':'read'})
        baseline=hashlib.sha256(json.dumps(new['jobs']['reconcile']['steps'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(baseline,'6bb787ff54736a095196299c69aac21410ad6fd432ab03fdf8707b2650211aea')
        job=new['jobs']['reconcile_soxx']
        self.assertEqual(job['concurrency'],new['jobs']['reconcile']['concurrency'])
        self.assertNotIn('environment',job)
        self.assertEqual(job['steps'][-1]['env']['MARKET_DATA_STORE_TOKEN'],'${{ secrets.MARKET_DATA_STORE_TOKEN }}')

    def test_private_receipt_exact_normal_single_addition_in_local_bare(self):
        import subprocess
        import tempfile
        from datetime import timedelta
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            remote,author,delivery=(p/k for k in ('remote.git','author','delivery'))
            for x in (remote,author,delivery): x.mkdir()
            def g(root,*args):
                return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()
            g(remote,'init','--bare','-b','main')
            g(author,'init','-b','main')
            g(author,'config','user.name','Fixture')
            g(author,'config','user.email','fixture@example.invalid')
            g(author,'commit','--allow-empty','-m','approved store base')
            base=g(author,'rev-parse','HEAD')
            now=datetime.now(timezone.utc)
            ts=lambda offset:(now+timedelta(minutes=offset)).strftime('%Y-%m-%dT%H:%M:%SZ')
            raw=json.dumps(dict(schema_version=1,kind='soxx_retained_reconciliation_receipt',compute_sha=SHA,
                                workflow_ref=D.WORKFLOW_REF,action='exact_price_appendix_only',
                                valid_from=ts(-5),valid_until=ts(5),profile=json.dumps({'schema_version':3,'salt':'a'*64})),
                           separators=(',',':'),sort_keys=True).encode()
            commitment=hashlib.sha256(D.DOMAIN+raw).hexdigest()
            ref=f'refs/heads/reconciliation-receipts/soxx-web-appendix/{SHA}/{commitment}'
            f=author/D.RECEIPT
            f.parent.mkdir()
            f.write_bytes(raw)
            g(author,'add','.')
            g(author,'commit','-m','exact private single-addition authority')
            oid=g(author,'rev-parse','HEAD')
            g(author,'push',str(remote),oid+':'+ref)
            with patch.object(D,'STORE_URL',str(remote)),patch.object(D,'load_profile',return_value=SimpleNamespace(base=base)):
                observed,resolved,content,_=D._materialize_receipt(delivery,SHA,commitment)
                self.assertEqual((observed,resolved,content),(ref,oid,raw))
            # Same opaque hash is not enough if the supposedly immutable receipt
            # also adds an unrelated file.
            (author/'unexpected').write_text('out of scope')
            g(author,'add','.')
            g(author,'commit','--amend','--no-edit')
            g(author,'push','--force',str(remote),g(author,'rev-parse','HEAD')+':'+ref)
            other=p/'other'; other.mkdir()
            with patch.object(D,'STORE_URL',str(remote)),patch.object(D,'load_profile',return_value=SimpleNamespace(base=base)):
                with self.assertRaises(ValueError):
                    D._materialize_receipt(other,SHA,commitment)

    def test_bad_owner_never_accesses_private_read_or_writer(self):
        with patch.object(D,'_private_metadata') as private,patch.object(D,'publish_atomically') as write:
            with self.assertRaises(ValueError):
                D.run(Path('/fixture'),Path('/fixture'),Path('/fixture'),dict(env(),GITHUB_ACTOR='other'))
            private.assert_not_called()
            write.assert_not_called()

    def test_atomic_publish_exact_outputs_and_stable_replay_claim(self):
        from types import SimpleNamespace
        from dataclasses import replace
        import tempfile,subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            remote,repo=root/'remote.git',root/'repo'
            remote.mkdir();repo.mkdir()
            def g(where,*a):
                return subprocess.check_output(['git','-C',str(where),*a],stderr=subprocess.DEVNULL,text=True).strip()
            g(remote,'init','--bare','-b','main')
            g(repo,'init','-b','main')
            g(repo,'config','user.name','Fixture');g(repo,'config','user.email','fixture@example.invalid')
            g(repo,'remote','add','origin',str(remote))
            g(repo,'commit','--allow-empty','-m','store base fixture')
            base=g(repo,'rev-parse','HEAD')
            g(repo,'push','origin','main')
            prefix='data/market-data/'
            manifest=prefix+'collector-state/fixture-publication.json'
            artifacts=[prefix+'recovery/fixture-'+str(i)+'.json' for i in range(3)]
            blobs={}
            for name in artifacts:
                f=repo/name; f.parent.mkdir(parents=True,exist_ok=True); f.write_text('fixture data')
                blobs[name]=g(repo,'hash-object',name)
            f=repo/manifest;f.parent.mkdir(parents=True)
            f.write_text(json.dumps(dict(schema_version=1,request_id='fixture',trade_date='2035-01-15',stage='soxx_fixture',
                                        required_outputs=[dict(path=a.removeprefix(prefix),blob_sha=blobs[a]) for a in artifacts])))
            blobs[manifest]=g(repo,'hash-object',manifest)
            g(repo,'add','.')
            g(repo,'commit','-m','four immutable sources')
            plan=SimpleNamespace(trade_dates=('2035-01-14','2035-01-15'),base=base,head=g(repo,'rev-parse','HEAD'),
                                 evidence_base=base,tree=g(repo,'rev-parse','HEAD^{tree}'),blobs=blobs,manifest=manifest)
            # Force time to reach the exact expiry only after slow Git preflight
            # and after the last authority ref check, with no production write.
            events=[]
            old_resolve=D._resolve_ref
            authority_ref='refs/heads/fixture-authority'
            expiry=datetime(2035,1,24,0,10,tzinfo=timezone.utc)
            approved=SimpleNamespace(start=expiry.replace(minute=5),end=expiry)
            class Clock:
                @classmethod
                def now(cls,tz):
                    events.append('utc')
                    if events[-2:]!=['receipt-ref','utc']:
                        raise AssertionError('time check must follow exact late ref check')
                    return expiry
            def resolve(where,ref):
                if ref==authority_ref:
                    events.append('receipt-ref')
                    return '6'*40
                return old_resolve(where,ref)
            with patch.object(D,'_resolve_ref',side_effect=resolve),patch.object(D,'datetime',Clock):
                with self.assertRaises(ValueError):
                    D.publish_atomically(repo,SHA,'19',COMMIT,'6'*40,plan,
                        before_push=lambda:D.require_current_receipt(repo,authority_ref,'6'*40,approved))
            self.assertEqual(events,['receipt-ref','utc'])
            self.assertEqual(g(remote,'rev-parse','main'),base)
            self.assertEqual(g(remote,'for-each-ref','--format=%(refname)','refs/tags/','refs/heads/reconciliation-claims/'),'')
            # A rejected branch/ref is an atomic all-or-nothing failure.
            hook=remote/'hooks/pre-receive'
            hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o700)
            with self.assertRaises(ValueError):
                D.publish_atomically(repo,SHA,'19',COMMIT,'6'*40,plan,before_push=lambda:None)
            self.assertEqual(g(remote,'rev-parse','main'),base)
            self.assertEqual(g(remote,'for-each-ref','--format=%(refname)','refs/tags/','refs/heads/reconciliation-claims/'),'')
            hook.unlink()
            # Race an ordinary retained caller into the same claim after the
            # absence check but before receive-pack. The claim lease must reject
            # the whole transaction; no data or durable tag may slip through.
            original=D._output
            def race(where,*args,**kwargs):
                if args[0]=='push':
                    g(repo,'push','origin',base+':'+D.claim_ref())
                return original(where,*args,**kwargs)
            with patch.object(D,'_output',side_effect=race),self.assertRaises(ValueError):
                D.publish_atomically(repo,SHA,'19',COMMIT,'6'*40,plan,before_push=lambda:None)
            self.assertEqual(g(remote,'rev-parse','main'),base)
            self.assertEqual(g(remote,'tag','--list'),'')
            self.assertEqual(g(remote,'rev-parse',D.claim_ref()),base)
            g(remote,'update-ref','-d',D.claim_ref(),base)  # fixture reset, never in production code
            result=D.publish_atomically(repo,SHA,'19',COMMIT,'6'*40,plan,before_push=lambda:None)
            oid=result['store_commit_sha'];claim_ref=D.claim_ref()
            claim_oid=g(remote,'rev-parse',claim_ref)
            self.assertEqual(g(remote,'rev-parse','main'),oid)
            self.assertEqual(g(remote,'rev-parse','refs/tags/market-data-read/'+oid),oid)
            self.assertEqual(g(remote,'rev-list','--parents','-n','1',oid).split(),[oid,base])
            self.assertEqual(g(remote,'rev-parse',oid+'^{tree}'),plan.tree)
            claim=json.loads(g(remote,'show',claim_oid+':claim.json'))
            self.assertEqual((claim['actor_id'],claim['run_id'],claim['receipt_commitment'],claim['published_store_commit']),
                             ('10399225','19',COMMIT,oid))
            # Simulate admin rewind (retained claim is not deleted).
            g(repo,'push','--force','origin',base+':refs/heads/main')
            for dates in (plan.trade_dates,('2035-01-15','2035-01-16')):
                plan.trade_dates=dates
                with self.assertRaises(ValueError):
                    D.publish_atomically(repo,SHA,'20','7'*64,'8'*40,plan,before_push=lambda:None)
            self.assertEqual(g(remote,'rev-parse',claim_ref),claim_oid)
            self.assertEqual(g(remote,'rev-parse','main'),base)
    def test_uncertain_atomic_attempt_is_not_retried(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        e=dict(env(),MARKET_DATA_STORE_TOKEN='fake')
        plan=SimpleNamespace()
        with patch.object(D,'_environment',side_effect=lambda *a:nullcontext()), \
             patch.object(D,'_source'),patch.object(D,'_private_metadata'), \
             patch.object(D,'_materialize_receipt',return_value=('private-ref','synthetic-oid',b'receipt',plan)), \
             patch.object(D,'_resolve_ref',return_value='synthetic-oid'), \
             patch.object(D,'_quiet_store_checkout'),patch.object(D,'_require_origins'),patch.object(D,'preflight'), \
             patch.object(D,'_require_local_and_remote'),patch.object(D,'bound_receipt'), \
             patch.object(D,'publish_atomically',side_effect=ValueError('uncertain')) as claim, \
             patch.object(D,'_verify_store_publisher') as separately_invoked:
            with self.assertRaises(ValueError):
                D.run(Path('/fixture'),Path('/fixture'),Path('/fixture'),e)
            claim.assert_called_once()
            separately_invoked.assert_not_called()
