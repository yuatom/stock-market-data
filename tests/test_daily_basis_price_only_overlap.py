"""Real local Git and existing fabricated fixture; no HTTP or production writes."""
import ast
import copy
from pathlib import Path
import tempfile
import types
import unittest

# Reuse only the existing fixture helpers without requiring its pytest runner.
# No production function or validation boundary is mocked.
p=Path(__file__).with_name('test_daily_basis_updates.py')
tree=ast.parse(p.read_text());names={'git','rows','request','response','fixture','setup','capture','synthetic_delivery'}
tree.body=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in names) or isinstance(n,(ast.Import,ast.ImportFrom)) and not (isinstance(n,ast.Import) and any(x.name=='pytest' for x in n.names))]
F=types.ModuleType('existing_daily_synthetic_fixture');exec(compile(tree,str(p),'exec'),F.__dict__)

class PriceOnlyOverlap(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.D,self.repo,self.g,self.cal,self.paths,self.store,self.now,self.u=F.setup(Path(self.tmp.name).resolve())
    def test_volume_only_is_literal_and_price_only(self):
        values=F.rows()[:5];values[0]['volume']='301';values[3]['volume']='131'
        with self.store.lock():
            c=self.u.run_slot('nov10','101-1',capture=F.capture(values))
            self.assertTrue(self.u.validate_candidate(c))
            snap=self.u._snapshot(c.selector);self.assertEqual(snap['records'],values)
            self.assertEqual(snap['volume_policy'],'BLOCKED_UNRESOLVED')
            d=F.synthetic_delivery(self.D,self.u,c,self.now[0]);self.u.promote(c,d)
            view=self.u.read_current(d,fields=('open','high','low','close'));self.assertTrue(all('volume' not in r for r in view['records']))
            with self.assertRaises(self.D.DailyBasisError):self.u.read_current(d,fields=('volume',))
            for path,raw in self.paths.items():
                if path!=F.B.catalog_path(F.B.PROVIDER,'SOXX'):self.assertEqual(self.store.read(self.store.head(),path),raw)
    def test_old_price_change_still_rejected_and_response_retained(self):
        values=F.rows()[:5];values[0].update(open='10.1',high='10.1',low='10.1',close='10.1',volume='301')
        with self.store.lock(),self.assertRaises(self.D.DailyBasisError):self.u.run_slot('nov10','101-1',capture=F.capture(values))
        ref=self.store.ref(self.u._attempt_ref(self.g['slots'][0]));prefix=self.u._prefix(self.g['slots'][0])
        self.assertTrue(self.store.exists(ref,prefix+'/response.raw'));self.assertTrue(self.store.exists(ref,prefix+'/envelope.json'))
        self.assertEqual(self.store.read(self.store.head(),F.B.catalog_path(F.B.PROVIDER,'SOXX')),self.paths[F.B.catalog_path(F.B.PROVIDER,'SOXX')])
    def test_metadata_mismatch_retains_raw_and_rejects(self):
        def capture(**kw):
            kw['before_request']();req=kw['request'];raw=F.response(dict(req,symbol='SPY'),F.rows()[:5]);kw['retain_raw'](raw)
            envelope=F.build_envelope(raw,req,self.now[0].isoformat());kw['retain_envelope'](envelope)
        with self.store.lock(),self.assertRaises(self.D.DailyBasisError):self.u.run_slot('nov10','101-1',capture=capture)
        ref=self.store.ref(self.u._attempt_ref(self.g['slots'][0]));self.assertTrue(self.store.exists(ref,self.u._prefix(self.g['slots'][0])+'/response.raw'))
    def test_quarantine_cannot_be_relaxed(self):
        old=self.u._snapshot(self.g['initial_selector']);new=copy.deepcopy(old);new['volume_policy']='ENABLED'
        with self.assertRaises(F.B.BasisError):self.D.require_price_only_overlap(old,new)
    def test_overlap_metadata_cannot_change(self):
        old=self.u._snapshot(self.g['initial_selector']);new=copy.deepcopy(old);new['request']['adjust']='none'
        with self.assertRaises(F.B.BasisError):self.D.require_price_only_overlap(old,new)
    def test_missing_date_still_rejected(self):
        with self.store.lock(),self.assertRaises(self.D.DailyBasisError):self.u.run_slot('nov10','101-1',capture=F.capture(F.rows()[:4]))

if __name__=='__main__':unittest.main()
