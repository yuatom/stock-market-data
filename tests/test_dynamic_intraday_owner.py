import json,os,unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import test_dynamic_daily_owner_integration as fixture
from test_dynamic_daily_owner_integration import git
import market_data_collection as c
import market_data_collectors as b
from dynamic_daily_owner import DynamicPublicationUnknown
class IntradayOwnerTests(unittest.TestCase):
    setUp=fixture.DynamicOwnerIntegration.setUp
    make_owner=fixture.DynamicOwnerIntegration.make_owner
    ledger=fixture.DynamicOwnerIntegration.ledger
    def transport(self,url,**kwargs):
        symbol=parse_qs(urlparse(url).query)['symbol'][0]
        kwargs['before_request']();self.calls.append(symbol)
        self.assertEqual(self.ledger()['credits'],len(self.calls))
        rows=[dict(datetime=f'2026-11-14 09:{30+i:02d}:00',open='10',high='12',low='9',close='11',volume='100') for i in range(30)]
        return json.dumps(dict(status='ok',meta=dict(symbol=symbol,interval='1min'),values=rows)).encode()
    def run_window(self,transport=None,nasdaq=None):
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(b,'_http_raw',side_effect=transport or self.transport),patch.object(c,'fetch_nasdaq_regular',side_effect=nasdaq or (lambda *args:([],{'target_window_match_count':0}))):
            return c.collect_regular_window(mode='discovery_open_30m',stage='discovery_open_30m',trade_date=self.owner.day,start_et='09:30',end_et='10:00',store_root=self.store,universe_config={},config=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-store.yaml'),access=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-collector-access.yaml'),eligible_universe=[('AAA','stocks'),('BBB','stocks')],durable_owner=self.owner)
    def test_two_paid_symbols_close_actual_selected_capture_before_next(self):
        def transport(url,**kwargs):
            if self.calls:
                paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
                self.assertTrue(any('/sessions/' in p and 'aaa' in p for p in paths))
                terminal=[p for p in paths if 'dynamic-intraday-attempt-' in p and p.endswith('-terminal.json')]
                self.assertEqual(len(terminal),1)
                self.assertTrue(json.loads(git(self.remote,'show','refs/heads/main:'+terminal[0]))['fallback_selected'])
            return self.transport(url,**kwargs)
        result=self.run_window(transport);self.assertEqual(self.calls,['AAA','BBB']);self.assertEqual(self.ledger()['credits'],2)
        self.assertTrue(result['snapshot_written'])
    def test_unknown_transport_stops_second_and_no_followup_push(self):
        def unknown(url,**kwargs):kwargs['before_request']();self.calls.append('AAA');raise TimeoutError('synthetic')
        with self.assertRaises(DynamicPublicationUnknown):self.run_window(unknown)
        self.assertEqual(self.calls,['AAA']);self.assertEqual(self.ledger()['credits'],1)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertFalse(any(p.endswith('-terminal.json') for p in paths));self.assertTrue(self.owner.failed)
    def test_unselected_fallback_retained_and_primary_capture_is_selected(self):
        def primary(symbol,*args):
            return [dict(symbol=symbol,asset_class='stocks',session='regular',event_time='2026-11-14T09:30:00-05:00',source_timestamp='2026-11-14T09:30:00-05:00',last_sale=10.,reported_volume=100.)],{'target_window_match_count':1}
        def sparse(url,**kwargs):
            payload=json.loads(self.transport(url,**kwargs));payload['values']=payload['values'][:1];return json.dumps(payload).encode()
        self.run_window(sparse,primary)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        terms=[json.loads(git(self.remote,'show','refs/heads/main:'+p)) for p in paths if p.endswith('-terminal.json')]
        self.assertEqual(len(terms),2);self.assertTrue(all(not x['fallback_selected'] for x in terms))
        self.assertTrue(all('/nasdaq_public_intraday/' in x['selected_capture'] for x in terms));self.assertEqual(self.ledger()['credits'],2)
    def test_known_provider_error_retained_before_second(self):
        def rejected(url,**kwargs):
            self.transport(url,**kwargs)
            return b'{"status":"error","message":"synthetic rejection"}'
        from dynamic_intraday_response import KnownIntradayRejection
        with self.assertRaises(KnownIntradayRejection):self.run_window(rejected)
        self.assertEqual(self.calls,['AAA']);self.assertFalse(self.owner.failed)
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertEqual(sum(p.endswith('.raw') for p in paths),1)
        self.assertEqual(sum(p.endswith('-terminal.json') for p in paths),1)
        self.assertEqual(self.owner.intraday_outcomes[0]['status'],'REJECTED')
    def test_same_business_intraday_claim_cannot_repeat_new_run(self):
        self.run_window()
        # Discard only uncommitted final snapshot in this synthetic clone, then
        # construct a different run at the remote durable head.
        git(self.repo,'reset','--hard',self.owner.head);git(self.repo,'clean','-fd')
        self.owner=self.make_owner('456-1');self.calls=[]
        with self.assertRaises(DynamicPublicationUnknown):self.run_window()
        self.assertEqual(self.calls,[]);self.assertEqual(self.ledger()['credits'],2)
    def test_actual_dynamic_daily_then_intraday_then_final_closure(self):
        import collect_dynamic_candidates as d
        import run_dynamic_guarded as runtime
        def both(url,**kwargs):
            interval=parse_qs(urlparse(url).query)['interval'][0]
            return fixture.DynamicOwnerIntegration.transport(self,url,**kwargs) if interval=='1day' else self.transport(url,**kwargs)
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(b,'_http_raw',side_effect=both),patch.object(c,'fetch_nasdaq_regular',return_value=([],{'target_window_match_count':0})):
            result=d.collect_request(request=self.request,store_root=self.store,store_config_path=fixture.oldtests.ROOT/'config/market-data-store.yaml',access_path=fixture.oldtests.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=fixture.oldtests.ROOT/'config/dynamic-candidate-collection.yaml',durable_owner=self.owner)
        result=runtime.finalize(self.owner,result);self.owner.assert_current()
        self.assertEqual(self.ledger()['credits'],4);self.assertEqual(self.calls,['AAA','BBB','AAA','BBB'])
        self.assertEqual(result['publication_status'],'DURABILITY_VERIFIED')
    def test_raw_publication_unknown_stops_next(self):
        original=self.owner.publish
        def fail(paths,**kwargs):
            if any(p.endswith('.raw') for p in paths):self.owner.abort()
            return original(paths,**kwargs)
        with patch.object(self.owner,'publish',side_effect=fail):
            with self.assertRaises(DynamicPublicationUnknown):self.run_window()
        self.assertEqual(self.calls,['AAA']);self.assertEqual(self.ledger()['credits'],1)
    def test_full_nasdaq_window_does_not_reserve_twelve(self):
        def complete(symbol,*args):
            facts=[dict(symbol=symbol,asset_class='stocks',session='regular',event_time=f'2026-11-14T09:{30+i:02d}:00-05:00',source_timestamp=f'2026-11-14T09:{30+i:02d}:00-05:00',last_sale=10.,reported_volume=100.) for i in range(30)]
            return facts,{'target_window_match_count':30}
        result=self.run_window(nasdaq=complete)
        self.assertEqual(self.calls,[]);self.assertTrue(result['snapshot_written'])
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertFalse(any('dynamic-intraday-attempt-' in p for p in paths))
    def test_symlink_raw_target_never_written(self):
        import hashlib,tempfile
        from pathlib import Path
        outside=Path(self.temp.name)/'outside';outside.write_bytes(b'untouched')
        def linked(url,**kwargs):
            raw=self.transport(url,**kwargs)
            path=self.store/(self.owner.raw_prefix('AAA')+hashlib.sha256(raw).hexdigest()+'.raw')
            path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(outside)
            return raw
        with self.assertRaises(DynamicPublicationUnknown):self.run_window(linked)
        self.assertEqual(outside.read_bytes(),b'untouched');self.assertEqual(self.calls,['AAA'])
    def test_partial_raw_write_unknown_has_no_terminal_or_next_call(self):
        from pathlib import Path
        original=Path.write_bytes
        def partial(path,data):
            if path.suffix=='.raw':original(path,data[:5]);raise OSError('synthetic disk failure')
            return original(path,data)
        with patch.object(Path,'write_bytes',partial):
            with self.assertRaises(DynamicPublicationUnknown):self.run_window()
        self.assertEqual(self.calls,['AAA'])
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertFalse(any(p.endswith('.raw') or p.endswith('-terminal.json') for p in paths))
    def test_actual_collect_request_rejection_finalizes_STOPPED_not_unknown(self):
        import collect_dynamic_candidates as d
        import run_dynamic_guarded as runtime
        def both(url,**kwargs):
            interval=parse_qs(urlparse(url).query)['interval'][0]
            if interval=='1day':return fixture.DynamicOwnerIntegration.transport(self,url,**kwargs)
            self.transport(url,**kwargs);return b'{"status":"error","message":"synthetic"}'
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(b,'_http_raw',side_effect=both),patch.object(c,'fetch_nasdaq_regular',return_value=([],{'target_window_match_count':0})):
            result=d.collect_request(request=self.request,store_root=self.store,store_config_path=fixture.oldtests.ROOT/'config/market-data-store.yaml',access_path=fixture.oldtests.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=fixture.oldtests.ROOT/'config/dynamic-candidate-collection.yaml',durable_owner=self.owner)
        self.assertEqual(result['status'],'STOPPED');self.assertFalse(self.owner.failed)
        self.assertEqual(result['intraday_execution']['conservative_reserved_credits'],1)
        runtime.finalize(self.owner,result);self.owner.assert_current()
        self.assertEqual(self.calls,['AAA','BBB','AAA']);self.assertEqual(self.ledger()['credits'],3)
    def test_guard_expiration_keeps_reservation_zero_transport(self):
        from datetime import timedelta
        def expire(url,**kwargs):
            self.now+=timedelta(minutes=1)
            kwargs['before_request']();self.calls.append('unexpected actual open')
        with self.assertRaises(DynamicPublicationUnknown):self.run_window(expire)
        self.assertEqual(self.calls,[]);self.assertEqual(self.ledger()['credits'],1)
