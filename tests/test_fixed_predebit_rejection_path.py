"""Actual fixed CLI, owner and local publisher for known pre-debit refusals."""
import json,unittest
from unittest.mock import patch
import test_fixed_known_rejection_path as known
from test_dynamic_daily_owner_integration import git,DAY
from dynamic_daily_owner import DynamicPublicationUnknown
import admit_market_data_baseline as admission

class PredebitRejectionTests(known.KnownRejectionPathTests):
    def seed_budget(self):
        self.budget=self.store/f'collector-state/2026-11/{DAY}-twelve-data-budget.json'
        self.budget.parent.mkdir(parents=True,exist_ok=True)
        self.budget.write_text(json.dumps(dict(schema_version=1,trade_date=DAY,credits=200,minutes={})))
        git(self.repo,'add','.');git(self.repo,'commit','-qm','synthetic exhausted budget');git(self.repo,'push','origin','HEAD:main')
        return self.budget.read_bytes()
    def assert_partial_no_new_attempts(self):
        owner=self.factory.latest_owner
        self.assertFalse(owner.failed);self.assertIsNone(owner.active)
        pointer=json.loads((self.store/f'snapshots/2026-11/{DAY}/open_15m/latest.json').read_bytes())
        snap=json.loads((self.store/pointer['snapshot_path']).read_bytes())
        self.assertEqual(sorted(snap['coverage']['stage_snapshot']['partial_symbols']),['AAA','BBB'])
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertFalse(any('fixed-intraday-attempt-' in p or p.endswith('.raw') for p in paths))
        self.assertIsNotNone(self.publisher.last_manifest)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
    def test_exhausted_budget_preserves_sparse_primary_without_http(self):
        before=self.seed_budget();self.setup_path()
        def forbidden(*a,**kw):self.fail('HTTP must not be entered')
        self.assertEqual(self.run_path(forbidden),0)
        self.assertEqual(self.budget.read_bytes(),before);self.assert_partial_no_new_attempts()
    def test_expired_deadline_preserves_sparse_primary_without_http(self):
        self.setup_path();self.factory.deadline=0
        def forbidden(*a,**kw):self.fail('HTTP must not be entered')
        self.assertEqual(self.run_path(forbidden),0);self.assert_partial_no_new_attempts()
    def test_object_drift_is_not_a_known_predebit_refusal(self):
        self.setup_path()
        with patch.object(admission,'debit_before_request',side_effect=admission.AdmissionError('object_drift')):
            with self.assertRaises(DynamicPublicationUnknown):self.run_path()
        self.assertTrue(self.factory.latest_owner.failed);self.assertIsNone(self.publisher.last_manifest)
    def test_unpublished_local_debit_cannot_be_reclassified(self):
        self.setup_path()
        def mutate(*args,**kw):
            writer=args[1];p=writer.safe_path(writer.budget_relative)
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(json.dumps(dict(schema_version=1,trade_date=DAY,credits=1,minutes={})))
            raise admission.AdmissionError('deadline_exceeded')
        with patch.object(admission,'debit_before_request',side_effect=mutate):
            with self.assertRaises(DynamicPublicationUnknown):self.run_path()
        self.assertTrue(self.factory.latest_owner.failed);self.assertIsNone(self.publisher.last_manifest)
