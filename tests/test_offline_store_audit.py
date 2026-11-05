import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import market_data_store as store
import market_data_collectors as collectors

class OfflineAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'store'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['/usr/bin/git','-C',str(self.repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','empty snapshot'],check=True)
        self.empty_sha=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse','HEAD'],text=True).strip()
        store.append_daily_bars(self.repo / 'data/market-data', provider='twelve_data_basic', symbol='AMD', asset_class='stocks', series_semantics='daily_regular_ohlcv', bars=[{'trade_date':'2026-11-03','open':1,'high':2,'low':1,'close':2,'volume':5}])
        self.commit(); self.old_sha = self.sha
        inv='fixture-run'; token=hashlib.sha256(inv.encode()).hexdigest()[:16]
        self.conflict=f'data/market-data/collector-state/2026-11/2026-11-04-previous_session_eod-{token}-conflicts.json'
        path=self.repo / self.conflict; path.parent.mkdir(parents=True)
        old={'trade_date':'2026-11-03','open':1,'high':2,'low':1,'close':2,'volume':5}
        new=dict(old,close=3)
        record=collectors._conflict_record(store_root=self.repo/'data/market-data',symbol='AMD',old=old,new=new,outputsize=10,effective_date=None,trade_date='2026-11-04',access={'twelve_data_basic':{'base_url':'https://example.invalid','time_series_path':'/time_series','interval_daily':'1day'}})
        self.diagnostic={'schema_version':1,'diagnostic_authority':'non_authoritative_conflict_evidence','trade_date':'2026-11-04','invocation':{'mode':'previous_session_eod','invocation_id':inv},'compute_sha':'a'*40,'store_input_sha':self.old_sha,'failure_target_count':1,'conflict_record_count':1,'conflict_diagnostic_record_count':1,'conflict_diagnostics_truncated':False,'conflicts':[record]}
        path.write_text(json.dumps(self.diagnostic))
        self.commit()
    def commit(self):
        subprocess.run(['/usr/bin/git','-C',str(self.repo),'add','.'],check=True)
        subprocess.run(['/usr/bin/git','-C',str(self.repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
        self.sha=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse','HEAD'],text=True).strip()
    def test_local_audit_positive_and_wrong_shard(self):
        import offline_store_audit as audit
        out=audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        self.assertEqual(out['series'][0]['last_trade_date'],'2026-11-03')
        shard=next((self.repo/'data/market-data/series/daily/twelve_data_basic/AMD').glob('[0-9]*.json'))
        obj=json.loads(shard.read_text()); obj['series_id']='wrong'; shard.write_text(json.dumps(obj))
        self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
    def test_wrong_conflict_or_missing_commit_rejected(self):
        import offline_store_audit as audit
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,'0'*40,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-03','2026-11-03',['AMD'],self.conflict)
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict,'0'*40)
    def test_conflict_tampering_and_wrong_old_snapshot_rejected(self):
        import copy
        import offline_store_audit as audit
        for change in ('digest','old_value','old_type','row_hash','new_row_hash','index_blob','shard_blob','row_number','missing_snapshot','wrong_snapshot'):
            data=copy.deepcopy(self.diagnostic); row=data['conflicts'][0]
            if change=='digest': row['fields'][0]['new_sha256']='0'*64
            if change=='old_value': row['fields'][0]['old_value']=999
            if change=='old_type': row['fields'][0]['old_type']='float'
            if change=='row_hash': row['old_row_sha256']='0'*64
            if change=='new_row_hash': row['new_row_sha256']='0'*64
            if change=='index_blob': row['store_source']['index_blob_sha']=None
            if change=='shard_blob': row['store_source']['shard']['blob_sha']='0'*40
            if change=='row_number': row['store_source']['row_number']=7
            if change=='missing_snapshot': data['store_input_sha']='0'*40
            if change=='wrong_snapshot': data['store_input_sha']=self.empty_sha
            (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
            with self.subTest(change=change), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
