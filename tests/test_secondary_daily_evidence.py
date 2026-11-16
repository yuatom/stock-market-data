import hashlib
import json
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))

class SecondaryEvidenceTest(unittest.TestCase):
    def api(self):
        import secondary_daily_evidence
        return secondary_daily_evidence.normalize_capture

    def capture(self, payload=None):
        if payload is None:
            payload={'chart':{'error':None,'result':[{'meta':{'symbol':'AMD','currency':'USD','exchangeTimezoneName':'America/New_York','dataGranularity':'1d'},'timestamp':[1794580200],'indicators':{'quote':[{'close':['1.12345678901234567890123456789']}],'adjclose':[{'adjclose':['1.1']}]},'events':{'splits':{'1794580200':{'date':1794580200,'numerator':3,'denominator':1,'splitRatio':'3:1'}},'dividends':{'1794580200':{'date':1794580200,'amount':'0.01'}}}}]}}
        raw=json.dumps(payload).encode()
        url='https://query2.finance.yahoo.com/v8/finance/chart/AMD?interval=1d&range=6mo&events=div%2Csplits%2CcapitalGains'
        meta={'url':url,'final_url':url,'http_status':200,'transport':'success','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'started_at':'2026-11-16T13:20:00+00:00','finished_at':'2026-11-16T13:21:00+00:00'}
        return raw,meta

    def test_raw_binding_and_actions_do_not_promote(self):
        raw,m=self.capture(); r=self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')
        self.assertEqual(r['capture']['sha256'],m['sha256'])
        self.assertFalse(r['capture']['authenticated_origin'])
        self.assertEqual(r['bars'][0]['close'],'1.12345678901234567890123456789')
        self.assertEqual(r['adjusted_close_observations'][0]['value'],'1.1')
        self.assertEqual(r['corporate_actions']['splits'][0]['numerator'],'3')
        self.assertFalse(r['technical_eligible'])
        self.assertEqual(r['adjustment_semantics'],'unverified_provider_observation')

    def test_tamper_request_identity_and_clock_rejected(self):
        for mutation in ['raw','hash','bytes','symbol','redirect','failed','naive','reversed','credentials','duplicate_symbol']:
            raw,m=self.capture()
            if mutation=='raw':raw+=b' '
            if mutation=='hash':m['sha256']='0'*64
            if mutation=='bytes':m['bytes']+=1
            if mutation=='symbol':m['url']=m['url'].replace('/AMD?','/SOXX?')
            if mutation=='redirect':m['final_url']='https://example.com/'
            if mutation=='failed':m['transport']='failed'
            if mutation=='naive':m['started_at']='2026-11-16T13:20:00'
            if mutation=='reversed':m['finished_at']='2026-11-15T13:20:00+00:00'
            if mutation=='credentials':m['url']=m['url'].replace('https://','https://user:pass@')
            if mutation=='duplicate_symbol':m['url']+='&interval=1d'
            if mutation in ['symbol','credentials','duplicate_symbol']:
                m['final_url']=m['url']
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')

    def test_actions_absent_distinct_empty_and_bad_adjusted_array(self):
        raw,m=self.capture();p=json.loads(raw);r=p['chart']['result'][0];r.pop('events')
        raw,m=self.capture(p);out=self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')
        self.assertEqual(out['corporate_actions']['status'],'absent_not_proof_of_no_actions')
        r['events']={};raw,m=self.capture(p);out=self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')
        self.assertEqual(out['corporate_actions']['status'],'provided_coverage_unverified')
        r['indicators']['adjclose'][0]['adjclose']=[];raw,m=self.capture(p)
        with self.assertRaises(ValueError):self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')

    def test_invalid_split_ratio_rejected(self):
        for numerator,denominator,ratio in [(3,0,'3:0'),(3,1,'2:1'),(True,1,'3:1')]:
            raw,m=self.capture();p=json.loads(raw);s=p['chart']['result'][0]['events']['splits']['1794580200'];s.update(numerator=numerator,denominator=denominator,splitRatio=ratio)
            raw,m=self.capture(p)
            with self.subTest(ratio=ratio),self.assertRaises(ValueError):self.api()(raw,m,provider='yahoo',symbol='AMD',as_of_date='2026-11-16')

    def test_sina_duplicate_price_members_rejected(self):
        raw=b'var([{"d":"2026-11-13","c":"1","c":"2"}]);'
        _,m=self.capture();m.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        m['url']=m['final_url']='https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?symbol=AMD&num=120'
        with self.assertRaises(ValueError):self.api()(raw,m,provider='sina',symbol='AMD',as_of_date='2026-11-16')

    def test_synthetic_fixture_reproduces_versioned_isolated_contract(self):
        fixture=json.loads((pathlib.Path(__file__).parent/'fixtures/secondary_daily/synthetic_v1.json').read_text())
        self.assertEqual(fixture['fixture_kind'],'synthetic_only_not_market_evidence')
        results=[]
        for provider in ['sina','yahoo']:
            if provider=='sina':
                raw=fixture['sina_text'].encode('gbk')
                url='https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?symbol=TEST&num=2'
            else:
                raw=json.dumps(fixture['yahoo_payload']).encode()
                url='https://query2.finance.yahoo.com/v8/finance/chart/TEST?interval=1d&range=1mo&events=div%2Csplits%2CcapitalGains'
            meta={'url':url,'final_url':url,'http_status':200,'transport':'success','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'started_at':'2020-01-04T12:00:00+00:00','finished_at':'2020-01-04T12:00:01+00:00'}
            r=self.api()(raw,meta,provider=provider,symbol=fixture['symbol'],as_of_date=fixture['as_of_date'])
            self.assertEqual(r['schema_version'],'secondary-daily-candidate/v1')
            self.assertEqual(r['bars'][0]['open'],'100.1234567890123456789')
            self.assertEqual(r['bars'][1]['volume'],9007199254740993)
            self.assertIsNone(r['bars'][0]['volume'])
            self.assertEqual(r['quality_flags'][0]['trade_date'],'2020-01-03')
            for field in ['technical_eligible','market_fact_authority','volume_eligible','action_coverage_verified']:
                self.assertFalse(r[field])
            results.append(r)
        self.assertNotEqual(results[0]['provider'],results[1]['provider'])
        self.assertEqual(results[1]['corporate_actions']['splits'][0]['numerator'],'4')

if __name__=='__main__':unittest.main()
