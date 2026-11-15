"""Real request ancestry, local bare Git, collector and owner; synthetic HTTP only."""
import json,os,subprocess,tempfile,unittest,socket,time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import collect_dynamic_candidates as d
from dynamic_daily_owner import DynamicDailyOwner,DynamicPublicationUnknown,encode,blob
import test_dynamic_candidates as oldtests

NOW=datetime(2026,11,14,14,0,10,tzinfo=timezone.utc);DAY='2026-11-14'
def git(root,*args):return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()
def init(root,name,files):
    repo=root/name;remote=root/(name+'.git')
    subprocess.run(['git','init','--bare','-q',str(remote)],check=True);subprocess.run(['git','init','-q',str(repo)],check=True)
    git(repo,'config','user.name','test');git(repo,'config','user.email','test@example.invalid');git(repo,'remote','add','origin',str(remote))
    for path,raw in files.items():p=repo/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    git(repo,'add','.');git(repo,'commit','-qm','seed');git(repo,'branch','-M','main');git(repo,'push','-q','origin','main');return repo,remote

class DynamicOwnerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name).resolve()
        self.compute,_=init(root,'compute',{'source.txt':b'synthetic source\n'});self.sha=git(self.compute,'rev-parse','HEAD')
        self.request=oldtests.DynamicCandidateRequestTests()._request();self.request.update(trade_date=DAY,market_data_contract_sha=self.sha,candidate_symbols=['AAA','BBB'])
        self.raw=encode(self.request);self.control,_=init(root,'control',{'requests/dynamic-candidate-request.json':self.raw})
        self.control_sha=git(self.control,'rev-parse','HEAD');git(self.control,'update-ref','refs/remotes/origin/discovery-requests',self.control_sha)
        self.repo,self.remote=init(root,'store',{'data/market-data/seed.json':b'{}\n'});self.store=self.repo/'data/market-data'
        self.prior=dict(trade_date='2026-11-12',open=10.,high=12.,low=9.,close=11.,volume=100.)
        for symbol in self.request['candidate_symbols']:d.store.append_daily_bars(self.store,provider='twelve_data_basic',symbol=symbol,asset_class='stocks',bars=[self.prior],series_semantics='daily_regular_ohlcv')
        git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic old prices');git(self.repo,'push','-q','origin','main');self.start=git(self.repo,'rev-parse','HEAD')
        self.now=NOW;self.owner=self.make_owner();self.calls=[]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW if tz is None else NOW.astimezone(tz)
        self.clockpatch=patch.object(d.collectors,'datetime',Clock);self.clockpatch.start();self.addCleanup(self.clockpatch.stop)
        self.netpatch=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'));self.netpatch.start();self.addCleanup(self.netpatch.stop)
    def make_owner(self,run='123-1'):
        return DynamicDailyOwner(self.repo,git(self.repo,'rev-parse','HEAD'),DAY,run,compute=self.compute,compute_sha=self.sha,control=self.control,request_commit=self.control_sha,request_blob=blob(self.raw),clock=lambda:self.now,deadline=time.monotonic()+120)
    def ledger(self):return json.loads(git(self.remote,'show','refs/heads/main:'+self.owner.budget_relative))
    def transport(self,url,**kwargs):
        from urllib.parse import urlparse,parse_qs
        symbol=parse_qs(urlparse(url).query)['symbol'][0]
        kwargs['before_request']();self.calls.append(symbol)
        self.assertEqual(self.ledger()['credits'],len(self.calls))
        row=dict(datetime='2026-11-12',open='10',high='12',low='9',close='11',volume='101' if symbol=='AAA' else '100')
        return json.dumps(dict(status='ok',meta=dict(symbol=symbol,interval='1day'),values=[row,dict(row,datetime='2026-11-13')])).encode()
    def collect(self,transport=None,intraday=None):
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(d.collectors,'_http_raw',side_effect=transport or self.transport),patch.object(d,'collect_regular_window',side_effect=intraday or (lambda **kw:dict(status='ok',snapshot_written=False))):
            return d.collect_request(request=self.request,store_root=self.store,store_config_path=oldtests.ROOT/'config/market-data-store.yaml',access_path=oldtests.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=oldtests.ROOT/'config/dynamic-candidate-collection.yaml',durable_owner=self.owner)
    def test_real_collector_closes_each_symbol_before_next_and_intraday(self):
        def intraday(**kwargs):self.owner.assert_current();return dict(status='ok',snapshot_written=False)
        result=self.collect(intraday=intraday);self.assertEqual(result['status'],'ok');self.assertEqual(self.calls,['AAA','BBB']);self.assertEqual(self.ledger()['credits'],2)
        self.assertEqual(result['daily_history_execution']['local_budget_debits'],2)
        head=git(self.remote,'rev-parse','refs/heads/main')
        for symbol in self.calls:
            prefix=f'data/market-data/series/daily/twelve_data_basic/{symbol}/'
            index=json.loads(git(self.remote,'show',head+':'+prefix+'_index.json'))
            self.assertEqual(index['record_count'],2)
            prior_doc=json.loads(git(self.remote,'show',head+':'+prefix+'000000.json'))
            self.assertEqual(prior_doc['records'][0],self.prior)
        self.assertEqual(json.loads(git(self.remote,'show',head+':data/market-data/series/daily/twelve_data_basic/AAA/_index.json'))['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
        paths=git(self.remote,'ls-tree','-r','--name-only',head).splitlines();self.assertEqual(len([p for p in paths if p.endswith('-terminal.json')]),2);self.assertEqual(len([p for p in paths if p.endswith('.raw')]),2)
    def test_unknown_publication_escapes_collect_request_no_outer_manifest(self):
        real=self.owner.verify
        def fail(*args,**kwargs):raise RuntimeError('unknown durability')
        with patch.object(self.owner,'verify',side_effect=fail),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,[]);self.assertEqual(self.ledger()['credits'],1)
        self.assertFalse(list(self.store.glob('collector-state/*/*-publication.json')))
    def test_raw_durable_parse_rejection_has_terminal_and_reserved_credit(self):
        def bad(url,**kwargs):kwargs['before_request']();self.calls.append('AAA');return b'{bad JSON complete raw'
        result=self.collect(transport=bad);self.assertEqual(result['status'],'STOPPED');self.assertEqual(self.calls,['AAA']);self.assertEqual(self.ledger()['credits'],1)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines();rawpath=next(p for p in paths if p.endswith('.raw'))
        self.assertEqual(git(self.remote,'show','refs/heads/main:'+rawpath),'{bad JSON complete raw');self.assertTrue(any(p.endswith('-terminal.json') for p in paths))
    def test_known_price_rejection_closed_before_other_symbol_continues(self):
        original=self.transport
        def changed(url,**kwargs):
            raw=original(url,**kwargs);doc=json.loads(raw)
            if doc['meta']['symbol']=='AAA':doc['values'][0]['high']='12.1'
            else:
                first=json.loads(git(self.remote,'show','refs/heads/main:data/market-data/series/daily/twelve_data_basic/AAA/_index.json'))
                self.assertEqual(first['record_count'],1);self.assertEqual(first['lineage_segments'][-1]['volume_policy'],'BLOCKED_UNRESOLVED')
            return json.dumps(doc).encode()
        result=self.collect(transport=changed);self.assertEqual(result['status'],'partial');self.assertEqual(self.calls,['AAA','BBB']);self.assertEqual(self.ledger()['credits'],2)
        self.assertEqual(result['daily_history_failures'][0]['reason'],'MarketDataConflict:conflicting bar for AAA 2026-11-12')
    def test_second_raw_publication_unknown_preserves_first_closure(self):
        real=self.owner.verify
        def unknown(repo,sha,**kwargs):
            receipt=real(repo,sha,**kwargs)
            if self.owner.active['symbol']=='BBB' and 'raw' in self.owner.active:raise RuntimeError('synthetic lost durability response')
            return receipt
        with patch.object(self.owner,'verify',side_effect=unknown),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,['AAA','BBB']);self.assertEqual(self.ledger()['credits'],2)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        terminals=[json.loads(git(self.remote,'show','refs/heads/main:'+p)) for p in paths if p.endswith('-terminal.json')]
        self.assertEqual([x['symbol'] for x in terminals],['AAA'])
        self.assertFalse(list(self.store.glob('collector-state/*/*-publication.json')))
    def test_confirmed_expiry_summary_records_reservation_but_zero_http(self):
        from datetime import timedelta
        real=self.owner.verify
        def expired(*args,**kwargs):
            result=real(*args,**kwargs);self.now=NOW+timedelta(minutes=1);return result
        with patch.object(self.owner,'verify',side_effect=expired):result=self.collect()
        self.assertEqual(result['status'],'STOPPED');self.assertEqual(self.calls,[]);self.assertEqual(self.ledger()['credits'],1)
        summary=result['daily_history_execution'];self.assertEqual(summary['local_budget_debits'],1);self.assertEqual(summary['requests_attempted'],0)
        self.assertEqual(summary['budget_reservations']['AAA'],'DEBIT_CONFIRMED_NO_HTTP')
    def test_real_bounded_transport_calls_guard_immediately_before_open(self):
        from email.message import Message
        order=[]
        class Response:
            status=200
            headers=Message()
            headers['Content-Type']='application/json';headers['Content-Length']='2'
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b'{}'
        class Opener:
            def open(self,*args,**kwargs):order.append('open');return Response()
        with patch.object(d.collectors.urllib.request,'build_opener',return_value=Opener()):
            raw=d.collectors._http_raw('https://synthetic.invalid',timeout=1,user_agent='offline-test',maximum_bytes=1024,before_request=lambda:order.append('guard'))
        self.assertEqual(raw,b'{}');self.assertEqual(order,['guard','open'])
    def test_claim_write_failure_never_pushes_or_calls_transport(self):
        original=Path.write_bytes
        def fail(path,data):
            if path.name.startswith('dynamic-daily-attempt-'):raise OSError('synthetic claim disk error')
            return original(path,data)
        with patch.object(Path,'write_bytes',fail),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,[]);self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_manifest_prelude_failure_never_pushes_or_calls_transport(self):
        with patch.object(self.owner,'_manifest',side_effect=OSError('synthetic manifest write')),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,[]);self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_market_append_after_write_error_does_not_publish_partial_market(self):
        real=d.store.append_daily_bars
        def half_write(*args,**kwargs):
            result=real(*args,**kwargs);raise OSError('synthetic post-write disk uncertainty')
        with patch.object(d.store,'append_daily_bars',side_effect=half_write),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,['AAA']);self.assertEqual(self.ledger()['credits'],1)
        index=json.loads(git(self.remote,'show','refs/heads/main:data/market-data/series/daily/twelve_data_basic/AAA/_index.json'))
        self.assertEqual(index['record_count'],1);self.assertFalse(index.get('lineage_segments'))
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines();self.assertTrue(any(p.endswith('.raw') for p in paths));self.assertFalse(any(p.endswith('-terminal.json') for p in paths))
        self.assertFalse(list(self.store.glob('collector-state/*/*-publication.json')))
    def test_truncated_raw_storage_is_not_published_as_complete(self):
        original=Path.open
        class Broken:
            def __init__(self,handle):self.handle=handle
            def __enter__(self):return self
            def __exit__(self,*args):self.handle.close()
            def write(self,data):self.handle.write(data[:8]);self.handle.flush();raise OSError('synthetic truncated raw write')
        def fail(path,*args,**kwargs):
            handle=original(path,*args,**kwargs)
            return Broken(handle) if path.suffix=='.raw' and args and args[0]=='xb' else handle
        with patch.object(Path,'open',fail),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,['AAA']);self.assertEqual(self.ledger()['credits'],1)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines();self.assertFalse(any(p.endswith('.raw') or p.endswith('-terminal.json') for p in paths))
    def test_publication_unknown_cannot_be_swallowed_by_outer_intraday_handler(self):
        def unknown(**kwargs):raise DynamicPublicationUnknown('synthetic propagation sentinel')
        with self.assertRaises(DynamicPublicationUnknown):self.collect(intraday=unknown)
        self.assertEqual(self.calls,['AAA','BBB']);self.assertEqual(self.ledger()['credits'],2)
        self.assertFalse(list(self.store.glob('collector-state/*/*-publication.json')))
    def test_unknown_guard_remote_read_never_falls_back_to_terminal_push(self):
        import admit_market_data_baseline as base
        original=base.git;pushes=[];failed=[]
        def transient(repo,*args,**kwargs):
            if args[:2]==('push','origin'):pushes.append(args)
            if (Path(repo)==self.compute and args==('ls-remote','origin','refs/heads/main') and
                    self.owner.reservation=='DEBIT_CONFIRMED' and not failed):
                failed.append(True);raise RuntimeError('synthetic guard read unknown')
            return original(repo,*args,**kwargs)
        with patch.object(base,'git',side_effect=transient),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(failed,[True]);self.assertEqual(self.calls,[]);self.assertEqual(len(pushes),1)
        self.assertEqual(self.ledger()['credits'],1)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines();self.assertFalse(any(p.endswith('-terminal.json') or p.endswith('.raw') for p in paths))
    def test_reserve_remote_read_unknown_before_debit_has_no_fallback_write(self):
        import admit_market_data_baseline as base
        original_git=base.git;original_reserve=self.owner._reserve;entered=[];pushes=[];failures=[]
        def reserve(*args,**kwargs):entered.append(True);return original_reserve(*args,**kwargs)
        def unknown(repo,*args,**kwargs):
            if args[:2]==('push','origin'):pushes.append(args)
            if entered and args==('ls-remote','origin','refs/heads/main') and not failures:
                failures.append(True);raise RuntimeError('synthetic predebit read unknown')
            return original_git(repo,*args,**kwargs)
        with patch.object(self.owner,'_reserve',side_effect=reserve),patch.object(base,'git',side_effect=unknown),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.calls,[]);self.assertEqual(pushes,[]);self.assertEqual(failures,[True])
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
        self.assertFalse(list(self.store.glob('collector-state/*/*-publication.json')))
    def test_first_introduction_required_before_any_credit(self):
        p=self.control/'requests/dynamic-candidate-request.json';p.write_bytes(encode(dict(self.request,requested_at='2026-11-14T15:00:00Z')))
        git(self.control,'add','.');git(self.control,'commit','-qm','same business repack');new=git(self.control,'rev-parse','HEAD');git(self.control,'update-ref','refs/remotes/origin/discovery-requests',new)
        with self.assertRaises(ValueError):DynamicDailyOwner(self.repo,self.start,DAY,'999-1',compute=self.compute,compute_sha=self.sha,control=self.control,request_commit=new,request_blob=blob(p.read_bytes()),clock=lambda:self.now,deadline=time.monotonic()+120)
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)

if __name__=='__main__':unittest.main()
