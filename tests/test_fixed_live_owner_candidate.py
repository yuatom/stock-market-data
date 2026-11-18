"""Real existing publisher with local bare Git; never a provider request."""
import unittest,time,json
from dynamic_daily_owner import DynamicPublicationUnknown
from fixed_live_owner_candidate import FixedLiveOwner
import test_fixed_intraday_offline as fixture
import admit_market_data_baseline as base
import market_data_collectors as collectors

class FixedLiveOwnerTests(unittest.TestCase):
    setUp=fixture.FixedIntradayOfflineTest.setUp
    ledger=fixture.FixedIntradayOfflineTest.ledger
    def make_owner(self,stage='open_15m',trigger='schedule',symbols=None):
        return FixedLiveOwner(self.repo,self.start,fixture.fixture.DAY,'123-1',compute=self.compute,
            compute_sha=self.sha,stage=stage,symbols=symbols or ['AAA','BBB'],
            invocation=dict(trigger=trigger,request_id=None,transaction_id=None,control_ref=None,request_blob=None),
            clock=lambda:self.now,deadline=time.monotonic()+120)
    def test_real_existing_store_publisher_persists_debit_before_provider(self):
        self.owner.begin_intraday(fixture.fixture.DAY,'open_15m','09:30','09:45')
        config=collectors.load_yaml(fixture.fixture.oldtests.ROOT/'config/market-data-store.yaml')
        self.owner.reserve('AAA',base.load_helpers(),config['collector']['budgets']['twelve_data_basic'])
        self.assertEqual(self.ledger()['credits'],1)
        claim=json.loads((self.repo/self.owner.active['claim']).read_bytes())
        self.assertIsNone(claim['business_request_id'])
        self.assertFalse(self.owner.active['called'])
        self.owner.before_request()
        self.assertTrue(self.owner.active['called'])
        with self.assertRaises(DynamicPublicationUnknown):self.owner.abort()
        self.assertEqual(self.ledger()['credits'],1)
    def test_schedule_and_manual_not_rejected_as_push_only(self):
        for trigger in ('schedule','workflow_dispatch','push'):
            owner=self.make_owner(trigger=trigger)
            self.assertIsNone(owner.request['transaction_id'])
            self.assertEqual(owner.request['request_id'],'123-1')
    def test_close_stage_accepts_existing_window_and_selected_symbols(self):
        owner=self.make_owner(stage='close',symbols=['BBB'])
        owner.begin_intraday(fixture.fixture.DAY,'close','15:45','16:00')
        self.assertEqual(owner.request['candidate_symbols'],['BBB'])
    def test_workflow_factory_uses_selected_scope_and_existing_identity(self):
        from fixed_live_owner_candidate import WorkflowFixedOwnerFactory
        factory=WorkflowFixedOwnerFactory(repo=self.repo,compute=self.compute,compute_sha=self.sha,
            run_id='123-1',invocation=dict(trigger='schedule',request_id=None,transaction_id=None,control_ref=None,request_blob=None),
            clock=lambda:self.now,deadline=time.monotonic()+120)
        owner=factory(mode='close_retry',stage='close',trade_date=fixture.fixture.DAY,start_et='15:45',end_et='16:00',store_root=self.store,symbols=['BBB'])
        self.assertIs(owner,factory.latest_owner)
        self.assertEqual(owner.request['candidate_symbols'],['BBB'])
        self.assertEqual(owner.request['logical_trigger'],'schedule')
        self.assertIsNone(owner.request['transaction_id'])
