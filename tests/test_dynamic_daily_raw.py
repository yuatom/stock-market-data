"""Offline synthetic exact-byte capture/strict parsing. No real network or provider."""
import copy,json,hashlib,subprocess,shutil,tempfile,unittest,os
from pathlib import Path
from unittest.mock import patch
import dynamic_daily_response as parser
import collect_dynamic_candidates as d
import test_dynamic_candidates as fixtures

ROW=dict(datetime='2026-11-13',open='10',high='12',low='9',close='11',volume='100')
def payload():return dict(status='ok',meta=dict(symbol='AAA',interval='1day'),values=[dict(ROW,datetime='2026-11-12'),dict(ROW)])
def encoded(doc):return json.dumps(doc).encode()

class ParserTests(unittest.TestCase):
    def test_complete_rows_sorted_and_true_zero_only(self):
        doc=payload();doc['values'].reverse();doc['values'][0]['volume']='0'
        rows=parser.parse(encoded(doc),symbol='AAA',outputsize=256)
        self.assertEqual([r['trade_date'] for r in rows],['2026-11-12','2026-11-13'])
        self.assertEqual(rows[-1]['volume'],0);self.assertEqual(len(rows),2)
    def test_every_bad_row_or_identity_rejects_whole_response(self):
        cases=[]
        for key,value in [('symbol','OTHER'),('interval','1min')]:
            doc=payload();doc['meta'][key]=value;cases.append(doc)
        for field,value in [('close','bad'),('close','NaN'),('open','Infinity'),('volume',''),('volume',None),('volume','-1'),('volume','1.5'),('high','8'),('datetime','2026-11-31')]:
            doc=payload();doc['values'][0][field]=value;cases.append(doc)
        doc=payload();del doc['values'][0]['volume'];cases.append(doc)
        doc=payload();doc['values'][0]['extra']='unknown';cases.append(doc)
        doc=payload();doc['values'][0]=dict(doc['values'][1]);cases.append(doc)
        doc=payload();doc['values']=[];cases.append(doc)
        doc=payload();doc['status']='error';cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc),self.assertRaises(ValueError):parser.parse(encoded(doc),symbol='AAA',outputsize=256)
        for raw in (b'{broken',b'{"status":"ok","status":"error"}',b'{"values":NaN}'):
            with self.assertRaises(ValueError):parser.parse(raw,symbol='AAA',outputsize=256)

class RawRetentionTests(unittest.TestCase):
    def run_response(self,raw):
        root=Path(self.temp.name)/'market';root.mkdir(exist_ok=True)
        request=fixtures.DynamicCandidateRequestTests()._request();request.update(trade_date='2026-11-14',candidate_symbols=['AAA','BBB'])
        with patch.dict(os.environ,{'TWELVE_DATA_API_KEY':'SYNTHETIC_NEVER_PERSIST'}),patch.object(d.collectors,'_http_raw',return_value=raw) as transport,patch.object(d.collectors,'_http_json',side_effect=AssertionError('legacy parser forbidden')),patch.object(d,'collect_regular_window') as intraday:
            result=d.collect_request(request=request,store_root=root,store_config_path=fixtures.ROOT/'config/market-data-store.yaml',access_path=fixtures.ROOT/'config/market-data-collector-access.yaml',dynamic_config_path=fixtures.ROOT/'config/dynamic-candidate-collection.yaml')
        return root,result,transport,intraday
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
    def test_bad_json_retained_exactly_before_parse_and_closed_by_real_verifier(self):
        raw=b'{malformed complete response\n';root,result,transport,intraday=self.run_response(raw)
        self.assertEqual(result['status'],'STOPPED');self.assertFalse(result['market_fact_authority']);transport.assert_called_once();intraday.assert_not_called()
        state=result['daily_history_execution'];self.assertEqual(state['stop_phase'],'strict_daily_parse');self.assertEqual(state['requests_attempted'],1);self.assertEqual(state['local_budget_debits'],0)
        self.assertFalse(state['remote_predebit_established']);self.assertEqual(state['provider_charge_status'],'UNKNOWN')
        retained=state['raw_responses'][0];self.assertEqual((root/retained['path']).read_bytes(),raw);self.assertEqual(retained['sha256'],hashlib.sha256(raw).hexdigest())
        manifest=json.loads((root/result['publication_manifest_path']).read_text());self.assertIn(retained['path'],[x['path'] for x in manifest['required_outputs']])
        self.assertNotIn('SYNTHETIC_NEVER_PERSIST',json.dumps(result));self.assertFalse(any('apikey=' in p.read_text(errors='ignore') for p in root.rglob('*') if p.is_file()))
        remote=Path(self.temp.name)/'remote.git';writer=Path(self.temp.name)/'writer'
        subprocess.run(['git','init','--bare','-q',str(remote)],check=True);subprocess.run(['git','init','-q',str(writer)],check=True)
        def git(*args):return subprocess.check_output(['git','-C',str(writer),*args],text=True,stderr=subprocess.DEVNULL).strip()
        git('config','user.name','test');git('config','user.email','test@example.invalid');git('remote','add','origin',str(remote));shutil.copytree(root,writer/'data/market-data');git('add','.');git('commit','-qm','stopped exact raw');git('branch','-M','main');git('push','-q','origin','main')
        verified=fixtures.durability.verify_publication(writer,git('rev-parse','HEAD'),required_manifest='data/market-data/'+result['publication_manifest_path'])
        self.assertEqual(verified['status'],'durability_verified')
        self.assertIn('data/market-data/'+retained['path'],[x['path'] for x in verified['verified_required_outputs']])
    def test_wrong_meta_bad_row_and_missing_volume_retain_no_market_write_or_retry(self):
        cases=[]
        doc=payload();doc['meta']['symbol']='OTHER';cases.append(doc)
        doc=payload();doc['meta']['interval']='1min';cases.append(doc)
        doc=payload();doc['values'][0]['close']='bad';cases.append(doc)
        doc=payload();del doc['values'][0]['volume'];cases.append(doc)
        for doc in cases:
            raw=encoded(doc);root,result,transport,intraday=self.run_response(raw)
            transport.assert_called_once();intraday.assert_not_called();self.assertEqual(result['status'],'STOPPED')
            self.assertEqual(result['daily_history_execution']['local_budget_debits'],0)
            self.assertFalse((root/'series').exists())
            record=result['daily_history_execution']['raw_responses'][0];self.assertEqual((root/record['path']).read_bytes(),raw)
            manifest=json.loads((root/result['publication_manifest_path']).read_text());self.assertIn(record['path'],[x['path'] for x in manifest['required_outputs']])

if __name__=='__main__':unittest.main()
