import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_retained_xlc_quarantine as fixture_module
import build_retained_xlc_price_candidate as q

class PriceCandidateTests(unittest.TestCase):
    def fixture(self):
        report,old=fixture_module.RetainedXlcTests().fixture()
        report['retained_normalized_response']['records'][-1]=copy.deepcopy(q.NEW_ROW)
        return report,old
    def run_candidate(self,report,old):
        raw=q.store.canonical_bytes(report)
        with patch.object(q.source,'SOURCE_BLOB',q.store.git_blob_sha_bytes(raw)),patch.object(q.source,'SOURCE_HASH',hashlib.sha256(raw).hexdigest()):return q.candidate_rows(old,raw)
    def test_high_only_with_provider_new_date(self):
        report,old=self.fixture();rows,diffs=self.run_candidate(report,old)
        self.assertEqual(rows[:317],old[:317]);self.assertEqual(rows[317],dict(old[317],high=108.97));self.assertEqual(rows[318],q.NEW_ROW);self.assertEqual(diffs[0]['changed_fields'],{'high':{'old':108.9681,'new':108.97}})
    def test_old_volume_stays_old_not_provider(self):
        report,old=self.fixture();rows,_=self.run_candidate(report,old);self.assertEqual(rows[317]['volume'],5557979.);self.assertNotEqual(rows[317]['volume'],report['retained_normalized_response']['records'][-2]['volume'])
    def test_wrong_source_bytes_rejected(self):
        report,old=self.fixture()
        with self.assertRaises(ValueError):q.candidate_rows(old,q.store.canonical_bytes(report))
    def test_other_old_change_rejected(self):
        report,old=self.fixture();report['retained_normalized_response']['records'][-3]['high']=109.
        with self.assertRaises(ValueError):self.run_candidate(report,old)
    def test_new_row_change_rejected(self):
        report,old=self.fixture();report['retained_normalized_response']['records'][-1]['volume']=1.
        with self.assertRaises(ValueError):self.run_candidate(report,old)
    def test_missing_new_date_rejected(self):
        report,old=self.fixture();report['retained_normalized_response']['records'][-1]['trade_date']='2026-11-13'
        with self.assertRaises(ValueError):self.run_candidate(report,old)
    def test_baseline_volume_drift_rejected(self):
        report,old=self.fixture();old[-1]['volume']=5558000.
        with self.assertRaises(ValueError):self.run_candidate(report,old)
    def test_existing_destination_rejected_before_git(self):
        with tempfile.TemporaryDirectory() as td,patch.object(q.source,'git_bytes') as g,self.assertRaises(ValueError):q.build('/missing',td)
        g.assert_not_called()
    def test_no_publication_surface(self):
        code=Path(q.__file__).read_text();self.assertNotIn("'push'",code);self.assertNotIn('verify_publication(',code);self.assertNotIn('_fetch_replacement',code)

if __name__=='__main__':unittest.main()
