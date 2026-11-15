"""Offline observations of unmodified production functions, not remediation claims."""
import json,os,socket,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
import market_data_collection as c
import market_data_collectors as b
ROOT=Path(__file__).resolve().parents[1]
DAY='2026-11-14'
NOW=datetime(2026,11,14,16,0,tzinfo=timezone.utc)
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return NOW if tz is None else NOW.astimezone(tz)
class Observations(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.config=b.load_yaml(ROOT/'config/market-data-store.yaml');self.access=b.load_yaml(ROOT/'config/market-data-collector-access.yaml')
        for p in (patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(socket,'socket',side_effect=AssertionError('real network forbidden')),patch.object(b,'datetime',Clock),patch.object(c,'datetime',Clock)):
            p.start();self.addCleanup(p.stop)
    def state(self):return b._load_budget_state(b._budget_path(self.root,DAY),DAY)
    def fetch(self,symbol='AAA'):
        return c.fetch_twelve_regular(symbol,'stocks',DAY,'09:30','09:31',store_root=self.root,config=self.config,access=self.access)
    def test_success_http_precedes_local_debit(self):
        observed=[]
        def http(*args,**kwargs):observed.append(self.state()['credits']);return {'values':[]}
        with patch.object(b,'_http_json',side_effect=http):self.fetch()
        self.assertEqual(observed,[0]);self.assertEqual(self.state()['credits'],1)
    def test_provider_error_response_still_debits_locally(self):
        with patch.object(b,'_http_json',return_value={'status':'error','message':'synthetic rejection'}):
            with self.assertRaises(RuntimeError):self.fetch()
        self.assertEqual(self.state()['credits'],1)
    def test_unknown_http_repeat_attempt_has_no_claim_or_debit(self):
        with patch.object(b,'_http_json',side_effect=TimeoutError('synthetic timeout')) as http:
            for _ in range(2):
                with self.assertRaises(TimeoutError):self.fetch()
        self.assertEqual(http.call_count,2);self.assertEqual(self.state()['credits'],0)
        self.assertEqual(list(self.root.rglob('*')),[])
    def test_real_json_parse_failure_before_debit_and_no_raw_retained(self):
        class Response:
            status=200;headers={'content-type':'application/json'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{broken'
        with patch.object(b.urllib.request,'urlopen',return_value=Response()) as http:
            with self.assertRaises(json.JSONDecodeError):self.fetch()
        self.assertEqual(http.call_count,1);self.assertEqual(self.state()['credits'],0)
        self.assertEqual(list(self.root.rglob('*')),[])
    def test_minute_budget_can_fail_after_http(self):
        def http(*args,**kwargs):
            b.consume_twelve_credit(self.root,DAY,amount=8,per_day=200,per_minute=8)
            return {'values':[]}
        with patch.object(b,'_http_json',side_effect=http) as transport:
            with self.assertRaises(b.BudgetExceeded):self.fetch()
        self.assertEqual(transport.call_count,1);self.assertEqual(self.state()['credits'],8)
    def test_daily_exhaustion_blocks_transport(self):
        p=b._budget_path(self.root,DAY);p.parent.mkdir(parents=True);p.write_text(json.dumps(dict(schema_version=1,trade_date=DAY,credits=200,minutes={})))
        with patch.object(b,'_http_json') as http:
            with self.assertRaises(b.BudgetExceeded):self.fetch()
        http.assert_not_called()
    def test_real_shared_loop_continues_after_unknown_twelve(self):
        calls=[]
        def http(url,**kwargs):
            from urllib.parse import parse_qs,urlparse
            calls.append(parse_qs(urlparse(url).query)['symbol'][0]);raise TimeoutError('synthetic timeout')
        with patch.object(c,'fetch_nasdaq_regular',return_value=([],{'provider':c.NASDAQ,'target_window_match_count':0})),patch.object(b,'_http_json',side_effect=http):
            result=c.collect_regular_window(mode='discovery_open_30m',stage='discovery_open_30m',trade_date=DAY,start_et='09:30',end_et='09:31',store_root=self.root,universe_config={},config=self.config,access=self.access,eligible_universe=[('AAA','stocks'),('BBB','stocks')])
        self.assertEqual(calls,['AAA','BBB']);self.assertEqual(self.state()['credits'],0)
        self.assertFalse(result['snapshot_written'])
