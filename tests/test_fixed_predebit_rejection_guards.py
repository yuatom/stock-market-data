"""Known error code alone must not hide a changed or already-published state."""
import unittest
from unittest.mock import patch
import test_fixed_known_rejection_path as known
from test_dynamic_daily_owner_integration import git
from dynamic_daily_owner import DynamicPublicationUnknown
import admit_market_data_baseline as admission

class PredebitGuardTests(unittest.TestCase):
    setUp=known.KnownRejectionPathTests.setUp
    make_owner=known.KnownRejectionPathTests.make_owner
    setup_path=known.KnownRejectionPathTests.setup_path
    primary=known.KnownRejectionPathTests.primary
    synthetic_response=known.KnownRejectionPathTests.synthetic_response
    run_path=known.KnownRejectionPathTests.run_path
    def test_known_code_after_real_head_drift_stops(self):
        self.setup_path()
        def drift(*args,**kwargs):
            git(self.repo,'commit','--allow-empty','-qm','synthetic unexpected HEAD advance')
            raise admission.AdmissionError('deadline_exceeded')
        with patch.object(admission,'debit_before_request',side_effect=drift):
            with self.assertRaises(DynamicPublicationUnknown):self.run_path()
        self.assertTrue(self.factory.latest_owner.failed)
        self.assertIsNone(self.publisher.last_manifest)
        self.assertEqual(self.symbols,[])
    def test_known_code_after_real_reserve_publication_stops(self):
        self.setup_path();original=admission.debit_before_request
        def published(*args,**kwargs):
            original(*args,**kwargs)
            raise admission.AdmissionError('deadline_exceeded')
        with patch.object(admission,'debit_before_request',side_effect=published):
            with self.assertRaises(DynamicPublicationUnknown):self.run_path()
        owner=self.factory.latest_owner
        self.assertTrue(owner.failed);self.assertTrue(owner.predebit_publish_started)
        self.assertIsNotNone(owner.active)
        self.assertIsNone(self.publisher.last_manifest)
        self.assertEqual(self.symbols,[])
        paths=git(self.remote,'ls-tree','-r','--name-only','refs/heads/main').splitlines()
        self.assertEqual(sum('fixed-intraday-attempt-' in p for p in paths),1)
        self.assertFalse(any(p.endswith('.raw') or p.endswith('-terminal.json') for p in paths))
