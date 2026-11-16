import copy
import importlib.util
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))

class SecondaryDailyTest(unittest.TestCase):
    def adapter(self):
        self.assertIsNotNone(importlib.util.find_spec('secondary_daily_adapter'), 'offline adapter must exist')
        import secondary_daily_adapter
        return secondary_daily_adapter

    def payload(self):
        return {'chart': {'error': None, 'result': [{'meta': {'symbol':'AMD','currency':'USD','exchangeTimezoneName':'America/New_York','dataGranularity':'1d'}, 'timestamp':[1794580200], 'indicators': {'quote':[{'open':[1.1234567],'high':[2],'low':[1],'close':[1.5],'volume':[None]}]}}]}}

    def test_yahoo_preserves_precision_missingness_and_provenance(self):
        r=self.adapter().normalize_yahoo(self.payload(), symbol='AMD', as_of_date='2026-11-14')
        self.assertEqual(r['bars'][0]['open'], '1.1234567')
        self.assertIsNone(r['bars'][0]['volume'])
        self.assertFalse(r['market_fact_authority'])
        self.assertEqual(r['adjustment_semantics'],'unverified_provider_observation')

    def test_rejects_identity_length_error_future_and_duplicate(self):
        a=self.adapter()
        for mutation in ['identity','length','error','duplicate','future','nan']:
            p=self.payload(); r=p['chart']['result'][0]
            if mutation=='identity': r['meta']['symbol']='SOXX'
            if mutation=='length': r['indicators']['quote'][0]['close']=[]
            if mutation=='error': p['chart']['error']={'code':'Not Found'}
            if mutation=='duplicate': r['timestamp']*=2; [v.extend(v) for v in r['indicators']['quote'][0].values()]
            if mutation=='future': r['timestamp']=[1894580200]
            if mutation=='nan': r['indicators']['quote'][0]['close']=[float('nan')]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): a.normalize_yahoo(p,symbol='AMD',as_of_date='2026-11-14')

    def test_sina_keeps_missing_null_and_rejects_invalid_payload(self):
        a=self.adapter(); r=a.normalize_sina('var([{"d":"2026-11-13","c":"1.234567"}]);',symbol='AMD',as_of_date='2026-11-14')
        self.assertEqual(r['bars'][0]['close'],'1.234567')
        self.assertIsNone(r['bars'][0]['open']); self.assertIsNone(r['bars'][0]['volume'])
        self.assertEqual(r['identity_verification'],'request_bound_only')
        for raw in ['garbage','var([]);','var([{"d":"2026-11-13","c":"NaN"}]);','var([{"d":"2026-11-13","c":0}]);']:
            with self.assertRaises(ValueError): a.normalize_sina(raw,symbol='AMD',as_of_date='2026-11-14')

    def test_large_integer_volume_not_rounded_and_bad_shapes_rejected(self):
        a=self.adapter(); p=self.payload()
        p['chart']['result'][0]['indicators']['quote'][0]['volume']=[9007199254740993]
        self.assertEqual(a.normalize_yahoo(p,symbol='AMD',as_of_date='2026-11-14')['bars'][0]['volume'],9007199254740993)
        for p in [{'chart': []}, {'chart': True}]:
            with self.assertRaises(ValueError): a.normalize_yahoo(p,symbol='AMD',as_of_date='2026-11-14')

    def test_observed_wrapper_exact_precision_and_untrusted_js(self):
        a=self.adapter()
        body='[{"d":"2026-11-13","c":"1.1234567890123456789"}]'
        prefix="/*<script>location.href='//sina.com';</script>*/"
        r=a.normalize_sina(prefix+'\nvar('+body+');',symbol='AMD',as_of_date='2026-11-16')
        self.assertEqual(r['bars'][0]['close'],'1.1234567890123456789')
        for raw in ['evil();var('+body+');', '/* arbitrary */var('+body+');',prefix+'var('+body+');evil();']:
            with self.assertRaises(ValueError): a.normalize_sina(raw,symbol='AMD',as_of_date='2026-11-16')

    def test_unknown_adjustment_denies_technical_use_and_flags_break(self):
        a=self.adapter()
        raw='[{"d":"2020-01-02","c":"100"},{"d":"2020-01-03","c":"25"}]'
        r=a.normalize_sina(raw,symbol='TEST',as_of_date='2020-01-04')
        self.assertFalse(r['technical_eligible'])
        self.assertEqual(r['quality_flags'][0]['trade_date'],'2020-01-03')
        self.assertEqual(r['quality_flags'][0]['reason'],'large_price_discontinuity_unexplained')
        with self.assertRaisesRegex(ValueError,'adjustment unverified'): a.require_technical_eligibility(r)
        r['technical_eligible']=True
        with self.assertRaises(ValueError): a.require_technical_eligibility(r)

    def test_decimal_and_input_boundaries(self):
        a=self.adapter()
        for c in ['1e1000','1e-1000','NaN','Infinity','-1','0',True]:
            import json
            with self.subTest(c=c), self.assertRaises(ValueError):
                a.normalize_sina(json.dumps([{'d':'2026-11-13','c':c}]),symbol='AMD',as_of_date='2026-11-16')
        for row in ['{"d":"2026-11-13","c":2,"h":1}', '{"d":"2026-11-13","c":1,"v":1.5}']:
            with self.assertRaises(ValueError): a.normalize_sina('['+row+']',symbol='AMD',as_of_date='2026-11-16')
        with self.assertRaises(ValueError): a.normalize_sina(' '*8000001,symbol='AMD',as_of_date='2026-11-16')

    def test_discontinuity_threshold_does_not_round_across_boundary(self):
        import json
        a=self.adapter()
        for value,flagged in [('0.59999999999999999999999999999',True),('0.6',False),('1.40000000000000000000000000001',True),('1.4',False)]:
            r=a.normalize_sina(json.dumps([{'d':'2026-11-12','c':'1'},{'d':'2026-11-13','c':value}]),symbol='AMD',as_of_date='2026-11-16')
            self.assertEqual(bool(r['quality_flags']),flagged,value)

if __name__=='__main__': unittest.main()
