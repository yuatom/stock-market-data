"""Dynamic proof + real local publication, synthetic provider only."""
import ast,copy,json,os,tempfile,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import test_historical_tail_publication as old
import test_guarded_history_recovery as fixture
import backfill_market_data_tail as tail

# Reuse only synthetic fixture builders; pytest is not a runtime dependency.
def proof_fixture(root):
    path=Path(__file__).with_name('test_dynamic_baseline_selection.py')
    tree=ast.parse(path.read_text());nodes=[]
    for n in tree.body:
        if isinstance(n,(ast.Import,ast.ImportFrom)) and not any(a.name=='pytest' for a in n.names):nodes.append(n)
        elif isinstance(n,ast.Assign):nodes.append(n)
        elif isinstance(n,ast.Expr) and isinstance(n.value,ast.Call):nodes.append(n)
        elif isinstance(n,ast.FunctionDef) and n.name in ('git','init','source'):
            n.decorator_list=[];nodes.append(n)
    ns={'__file__':str(path)};exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    return ns['source'](root)

class DynamicProofTests(unittest.TestCase):
    def test_single_exact_proof_and_multi_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            h,repo,sha,discovery,selector,universe=proof_fixture(Path(temp))
            def resolve(s=selector,**kw):return tail.resolve_tail_targets(h,repo=repo,store_sha=sha,universe=universe,execution_date='2026-10-18',dynamic_selector=s,discovery_repo=discovery,**kw)
            with self.assertRaises(Exception):resolve()
            selector['targets']=selector['targets'][:1]
            self.assertEqual([x['symbol'] for x in resolve()],['AAA'])
            for field in ('request_commit_sha','request_blob_sha','result_blob_sha'):
                wrong=copy.deepcopy(selector);wrong[field]='0'*40
                with self.subTest(field=field),self.assertRaises(Exception):resolve(wrong)
            for targets in ([],selector['targets']*2,[dict(selector['targets'][0],index_blob_sha='0'*40)]):
                wrong=dict(selector,targets=targets)
                with self.assertRaises(Exception):resolve(wrong)
            with self.assertRaises(Exception):resolve(fixed_selector={'schema_version':1,'targets':[]})
            with self.assertRaises(Exception):resolve(None)
            self.assertEqual(fixture.git(repo,'status','--porcelain'),'')

