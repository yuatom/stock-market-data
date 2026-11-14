import copy
import datetime
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import quarantine_retained_xlc_volume as q

class RetainedXlcTests(unittest.TestCase):
    def fixture(self):
        end=datetime.date(2026,11,12)
        rows=[dict(trade_date=(end-datetime.timedelta(days=462-i)).isoformat(),open=108.029999,high=108.97,low=107.9,close=108.6,volume=5558000.) for i in range(463)]
        old=copy.deepcopy(rows[-319:-1]);old[-1].update(high=108.9681,volume=5557979.)
        report=dict(status='RESPONSE_RETAINED_UNQUALIFIED',request_id='43594816095-1',compute_sha='65abc29a193f9944d032bb888d018547f84e9948',requests_attempted=1,budget_debits=1,retained_normalized_response=dict(symbol='XLC',authority='normalized_not_raw_http',records=rows))
        return report,old
    def validate(self,report,old):
        raw=q.store.canonical_bytes(report)
        with patch.object(q,'SOURCE_BLOB',q.store.git_blob_sha_bytes(raw)),patch.object(q,'SOURCE_HASH',hashlib.sha256(raw).hexdigest()):
            return q.validated_evidence(raw,old)
    def test_exact_conflict(self):
        report,old=self.fixture();digest,raw=self.validate(report,old);j=json.loads(raw);self.assertEqual(j['symbol'],'XLC');self.assertEqual(j['volume_policy'],'BLOCKED_UNRESOLVED')
    def test_wrong_symbol(self):
        report,old=self.fixture();report['retained_normalized_response']['symbol']='AMD'
        with self.assertRaises(ValueError):self.validate(report,old)
    def test_wrong_source_hash(self):
        report,old=self.fixture()
        with self.assertRaises(ValueError):q.validated_evidence(q.store.canonical_bytes(report),old)
    def test_wrong_source_identity(self):
        report,old=self.fixture();report['request_id']='123-1'
        with self.assertRaises(ValueError):self.validate(report,old)
    def test_other_price_change(self):
        report,old=self.fixture();report['retained_normalized_response']['records'][-3]['close']=108.7
        with self.assertRaises(ValueError):self.validate(report,old)
    def test_drifted_baseline(self):
        report,old=self.fixture();old[-1]['volume']=5557978.
        with self.assertRaises(ValueError):self.validate(report,old)
    def test_surface_requires_actions(self):
        with patch.dict(q.os.environ,{},clear=True),self.assertRaises(ValueError):q.execute('/none','/none','a'*40,'123-1')
    def execution_mock(self, fail_push=False):
        candidate='b'*40;compute='a'*40
        def fake(repo,*args):
            is_compute=str(repo).endswith('/compute')
            if args==('rev-parse','HEAD'):return compute if is_compute else candidate
            if args==('remote','get-url','origin'):return 'https://github.com/yuatom/'+('stock-market-data' if is_compute else 'stock-market-data-store')
            if args==('ls-remote','origin','refs/heads/main'):return (compute if is_compute else q.BASE)+'\trefs/heads/main'
            if args==('rev-parse','HEAD^'):return q.BASE
            if args and args[0]=='push' and fail_push:raise TimeoutError()
            return ''
        return fake
    def test_unknown_publication_does_not_retry(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY='yuatom/stock-market-data',GITHUB_REF='refs/heads/main',GITHUB_SHA='a'*40,GITHUB_WORKFLOW='Retained XLC Volume Quarantine',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='123')
        with patch.dict(q.os.environ,env,clear=True),patch.object(q,'prepare',return_value={'x'}),patch.object(q,'git',side_effect=self.execution_mock()) as g,patch.object(q,'verify_publication',side_effect=TimeoutError()),self.assertRaises(TimeoutError):q.execute('/store','/compute','a'*40,'123-1')
        self.assertEqual(sum(x.args[1]=='push' for x in g.call_args_list if len(x.args)>1),1)
    def test_unknown_push_does_not_retry_or_verify(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY='yuatom/stock-market-data',GITHUB_REF='refs/heads/main',GITHUB_SHA='a'*40,GITHUB_WORKFLOW='Retained XLC Volume Quarantine',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='123')
        with patch.dict(q.os.environ,env,clear=True),patch.object(q,'prepare',return_value={'x'}),patch.object(q,'git',side_effect=self.execution_mock(True)) as g,patch.object(q,'verify_publication') as v,self.assertRaises(TimeoutError):q.execute('/store','/compute','a'*40,'123-1')
        self.assertEqual(sum(x.args[1]=='push' for x in g.call_args_list if len(x.args)>1),1);v.assert_not_called()
    def test_workflow_has_no_provider_secret(self):
        root=Path(__file__).resolve().parents[1];s=(root/'.github/workflows/retained-xlc-volume-quarantine.yml').read_text();self.assertNotIn('TWELVE_DATA',s);self.assertIn('group: market-data-collector',s);self.assertIn(q.BASE,s)
    def test_source_has_one_push_no_fetch_route(self):
        source=Path(q.__file__).read_text();self.assertEqual(source.count("git(repo,'push'"),1);self.assertNotIn('_fetch_replacement',source);self.assertNotIn('backfill(',source)

if __name__=='__main__':unittest.main()
