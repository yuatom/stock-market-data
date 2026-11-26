from pathlib import Path
import sys, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from normal_daily_missing_repair import fill_missing_regular_daily_fields, MissingRepairError

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.old={'trade_date':'2026-11-19','open':10,'high':12,'low':9,'close':None}
        self.fresh={'trade_date':'2026-11-19','open':10.1,'high':12,'low':9,'close':11,'volume':12}
    def test_existing_conflict_does_not_stop_safe_missing_price(self):
        r,a=fill_missing_regular_daily_fields(self.old,self.fresh)
        self.assertEqual(r['open'],10)
        self.assertEqual(r['close'],11)
        self.assertEqual(a['filled'],['close'])
        self.assertEqual(a['preserved_conflicts'],['open'])
    def test_volume_not_assumed_available(self):
        r,a=fill_missing_regular_daily_fields(self.old,self.fresh)
        self.assertNotIn('volume',r)
    def test_explicitly_qualified_volume_can_fill_zero(self):
        self.fresh['volume']=0
        r,_=fill_missing_regular_daily_fields(self.old,self.fresh,allow_verified_volume=True)
        self.assertEqual(r['volume'],0)
    def test_volume_zero_is_present(self):
        self.old['volume']=0
        r,a=fill_missing_regular_daily_fields(self.old,self.fresh,allow_verified_volume=True)
        self.assertEqual(r['volume'],0)
        self.assertIn('volume',a['preserved_conflicts'])
    def test_mixing_must_not_break_ohlc(self):
        self.fresh.update(close=15,high=16)
        with self.assertRaises(MissingRepairError):fill_missing_regular_daily_fields(self.old,self.fresh)
    def test_non_null_invalid_is_not_missing(self):
        self.old['open']=0
        with self.assertRaises(MissingRepairError):fill_missing_regular_daily_fields(self.old,self.fresh)
    def test_different_date(self):
        self.fresh['trade_date']='2026-11-18'
        with self.assertRaises(MissingRepairError):fill_missing_regular_daily_fields(self.old,self.fresh)
    def test_malformed_missing_input(self):
        self.fresh['close']=None
        with self.assertRaises(MissingRepairError):fill_missing_regular_daily_fields(self.old,self.fresh)
    def test_complete_old_value_never_replaced(self):
        self.old['close']=10.25
        r,a=fill_missing_regular_daily_fields(self.old,self.fresh)
        self.assertEqual(r,self.old)
        self.assertEqual(a['filled'],[])
        self.assertIn('close',a['preserved_conflicts'])
