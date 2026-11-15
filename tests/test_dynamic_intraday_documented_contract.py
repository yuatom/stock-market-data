"""Documentation-derived shapes, NOT captured/live provider responses.
Source: https://twelvedata.com/docs/market-data/time-series (2026-11-15).
Values below are synthetic. No API request or account access occurs.
"""
import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import dynamic_intraday_response as r

class Writer:
    intraday_scope={'interval':'1min'}
    def __init__(self,root):self.root=Path(root);self.allowed=set();self.events=[]
    def reserve(self,*a):self.events.append('reserve')
    def raw_prefix(self,s):return 'synthetic/'+s+'/'
    def safe_path(self,p):return self.root/p
    def retain_raw(self,p):self.events.append('retained')
    def before_request(self):self.events.append('guard')
    def abort(self):raise RuntimeError('UNKNOWN')

class DocumentedContractTests(unittest.TestCase):
    def run_payload(self,payload):
        with tempfile.TemporaryDirectory() as tmp:
            w=Writer(tmp)
            def transport(*a,**kw):
                kw['before_request']();return json.dumps(payload).encode()
            with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(r.base,'_http_raw',side_effect=transport),patch.object(r.admission,'load_helpers',return_value={}):
                try:
                    return r.fetch_guarded('AAPL','stocks','2021-09-16','15:45','16:00',store_root=Path(tmp),config={'collector':{'budgets':{'twelve_data_basic':{}}}},access={'twelve_data_basic':{'base_url':'https://not-used.invalid','time_series_path':'/time_series'},'http':{'timeout_seconds':1,'user_agent':'offline-test'}},writer=w)
                finally:self.assertEqual(w.events,['reserve','guard','retained'])
    def payload(self):
        return {'status':'ok','meta':{'symbol':'AAPL','interval':'1min','exchange_timezone':'America/New_York','currency':'USD','exchange':'NASDAQ','mic_code':'XNAS','type':'Common Stock'},'values':[{'datetime':'2021-09-16 15:59:00','open':'100','high':'102','low':'99','close':'101','volume':'10'}]}
    def test_documented_shape_accepts(self):
        facts,_=self.run_payload(self.payload());self.assertEqual(len(facts),1);self.assertEqual(facts[0]['symbol'],'AAPL')
    def test_wrong_symbol_or_interval_rejected(self):
        for key,value in [('symbol','MSFT'),('interval','1day')]:
            with self.subTest(key=key):
                d=self.payload();d['meta'][key]=value
                with self.assertRaises(r.KnownIntradayRejection):self.run_payload(d)
    def test_invalid_meta_shape_known_rejection(self):
        for meta in [None,[],42,'invalid']:
            with self.subTest(meta=meta):
                d=self.payload();d['meta']=meta
                with self.assertRaises(r.KnownIntradayRejection):self.run_payload(d)
if __name__=='__main__':unittest.main()