class DynamicPublicationTests(fixture.RecoveryIntegrationFixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.tail=tail
        self.new=copy.deepcopy(self.old)+[dict(self.old[-1],trade_date='2026-10-16')]
        self.config['collector']['twelve_data_daily']['bounded_overlap_revision_policy']='price_only_volume_blocked_v1'
        # Real request ancestry/result/index blobs, built atop actual test Store.
        self.proof_temp=tempfile.TemporaryDirectory();self.addCleanup(self.proof_temp.cleanup)
        _,_,_,self.discovery,_,_=proof_fixture(Path(self.proof_temp.name))
        commit=fixture.git(self.discovery,'rev-parse','HEAD')
        request=json.loads(fixture.git(self.discovery,'show',commit+':requests/dynamic-candidate-request.json'))
        result=dict(request,mode='discovery_close',snapshot_stage='discovery_close',persistent_collection_membership=False,daily_history_available=[],daily_history_failures=[dict(symbol=x,reason=f'MarketDataConflict:conflicting bar for {x} 2026-10-15') for x in ('AAA','BBB')])
        p='data/market-data/collector-state/2026-10/2026-10-17-dynamic-candidate-close.json'
        target=self.repo/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(result))
        for symbol in ('AAA','BBB'):
            index_path=self.repo/f'data/market-data/series/daily/twelve_data_basic/{symbol}/_index.json'
            index=json.loads(index_path.read_text());index['asset_class']='stocks';index_path.write_text(json.dumps(index))
        fixture.git(self.repo,'add','.');fixture.git(self.repo,'commit','-qm','dynamic failure proof');fixture.git(self.repo,'push','origin','HEAD:main')
        self.base=fixture.git(self.repo,'rev-parse','HEAD')
        self.selector=dict(request_commit_sha=commit,request_blob_sha=fixture.git(self.discovery,'rev-parse',commit+':requests/dynamic-candidate-request.json'),result_blob_sha=fixture.git(self.repo,'rev-parse',self.base+':'+p),targets=[dict(symbol=x,index_blob_sha=fixture.git(self.repo,'rev-parse',self.base+f':data/market-data/series/daily/twelve_data_basic/{x}/_index.json')) for x in ('AAA','BBB')])
        self.selector['targets']=self.selector['targets'][:1]
        self.universe={'daily_series':[{'symbol':'FIX','asset_class':'etf'}]}
    def run_tail(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return old.NOW if tz is None else old.NOW.astimezone(tz)
        with patch.object(self.h.collectors,'datetime',Clock),patch.object(self.h.collectors,'_http_json',side_effect=self.payload):
            return tail.backfill(self.h,compute=self.compute,compute_sha=self.compute_sha,repo=self.repo,store_sha=self.base,execution_date=fixture.DAY,historical_cutoff='2026-10-16',request_id='321-1',config=self.config,universe=self.universe,access=self.access,key='SYNTHETIC',clock=lambda:old.NOW,dynamic_selector=self.selector,discovery_repo=self.discovery)
    def test_single_request_append_without_membership_or_old_row_changes(self):
        result=self.run_tail();self.assertEqual(result['status'],'PASS');self.assertEqual(self.http_count,1)
        head=result['store_commit_sha'];self.assertEqual(self.current_budget()['credits'],1)
        for symbol in ('AAA','BBB'):
            p=f'data/market-data/series/daily/twelve_data_basic/{symbol}/000000.json'
            self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+p),fixture.git(self.repo,'rev-parse',head+':'+p))
        self.assertEqual(self.universe,{'daily_series':[{'symbol':'FIX','asset_class':'etf'}]})
    def test_price_and_volume_conflict_one_request_old_price_preserved_quarantine_closed(self):
        self.new[-2]['high']+=0.1;self.new[-2]['volume']+=1
        with self.assertRaises(Exception) as caught:self.run_tail()
        self.assertEqual(caught.exception.code,'replacement_rejected');self.assertEqual(self.http_count,1)
        head=caught.exception.confirmed_terminal.store_commit_sha
        p='data/market-data/series/daily/twelve_data_basic/AAA/'
        index=json.loads(fixture.git(self.repo,'show',head+':'+p+'_index.json'))
        self.assertEqual(index['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        for shard in index['shards']:
            self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+p+shard['path']),fixture.git(self.repo,'rev-parse',head+':'+p+shard['path']))
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/tags/market-data-read/'+head),head)
        p='data/market-data/series/daily/twelve_data_basic/BBB/_index.json'
        self.assertEqual(fixture.git(self.repo,'rev-parse',self.base+':'+p),fixture.git(self.repo,'rev-parse',head+':'+p))
    def test_multi_target_rejected_before_any_provider_or_publication(self):
        self.selector['targets'].append(dict(symbol='BBB',index_blob_sha=fixture.git(self.repo,'rev-parse',self.base+':data/market-data/series/daily/twelve_data_basic/BBB/_index.json')))
        # First response would have a volume conflict, second a price conflict;
        # neither may be requested because multi-target admission is forbidden.
        self.new[-2]['volume']+=1
        with self.assertRaises(Exception):self.run_tail()
        self.assertEqual(self.http_count,0)
        self.assertEqual(fixture.git(self.store_remote,'rev-parse','refs/heads/main'),self.base)
    def test_insufficient_budget_stops_before_provider(self):
        self.seed_budget(200)
        with self.assertRaises(Exception) as caught:self.run_tail()
        self.assertEqual(caught.exception.code,'budget_exhausted')
        self.assertEqual(self.http_count,0);self.assertEqual(self.current_budget()['credits'],200)
    def test_quarantine_durability_unknown_stops_after_one_request(self):
        self.new[-2]['high']+=0.1;self.new[-2]['volume']+=1
        original=tail.recovery.verify_outputs
        def verify(repo,sha,manifest,outputs,*args,**kwargs):
            for output in outputs:
                if 'daily-history-tail-backfill-' in output['path']:
                    report=json.loads(fixture.git(repo,'show',sha+':'+output['path']))
                    if report.get('volume_quarantine'):raise RuntimeError('synthetic unknown')
            return original(repo,sha,manifest,outputs,*args,**kwargs)
        with patch.object(tail.recovery,'verify_outputs',side_effect=verify):
            with self.assertRaises(Exception) as caught:self.run_tail()
        self.assertEqual(caught.exception.code,'publication_unconfirmed')
        self.assertEqual(self.http_count,1);self.assertEqual(self.current_budget()['credits'],1)
    def test_metadata_rejection_stops_and_preserves_old_rows(self):
        item=dict(symbol='AAA',existing=self.old,effective=None,target_records=3,old_index={'lineage_segments':[]})
        self.new[-2]['source_timestamp']='different'
        with self.assertRaises(Exception):tail.qualify_tail(self.h,item,self.new,'2026-10-16',price_only=True)
        self.assertEqual(self.http_count,0)

if __name__=='__main__':unittest.main()
