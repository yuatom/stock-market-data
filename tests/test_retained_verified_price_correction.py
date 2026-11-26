import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from retained_verified_price_correction import prepare, CorrectionError, row_sha256

class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.old=[dict(trade_date="2026-11-18",open=5,high=8,low=4,close=6,volume=15)]
        self.incoming=[dict(self.old[0], open=5.1, volume=17),
            dict(trade_date="2026-11-19",open=6,high=9,low=4,close=7,volume=17)]
        self.req=[dict(trade_date="2026-11-18",expected_old_row_sha256=row_sha256(self.old[0]),fields={"open":"5.10"})]
    def runrule(self):
        return prepare(self.old,self.incoming,self.req,max_rows=3,tail_sessions=3)
    def test_only_approved_field_and_retains_volume(self):
        rows,audit=self.runrule()
        self.assertEqual(rows, [dict(self.old[0],open=5.1)])
        self.assertEqual(self.old[0]["open"],5)
        self.assertEqual(audit[0]["changes"], {"open":{"old":5,"new":5.1}})
    def test_obsolete_old_digest(self):
        self.old[0]["volume"]=99
        with self.assertRaises(CorrectionError): self.runrule()
    def test_unverified_value(self):
        self.req[0]["fields"]["open"]="5.25"
        with self.assertRaises(CorrectionError): self.runrule()
    def test_volume_not_admitted(self):
        self.req[0]["fields"]["volume"]="17.00"
        with self.assertRaises(CorrectionError): self.runrule()
    def test_other_revised_price_not_silently_discarded(self):
        self.incoming[0]["high"]=8.25
        with self.assertRaises(CorrectionError): self.runrule()
    def test_invalid_ohlc(self):
        self.incoming[0]["open"]=50
        self.req[0]["fields"]["open"]="50.00"
        with self.assertRaises(CorrectionError): self.runrule()
    def test_duplicate_source_date(self):
        self.incoming.insert(0,copy.deepcopy(self.incoming[0]))
        with self.assertRaises(CorrectionError): self.runrule()
    def test_date_outside_retained_tail(self):
        for i in ("19","20","21"):
            row=dict(self.old[0],trade_date="2026-11-"+i)
            self.old.append(row);self.incoming=[self.incoming[0],*self.old[1:]]
        with self.assertRaises(CorrectionError): self.runrule()
    def test_nonoverlap_not_inserted_by_correction(self):
        self.req[0]["trade_date"]="2026-11-19"
        with self.assertRaises(CorrectionError): self.runrule()
if __name__=="__main__": unittest.main()
