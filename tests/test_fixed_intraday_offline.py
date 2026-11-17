"""Real local bare Git publication, synthetic HTTP only; no production authority."""
import json, os, time, unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import test_dynamic_daily_owner_integration as fixture
from test_dynamic_daily_owner_integration import git
import market_data_collection as c
import market_data_collectors as b
from dynamic_daily_owner import DynamicPublicationUnknown
from fixed_intraday_offline import OfflineFixedIntradayOwner

class FixedIntradayOfflineTest(unittest.TestCase):
    setUp = fixture.DynamicOwnerIntegration.setUp
    ledger = fixture.DynamicOwnerIntegration.ledger
    def make_owner(self, run='123-1', trigger='push', stage='open_15m'):
        return OfflineFixedIntradayOwner(self.repo, git(self.repo,'rev-parse','HEAD'), fixture.DAY, run,
            compute=self.compute, compute_sha=self.sha, business_request_id='collector-fixed-synthetic-1',
            transaction_id='open15-synthetic-1', stage=stage, trigger=trigger,
            fixed_symbols=['AAA','BBB'], clock=lambda:self.now, deadline=time.monotonic()+120)
    def responses(self):
        rows=[dict(datetime=f'2026-11-14 09:{30+i:02d}:00',open='10',high='12',low='9',close='11',volume='100') for i in range(15)]
        return {s:json.dumps(dict(meta=dict(symbol=s,interval='1min'),values=rows)).encode() for s in ['AAA','BBB']}
    def collect(self,responses=None):
        return self.owner.simulate(self.responses() if responses is None else responses,
            config=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-store.yaml'))
    def paths(self):
        return git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
    def test_fixed_attempts_close_with_real_local_durability_no_provider_mock(self):
        result=self.collect()
        self.assertEqual(self.ledger()['credits'],2)
        self.assertEqual(result['attempt_count'],2)
        self.assertFalse(result['snapshot_written'])
        self.assertEqual(sum(p.endswith('-terminal.json') for p in self.paths()),2)
    def test_unknown_transport_keeps_one_durable_reservation_and_stops(self):
        with self.assertRaises(DynamicPublicationUnknown):self.collect({'AAA':None,'BBB':None})
        self.assertEqual(self.ledger()['credits'],1)
        self.assertEqual(sum('fixed-intraday-attempt-' in p for p in self.paths()),1)
        self.assertFalse(any(p.endswith('-terminal.json') for p in self.paths()))
    def test_unknown_durability_retains_reservation_without_raw(self):
        with patch.object(self.owner,'verify',side_effect=RuntimeError('uncertain')),self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.ledger()['credits'],1)
        self.assertFalse(any(p.endswith('.raw') for p in self.paths()))
    def test_unknown_claim_cannot_repeat_with_new_run_id(self):
        with self.assertRaises(DynamicPublicationUnknown):self.collect({'AAA':None,'BBB':None})
        self.owner=self.make_owner('456-1')
        with self.assertRaises(DynamicPublicationUnknown):self.collect()
        self.assertEqual(self.ledger()['credits'],1)
    def test_schedule_and_retry_identity_undecided_fail_closed(self):
        for trigger,stage in [('schedule','open_15m'),('push','close_retry'),('workflow_dispatch','open_15m')]:
            with self.subTest(trigger=trigger,stage=stage),self.assertRaises(ValueError):self.make_owner(trigger=trigger,stage=stage)
    def test_stage_or_window_mismatch_rejected_before_reservation(self):
        with self.assertRaises(DynamicPublicationUnknown):self.owner._begin_simulation(fixture.DAY,'open_30m','09:30','10:00')
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_remote_url_refused_without_network_or_publication(self):
        git(self.compute,'remote','set-url','origin','https://github.com/example/not-a-real-repo')
        with self.assertRaisesRegex(ValueError,'local bare'):self.make_owner()
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_known_rejection_retained_and_closed_before_second_symbol(self):
        responses=self.responses();responses['AAA']=b'{"status":"error","message":"synthetic"}'
        from dynamic_intraday_response import KnownIntradayRejection
        with self.assertRaises(KnownIntradayRejection):self.collect(responses)
        self.assertEqual(self.ledger()['credits'],1)
        self.assertEqual(sum(p.endswith('.raw') for p in self.paths()),1)
        self.assertEqual(sum(p.endswith('-terminal.json') for p in self.paths()),1)
    def test_same_checkout_stale_owner_refused_not_a_concurrency_claim(self):
        other=self.make_owner('456-1')
        with self.assertRaises(DynamicPublicationUnknown):self.collect({'AAA':None,'BBB':None})
        with self.assertRaises(DynamicPublicationUnknown):other._begin_simulation(fixture.DAY,'open_15m','09:30','09:45')
        self.assertEqual(self.ledger()['credits'],1)
    def test_expired_minute_after_debit_has_no_raw(self):
        from datetime import timedelta
        original=self.owner.verify
        def expiry(*args,**kw):
            result=original(*args,**kw);self.now=fixture.NOW+timedelta(minutes=1);return result
        with patch.object(self.owner,'verify',side_effect=expiry),self.assertRaises(Exception):self.collect()
        self.assertEqual(self.ledger()['credits'],1)
        self.assertFalse(any(p.endswith('.raw') for p in self.paths()))
    def test_callable_transport_not_accepted(self):
        with self.assertRaises(ValueError):self.collect({'AAA':lambda:None,'BBB':None})
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_direct_guarded_transport_callback_refused(self):
        with self.assertRaisesRegex(ValueError,'forbids provider'):self.owner.before_request()
    def test_pushurl_changed_after_construction_refused(self):
        git(self.repo,'config','remote.origin.pushurl','https://github.com/example/not-real')
        with self.assertRaises(Exception):self.collect()
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/main'),self.start)
    def test_network_pushurl_rejected_at_construction(self):
        git(self.repo,'config','remote.origin.pushurl','https://github.com/example/not-real')
        with self.assertRaisesRegex(ValueError,'local bare'):self.make_owner()
    def test_shared_collector_refused_before_any_provider_call(self):
        with patch.object(c,'fetch_nasdaq_regular') as nasdaq,patch.object(b,'_http_raw') as twelve:
            with self.assertRaisesRegex(ValueError,'simulate'):
                c.collect_regular_window(mode='open_15m',stage=None,trade_date=fixture.DAY,start_et='09:30',end_et='09:45',store_root=self.store,universe_config={},config=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-store.yaml'),access=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-collector-access.yaml'),eligible_universe=[('AAA','stocks')],durable_owner=self.owner)
            nasdaq.assert_not_called();twelve.assert_not_called()
    def test_multiple_push_destinations_rejected(self):
        git(self.repo,'config','--add','remote.origin.pushurl',str(self.remote))
        git(self.repo,'config','--add','remote.origin.pushurl','https://github.com/example/not-real')
        with self.assertRaisesRegex(ValueError,'local bare'):self.make_owner()
    def test_live_guarded_entry_cannot_open_provider_socket(self):
        from dynamic_intraday_response import fetch_guarded
        import urllib.request
        self.owner._begin_simulation(fixture.DAY,'open_15m','09:30','09:45')
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC'}),patch.object(urllib.request.OpenerDirector,'open',side_effect=AssertionError('provider forbidden')) as opened:
            with self.assertRaises(DynamicPublicationUnknown):
                fetch_guarded('AAA','stocks',fixture.DAY,'09:30','09:45',store_root=self.store,
                    config=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-store.yaml'),
                    access=b.load_yaml(fixture.oldtests.ROOT/'config/market-data-collector-access.yaml'),writer=self.owner)
            opened.assert_not_called()
