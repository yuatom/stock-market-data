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

class OfflineAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'store'; self.repo.mkdir()
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.repo)], check=True)
        store.append_daily_bars(self.repo / 'data/market-data', provider='twelve_data_basic', symbol='AMD', asset_class='stocks', series_semantics='daily_regular_ohlcv', bars=[{'trade_date':'2026-11-03','open':1,'high':2,'low':1,'close':2,'volume':5}])
        inv='fixture-run'; token=hashlib.sha256(inv.encode()).hexdigest()[:16]
        self.conflict=f'data/market-data/collector-state/2026-11/2026-11-04-previous_session_eod-{token}-conflicts.json'
        path=self.repo / self.conflict; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version':1,'diagnostic_authority':'non_authoritative_conflict_evidence','trade_date':'2026-11-04','invocation':{'mode':'previous_session_eod','invocation_id':inv},'compute_sha':'a'*40,'store_input_sha':'b'*40,'failure_target_count':0,'conflict_record_count':0,'conflict_diagnostic_record_count':0,'conflict_diagnostics_truncated':False,'conflicts':[]}))
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
