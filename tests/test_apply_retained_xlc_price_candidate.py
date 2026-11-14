from pathlib import Path
import unittest
from unittest.mock import patch
import apply_retained_xlc_price_candidate as q

class ApplyTests(unittest.TestCase):
    def env(self):return dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY='yuatom/stock-market-data',GITHUB_REF='refs/heads/main',GITHUB_SHA='a'*40,GITHUB_WORKFLOW='Retained XLC High Correction',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='123')
    def git(self,fail_push=False,drift=False):
        def fake(repo,*args):
            compute=str(repo).endswith('/compute')
            if args==('rev-parse','HEAD'):return 'a'*40 if compute else 'b'*40
            if args==('rev-parse','HEAD^'):return q.BASE
            if args==('remote','get-url','origin'):return 'https://github.com/yuatom/'+('stock-market-data' if compute else 'stock-market-data-store')
            if args==('ls-remote','origin','refs/heads/main'):return ('a'*40 if compute else ('c'*40 if drift else q.BASE))+'\trefs/heads/main'
            if args and args[0]=='push' and fail_push:raise TimeoutError()
            return ''
        return fake
    def test_unknown_push_stops(self):
        with patch.dict(q.os.environ,self.env(),clear=True),patch.object(q,'stage',return_value={'x'}),patch.object(q,'git',side_effect=self.git(True)) as g,patch.object(q,'verify_publication') as v,self.assertRaises(TimeoutError):q.execute('/store','/compute','a'*40,'123-1')
        self.assertEqual(sum(c.args[1]=='push' for c in g.call_args_list if len(c.args)>1),1);v.assert_not_called()
    def test_unknown_durability_stops(self):
        with patch.dict(q.os.environ,self.env(),clear=True),patch.object(q,'stage',return_value={'x'}),patch.object(q,'git',side_effect=self.git()) as g,patch.object(q,'verify_publication',side_effect=TimeoutError()),self.assertRaises(TimeoutError):q.execute('/store','/compute','a'*40,'123-1')
        self.assertEqual(sum(c.args[1]=='push' for c in g.call_args_list if len(c.args)>1),1)
    def test_wrongbase_stops_before_stage(self):
        with patch.dict(q.os.environ,self.env(),clear=True),patch.object(q,'stage') as s,patch.object(q,'git',side_effect=self.git(drift=True)),self.assertRaises(ValueError):q.execute('/store','/compute','a'*40,'123-1')
        s.assert_not_called()
    def test_attempt_two_rejected(self):
        env=self.env();env['GITHUB_RUN_ATTEMPT']='2'
        with patch.dict(q.os.environ,env,clear=True),patch.object(q,'git') as g,self.assertRaises(ValueError):q.execute('/store','/compute','a'*40,'123-2')
        g.assert_not_called()
    def test_no_workflow_surface_rejected(self):
        with patch.dict(q.os.environ,{},clear=True),self.assertRaises(ValueError):q.execute('/store','/compute','a'*40,'123-1')
    def test_workflow_free_no_provider(self):
        p=Path(q.__file__).resolve().parents[1]/'.github/workflows/retained-xlc-high-correction.yml';s=p.read_text();self.assertIn('group: market-data-collector',s);self.assertIn(q.BASE,s);self.assertNotIn('TWELVE_DATA',s)
    def test_single_push_no_provider(self):
        s=Path(q.__file__).read_text();self.assertEqual(s.count("git(repo,'push'"),1);self.assertNotIn('_fetch_replacement',s)

if __name__=='__main__':unittest.main()
