from datetime import datetime,timezone
from pathlib import Path
from unittest import TestCase
from scripts import retained_price_only_source_policy as P
from scripts import retained_price_only_slots as S

class Eligibility(TestCase):
    def setUp(self):
        self.raw=(Path(__file__).resolve().parents[1]/P.POLICY_PATH).read_bytes()
        self.web=S.parse(self.raw)
    def allowed(self,key,provider,origin):
        return dict(source_id=key,provider_identity=provider,role='admission_eligible',
            adapter='licensed_regular_daily_price_export_v1',origin=origin,license_sha256='a'*64,valid_from='2026-01-01T00:00:00Z',valid_until='2026-03-01T00:00:00Z')
    def test_web_is_corroboration_only_even_if_config_attempts_self_promotion(self):
        loaded=P.load_policy(self.raw)
        self.assertEqual({x['role'] for x in loaded.values()},{'corroboration_only'})
        with self.assertRaises(P.PolicyHold):
            P.select(loaded,{x['source_id']:x['origin'] for x in loaded.values()},now=datetime(2026,1,14,tzinfo=timezone.utc))
        self.web['profiles'][0]['role']='admission_eligible'
        with self.assertRaises(P.PolicyHold):P.load_policy(S.encode(self.web))
    def test_only_pinned_distinct_licensed_providers_can_be_selected(self):
        a=self.allowed('synthetic_a','fixture_a','https://a.example.invalid')
        b=self.allowed('synthetic_b','fixture_b','https://b.example.invalid')
        urls={'synthetic_a':'https://a.example.invalid/price','synthetic_b':'https://b.example.invalid/price'}
        def select(x,y,location=urls):
            return P.select(P.load_policy(S.encode(self.web|{'profiles':self.web['profiles']+[x,y]})),location,now=datetime(2026,1,14,tzinfo=timezone.utc))
        self.assertEqual(set(select(a,b)),set(urls))
        for x,y,location in [(a,b|{'provider_identity':'fixture_a'},urls),
                              (a,b|{'license_sha256':None},urls),
                              (a,b|{'valid_until':'2026-01-13T00:00:00Z'},urls),
                              (a,b|{'origin':'https://other.websol.barchart.com'},urls|{'synthetic_b':'https://other.websol.barchart.com/data'}),
                              (a,b,urls|{'synthetic_b':'https://other.example.invalid/price'}),
                              (a,b,{'unknown':urls['synthetic_a'],'synthetic_b':urls['synthetic_b']})]:
            with self.subTest(y=y,location=location),self.assertRaises(P.PolicyHold):select(x,y,location)
    def test_exact_offline_import_requires_identity_date_and_price_basis(self):
        a=self.allowed('synthetic_a','fixture_a','https://a.example.invalid')
        identity=dict(mic='XNMS',symbol='XQX',asset_class='etf',currency='USD',session='regular',
                      basis='split_adjusted_price_only',trade_date='2026-01-12')
        row=identity|dict(schema_version=1,kind='licensed_regular_daily_price_export_v1',
                          provider_identity='fixture_a',scheduled_close_et='16:00',
                          ohlc=dict(open='10.05',high='12.18',low='9.22',close='11.11'))
        self.assertEqual(P.import_prices(a,S.encode(row),expected=identity,scheduled_close='16:00'),row['ohlc'])
        for change in ({'basis':'total_return'},{'symbol':'AAA'},{'trade_date':'2026-01-16'},
                       {'kind':'net_asset_value'},{'session':'after_hours'},
                       {'ohlc':row['ohlc']|{'after_hours_close':'11.12'}},
                       {'provider_identity':'other'},{'volume':0},{'scheduled_close_et':'13:00'}):
            with self.subTest(change=change),self.assertRaises(P.PolicyHold):
                P.import_prices(a,S.encode(row|change),expected=identity,scheduled_close='16:00')
