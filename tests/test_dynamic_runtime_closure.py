import json,unittest
from unittest.mock import patch
import test_dynamic_daily_owner_integration as fixture
from test_dynamic_daily_owner_integration import git
from dynamic_daily_owner import DynamicPublicationUnknown
import run_dynamic_guarded as runtime

class ClosureTests(unittest.TestCase):
    setUp=fixture.DynamicOwnerIntegration.setUp
    make_owner=fixture.DynamicOwnerIntegration.make_owner
    ledger=fixture.DynamicOwnerIntegration.ledger
    transport=fixture.DynamicOwnerIntegration.transport
    collect=fixture.DynamicOwnerIntegration.collect
    def test_real_daily_then_final_inventory_closure(self):
        result=self.collect();daily_head=self.owner.head
        final=runtime.finalize(self.owner,result)
        self.assertNotEqual(final['store_commit_sha'],daily_head)
        self.owner.assert_current()
        self.assertEqual(self.ledger()['credits'],2)
        changed=git(self.repo,'diff','--name-only',daily_head,self.owner.head).splitlines()
        self.assertTrue(all('/series/' not in p for p in changed))
        manifest=json.loads(git(self.repo,'show',self.owner.head+':data/market-data/'+result['publication_manifest_path']))
        self.assertIn('collector-state/market-data-inventory.json',[x['path'] for x in manifest['required_outputs']])
    def test_final_unknown_does_not_retry_and_preserves_daily(self):
        result=self.collect();head=self.owner.head
        original=runtime.base.git;pushes=[]
        def fail(repo,*args):
            if args and args[0]=='push':pushes.append(args);raise RuntimeError('unknown')
            return original(repo,*args)
        with patch.object(runtime.base,'git',side_effect=fail):
            with self.assertRaises(DynamicPublicationUnknown):runtime.finalize(self.owner,result)
        self.assertEqual(len(pushes),1);self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),head)
        self.assertTrue(self.owner.failed)
    def test_post_daily_market_mutation_rejected_before_push(self):
        result=self.collect();head=self.owner.head
        path=self.store/'series/daily/twelve_data_basic/AAA/_index.json'
        path.write_text(path.read_text()+' ')
        with self.assertRaises(DynamicPublicationUnknown):runtime.finalize(self.owner,result)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),head)

    def test_actual_intraday_capture_snapshot_latest_and_inventory_bound(self):
        from market_data_store import write_capture,write_snapshot
        def intraday(**kw):
            self.owner.assert_current()
            rel,sha=write_capture(self.store,trade_date=self.owner.day,session='regular',provider='nasdaq_public_intraday',capture_id='discovery_'+self.request['stage']+'-aaa-160000-et',generated_at='2026-11-14T16:00:00-05:00',qualified_facts=[dict(symbol='AAA',price=11)],window=dict(start='15:45',end='16:00'))
            snap,_=write_snapshot(self.store,stage='discovery_'+self.request['stage'],trade_date=self.owner.day,snapshot_id='synthetic-close',generated_at='2026-11-14T16:00:01-05:00',data_refs=[dict(path=rel,blob_sha=sha)],coverage={},missing=[])
            return dict(status='ok',snapshot_written=True,snapshot_path=snap)
        result=self.collect(intraday=intraday);runtime.finalize(self.owner,result)
        self.owner.assert_current()
        manifest=json.loads((self.store/result['publication_manifest_path']).read_bytes())
        paths=[x['path'] for x in manifest['required_outputs']]
        self.assertTrue(any(x.startswith('sessions/') for x in paths))
        self.assertTrue(any(x.endswith('/latest.json') for x in paths))
    def test_workflow_has_single_guarded_entry_no_outer_publisher(self):
        from pathlib import Path
        import yaml
        text=(Path(runtime.__file__).resolve().parents[1]/'.github/workflows/dynamic-candidate-runtime.yml').read_text()
        doc=yaml.safe_load(text)
        self.assertEqual(doc['permissions'],{'contents':'read','actions':'read'})
        self.assertEqual(doc['concurrency']['group'],'market-data-collector')
        self.assertEqual(text.count('python -B scripts/run_dynamic_guarded.py'),1)
        self.assertNotIn('git push origin',text);self.assertNotIn('git pull --rebase',text)
        self.assertEqual(text.count('fetch-depth: 0'),3)
    def test_actual_B_cli_imports_keep_clean_checkout(self):
        import shutil,subprocess,sys,tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'compute';repo.mkdir()
            shutil.copytree(Path(runtime.__file__).parent,repo/'scripts',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for args in [('init','-q'),('config','user.name','test'),('config','user.email','test@example.invalid'),('add','.'),('commit','-qm','synthetic runtime'),('remote','add','origin','https://github.com/yuatom/stock-market-data.git')]:git(repo,*args)
            # Actual CLI passes both Compute/control local clean checks, then rejects
            # the deliberately wrong Store origin before any API call.
            proc=subprocess.run([sys.executable,'-B',str(repo/'scripts/run_dynamic_guarded.py'),'--control',str(repo),'--control-ref','a'*40,'--store',str(repo)],capture_output=True,text=True)
            self.assertNotEqual(proc.returncode,0)
            self.assertIn('AuthorityError',proc.stderr)
            self.assertEqual(git(repo,'ls-files','--others'),'')
            self.assertEqual(list(repo.rglob('*.pyc')),[])
