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
        compute_sha=subprocess.check_output(['/usr/bin/git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
        self.diagnostic={'schema_version':1,'diagnostic_authority':'non_authoritative_conflict_evidence','trade_date':'2026-11-04','invocation':{'mode':'previous_session_eod','invocation_id':inv},'compute_sha':compute_sha,'store_input_sha':self.old_sha,'failure_target_count':1,'conflict_record_count':1,'conflict_diagnostic_record_count':1,'conflict_diagnostics_truncated':False,'conflicts':[record]}
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
        for change in ('digest','old_value','old_type','row_hash','new_row_hash','index_blob','shard_blob','row_number','missing_snapshot','wrong_snapshot','missing_compute'):
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
            if change=='missing_compute': data['compute_sha']='a'*40
            (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
            with self.subTest(change=change), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_duplicate_json_and_nonfinite_rejected(self):
        import offline_store_audit as audit
        index=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD/_index.json'
        original=index.read_text()
        for label, raw in [('duplicate',original.replace('{','{"symbol":"AMD",',1)),
                           ('nan',original.replace('{','{"extra":NaN,',1)),
                           ('infinity',original.replace('{','{"extra":Infinity,',1)),
                           ('overflow',original.replace('{','{"extra":1e999,',1))]:
            index.write_text(raw); self.commit()
            with self.subTest(label=label), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_conflict_duplicate_key_rejected(self):
        import offline_store_audit as audit
        path=self.repo/self.conflict
        raw=path.read_text().replace('{','{"store_input_sha":"'+'0'*40+'",',1)
        path.write_text(raw); self.commit()
        with self.assertRaises(audit.AuditError):
            audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_required_blob_close_and_canonical_date(self):
        import offline_store_audit as audit
        index_path=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD/_index.json'
        shard_path=index_path.parent/'000000.json'
        original_index=json.loads(index_path.read_text()); original_doc=json.loads(shard_path.read_text())
        for kind in ('missing_blob','null_blob','nonobject_meta','wrong_count_type','missing_close','bad_date','nonnumeric_close','nan_close'):
            index=json.loads(json.dumps(original_index)); doc=json.loads(json.dumps(original_doc))
            if kind=='missing_blob': index['shards'][0].pop('blob_sha')
            elif kind=='null_blob': index['shards'][0]['blob_sha']=None
            elif kind=='nonobject_meta': index['shards'][0]=123
            elif kind=='wrong_count_type': index['shards'][0]['count']=True
            else:
                if kind=='missing_close': doc['records'][0].pop('close')
                if kind=='bad_date': doc['records'][0]['trade_date']='20261103'
                if kind=='nonnumeric_close': doc['records'][0]['close']='bad'
                if kind=='nan_close': doc['records'][0]['close']=float('nan')
                raw=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=True).encode()
                shard_path.write_bytes(raw+b'\n')
                meta=index['shards'][0]; meta['blob_sha']=store.git_blob_sha_bytes(raw+b'\n'); meta['json_sha256']=hashlib.sha256(raw).hexdigest(); meta['byte_length']=len(raw)
            index_path.write_text(json.dumps(index)); self.commit()
            with self.subTest(kind=kind), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
            shard_path.write_text(json.dumps(original_doc,sort_keys=True,separators=(',',':'))+'\n')

    def test_old_index_gap_and_wrong_shapes_fail_closed(self):
        import copy
        import offline_store_audit as audit
        base=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD'
        index_path=base/'_index.json'; shard_path=base/'000000.json'
        original_index=index_path.read_bytes(); original_shard=shard_path.read_bytes()
        index=json.loads(original_index); doc=json.loads(original_shard)
        doc['start']=100; raw=store.canonical_bytes(doc); shard_path.write_bytes(raw+b'\n')
        index['shards'][0]=store._shard_meta('000000.json',100,doc,False)
        index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); forged_sha=self.sha
        data=copy.deepcopy(self.diagnostic); item=data['conflicts'][0]
        data['store_input_sha']=forged_sha; item['store_source']['row_number']=100
        item['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{forged_sha}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
        item['store_source']['shard']['blob_sha']=index['shards'][0]['blob_sha']
        index_path.write_bytes(original_index); shard_path.write_bytes(original_shard)
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        data['conflicts'][0]['store_source']['shard']['path']=123
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],123)

    def test_cli_error_is_generic(self):
        command=[sys.executable, str(ROOT/'scripts/offline_store_audit.py'),'--store',str(self.repo/'private-secret'),
                 '--commit','0'*40,'--trade-date','2026-11-04','--target-date','2026-11-03',
                 '--symbols','AMD','--conflict-path',self.conflict,'--conflict-blob','0'*40]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertNotIn('private-secret',result.stderr)
        self.assertEqual(result.stdout,'')
        self.assertEqual(result.stderr,'local evidence audit failed: invalid or unavailable input\n')

    def test_old_index_prior_shard_actual_count_must_prove_global_offset(self):
        import copy
        import offline_store_audit as audit
        base=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD'
        index_path=base/'_index.json'; first_path=base/'000000.json'; second_path=base/'000100.json'
        original_index=index_path.read_bytes(); original_shard=first_path.read_bytes()
        index=json.loads(original_index); target=json.loads(original_shard)
        first=copy.deepcopy(target); first['records'][0]['trade_date']='2026-11-02'
        first_path.write_bytes(store.canonical_bytes(first)+b'\n')
        first_meta=store._shard_meta('000000.json',0,first,True)
        target['start']=1; second_path.write_bytes(store.canonical_bytes(target)+b'\n')
        target_meta=store._shard_meta('000100.json',1,target,False)
        index['shards']=[first_meta,target_meta]; index['record_count']=2; index['first_trade_date']='2026-11-02'
        index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); valid_sha=self.sha
        data=copy.deepcopy(self.diagnostic); row=data['conflicts'][0]
        data['store_input_sha']=valid_sha; row['store_source']['row_number']=1
        row['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{valid_sha}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
        row['store_source']['shard'].update(path='series/daily/twelve_data_basic/AMD/000100.json',blob_sha=target_meta['blob_sha'])
        index_path.write_bytes(original_index); first_path.write_bytes(original_shard); second_path.unlink()
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)['verified_conflict_entries'],1)
        # Two individually valid shards cannot duplicate a date across their boundary.
        first['records'][0]['trade_date']='2026-11-03'; first_path.write_bytes(store.canonical_bytes(first)+b'\n')
        target['start']=1; second_path.write_bytes(store.canonical_bytes(target)+b'\n')
        index['shards']=[store._shard_meta('000000.json',0,first,True),store._shard_meta('000100.json',1,target,False)]
        index['record_count']=2; index['first_trade_date']='2026-11-03'
        index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); duplicate_sha=self.sha
        data['store_input_sha']=duplicate_sha; row['store_source']['row_number']=1
        row['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{duplicate_sha}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
        row['store_source']['shard']['blob_sha']=index['shards'][1]['blob_sha']
        index_path.write_bytes(original_index); first_path.write_bytes(original_shard); second_path.unlink()
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        first['records'][0]['trade_date']='2026-11-02'
        first_path.write_bytes(store.canonical_bytes(first)+b'\n')
        first_meta=store._shard_meta('000000.json',0,first,True)
        first_meta.update(count=100,end=99)
        target['start']=100; second_path.write_bytes(store.canonical_bytes(target)+b'\n')
        target_meta=store._shard_meta('000100.json',100,target,False)
        index['shards']=[first_meta,target_meta]; index['record_count']=101; index['first_trade_date']='2026-11-02'
        index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); forged=self.sha
        data=copy.deepcopy(self.diagnostic); row=data['conflicts'][0]
        data['store_input_sha']=forged; row['store_source']['row_number']=100
        row['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{forged}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
        row['store_source']['shard'].update(path='series/daily/twelve_data_basic/AMD/000100.json',blob_sha=target_meta['blob_sha'])
        index_path.write_bytes(original_index); first_path.write_bytes(original_shard); second_path.unlink()
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

        # A valid-looking index with an absent prior shard also cannot prove offset 1.
        first_meta=store._shard_meta('000000.json',0,first,True)
        target['start']=1; second_path.write_bytes(store.canonical_bytes(target)+b'\n')
        target_meta=store._shard_meta('000100.json',1,target,False)
        index['shards']=[first_meta,target_meta]; index['record_count']=2
        index_path.write_bytes(store.canonical_bytes(index)+b'\n'); first_path.unlink()
        self.commit(); missing=self.sha
        data['store_input_sha']=missing; row['store_source']['row_number']=1
        row['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{missing}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
        row['store_source']['shard']['blob_sha']=target_meta['blob_sha']
        index_path.write_bytes(original_index); first_path.write_bytes(original_shard); second_path.unlink()
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_current_index_identity_and_optional_schema(self):
        import copy
        import offline_store_audit as audit
        path=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD/_index.json'
        original=json.loads(path.read_text())
        for field,value in [('schema_version',None),('schema_version',999),('schema_version',True),('interval','1min'),('asset_class','crypto'),('session','extended'),('series_semantics','wrong'),('adjustment_semantics','wrong'),('provider','wrong'),('symbol','OTHER'),('series_id','wrong'),('sort_order','descending'),('identity_effective_from','not-a-date'),('lineage_segments',{})]:
            index=copy.deepcopy(original)
            if field=='schema_version' and value is None: index.pop(field)
            else: index[field]=value
            path.write_text(json.dumps(index)); self.commit()
            with self.subTest(field=field,value=value), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_prior_index_identity_even_when_snapshot_and_blob_rebound(self):
        import copy
        import offline_store_audit as audit
        path=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD/_index.json'
        original=path.read_bytes(); index0=json.loads(original)
        cases=[('schema_version',None),('schema_version',999),('schema_version',True),('interval','1min'),('asset_class','crypto'),('session','extended'),('series_semantics','wrong'),('adjustment_semantics','wrong'),('provider','wrong'),('symbol','OTHER'),('series_id','wrong'),('sort_order','descending'),('identity_effective_from','not-a-date'),('lineage_segments',{})]
        for field,value in cases:
            index=copy.deepcopy(index0)
            if field=='schema_version' and value is None: index.pop(field)
            else: index[field]=value
            path.write_text(json.dumps(index)); self.commit(); old_sha=self.sha
            index_blob=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{old_sha}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
            diagnostic=copy.deepcopy(self.diagnostic); diagnostic['store_input_sha']=old_sha
            diagnostic['conflicts'][0]['store_source']['index_blob_sha']=index_blob
            path.write_bytes(original); (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
            with self.subTest(field=field,value=value), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_conflict_envelope_strict_types(self):
        import copy
        import offline_store_audit as audit
        for field,value in [('schema_version',True),('schema_version',1.0),('conflict_diagnostics_truncated',0),('conflict_diagnostics_truncated',1)]:
            diagnostic=copy.deepcopy(self.diagnostic); diagnostic[field]=value
            (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
            with self.subTest(field=field,value=value), self.assertRaises(audit.AuditError):
                audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_old_etf_conflict_uses_compute_pinned_collection_identity(self):
        import copy
        import offline_store_audit as audit
        row={'trade_date':'2026-11-03','open':1,'high':2,'low':1,'close':2,'volume':5}
        store.append_daily_bars(self.repo/'data/market-data',provider='twelve_data_basic',symbol='GLD',asset_class='etf',series_semantics='daily_regular_ohlcv',bars=[row])
        self.commit(); old_sha=self.sha
        extra=collectors._conflict_record(store_root=self.repo/'data/market-data',symbol='GLD',old=row,new=dict(row,close=3),outputsize=10,effective_date=None,trade_date='2026-11-04',access={'twelve_data_basic':{'base_url':'https://example.invalid','time_series_path':'/time_series','interval_daily':'1day'}})
        diagnostic=copy.deepcopy(self.diagnostic); diagnostic['store_input_sha']=old_sha
        diagnostic['conflicts'].append(extra); diagnostic['conflict_record_count']=diagnostic['conflict_diagnostic_record_count']=2; diagnostic['failure_target_count']=2
        (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
        self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)['verified_conflict_entries'],2)
        # Changing only the GLD asset class and rebinding the old index cannot become valid.
        index_path=self.repo/'data/market-data/series/daily/twelve_data_basic/GLD/_index.json'
        index=json.loads(index_path.read_text()); index['asset_class']='stocks'; index_path.write_text(json.dumps(index)); self.commit(); bad_old=self.sha
        diagnostic['store_input_sha']=bad_old
        diagnostic['conflicts'][1]['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{bad_old}:data/market-data/series/daily/twelve_data_basic/GLD/_index.json'],text=True).strip()
        (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
        with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_current_volume_owner_boundary(self):
        import offline_store_audit as audit
        index_path=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD/_index.json'
        shard_path=index_path.parent/'000000.json'
        index0=json.loads(index_path.read_text()); doc0=json.loads(shard_path.read_text())
        for value,valid in [(-1,False),('bad',False),(True,True),(None,True)]:
            index=json.loads(json.dumps(index0)); doc=json.loads(json.dumps(doc0)); doc['records'][0]['volume']=value
            raw=store.canonical_bytes(doc); shard_path.write_bytes(raw+b'\n')
            index['shards'][0]=store._shard_meta('000000.json',0,doc,False)
            index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit()
            with self.subTest(value=value):
                if valid: self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)['verified_conflict_entries'],1)
                else:
                    with self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_old_duplicate_dates_and_volume_fail_after_rebinding(self):
        import copy
        import offline_store_audit as audit
        base=self.repo/'data/market-data/series/daily/twelve_data_basic/AMD'
        index_path=base/'_index.json'; shard_path=base/'000000.json'
        original_index=index_path.read_bytes(); original_shard=shard_path.read_bytes()
        index0=json.loads(original_index); doc0=json.loads(original_shard)
        for kind in ('duplicate','negative_volume','string_volume'):
            index=copy.deepcopy(index0); doc=copy.deepcopy(doc0); position=0
            if kind=='duplicate': doc['records'].append(copy.deepcopy(doc['records'][0])); index['record_count']=2; position=1
            if kind=='negative_volume': doc['records'][0]['volume']=-1
            if kind=='string_volume': doc['records'][0]['volume']='bad'
            shard_path.write_bytes(store.canonical_bytes(doc)+b'\n'); index['shards'][0]=store._shard_meta('000000.json',0,doc,False)
            index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); old_sha=self.sha
            diagnostic=copy.deepcopy(self.diagnostic); diagnostic['store_input_sha']=old_sha; item=diagnostic['conflicts'][0]
            item['store_source']['row_number']=position; item['old_row_sha256']=store.json_sha256(doc['records'][position])
            item['new_row_sha256']=store.json_sha256(dict(doc['records'][position],close=3))
            item['store_source']['index_blob_sha']=subprocess.check_output(['/usr/bin/git','-C',str(self.repo),'rev-parse',f'{old_sha}:data/market-data/series/daily/twelve_data_basic/AMD/_index.json'],text=True).strip()
            item['store_source']['shard']['blob_sha']=index['shards'][0]['blob_sha']
            index_path.write_bytes(original_index); shard_path.write_bytes(original_shard)
            (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
            with self.subTest(kind=kind), self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)

    def test_conflict_interval_and_failure_target_count(self):
        import copy
        import offline_store_audit as audit
        for kind in ('wrong_interval','missing_interval','zero_failures'):
            data=copy.deepcopy(self.diagnostic)
            if kind=='wrong_interval': data['conflicts'][0]['request_parameters']['interval']='1min'
            if kind=='missing_interval': data['conflicts'][0]['request_parameters'].pop('interval')
            if kind=='zero_failures': data['failure_target_count']=0
            (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
            with self.subTest(kind=kind), self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)
        data=copy.deepcopy(self.diagnostic); data['conflicts'].append(copy.deepcopy(data['conflicts'][0])); data['conflict_record_count']=data['conflict_diagnostic_record_count']=2
        (self.repo/self.conflict).write_text(json.dumps(data)); self.commit()
        self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-11-03',['AMD'],self.conflict)['verified_conflict_entries'],2)

    def test_spcx_effective_date_current_and_old_rows(self):
        import copy
        import offline_store_audit as audit
        valid={'trade_date':'2026-06-12','open':1,'high':2,'low':1,'close':2,'volume':5}
        store.append_daily_bars(self.repo/'data/market-data',provider='twelve_data_basic',symbol='SPCX',asset_class='stocks',series_semantics='daily_regular_ohlcv',identity_effective_from='2026-06-12',bars=[valid,dict(valid,trade_date='2026-06-15')])
        self.commit()
        self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-06-12',['SPCX'],self.conflict)['series'][0]['target_date_present'],True)
        self.assertEqual(audit.audit(self.repo,self.sha,'2026-11-04','2026-06-15',['SPCX'],self.conflict)['series'][0]['target_date_present'],True)
        base=self.repo/'data/market-data/series/daily/twelve_data_basic/SPCX'; index_path=base/'_index.json'; shard_path=base/'000000.json'
        original_index=index_path.read_bytes(); original_shard=shard_path.read_bytes()
        index=json.loads(original_index); doc=json.loads(original_shard); doc['records'][0]['trade_date']='2026-06-11'
        shard_path.write_bytes(store.canonical_bytes(doc)+b'\n'); index['shards'][0]=store._shard_meta('000000.json',0,doc,False)
        index['first_trade_date']='2026-06-11'; index_path.write_bytes(store.canonical_bytes(index)+b'\n'); self.commit(); bad_old=self.sha
        with self.subTest(source='current'), self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-06-11',['SPCX'],self.conflict)
        row=doc['records'][0]
        extra=collectors._conflict_record(store_root=self.repo/'data/market-data',symbol='SPCX',old=row,new=dict(row,close=3),outputsize=10,effective_date='2026-06-12',trade_date='2026-11-04',access={'twelve_data_basic':{'base_url':'https://example.invalid','time_series_path':'/time_series','interval_daily':'1day'}})
        diagnostic=copy.deepcopy(self.diagnostic); diagnostic['store_input_sha']=bad_old; diagnostic['conflicts']=[extra]
        index_path.write_bytes(original_index); shard_path.write_bytes(original_shard)
        (self.repo/self.conflict).write_text(json.dumps(diagnostic)); self.commit()
        with self.subTest(source='old'), self.assertRaises(audit.AuditError): audit.audit(self.repo,self.sha,'2026-11-04','2026-06-12',['SPCX'],self.conflict)
