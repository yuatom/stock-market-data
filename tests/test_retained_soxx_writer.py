"""Local bare-Git writer tests; synthetic prepared objects, no real credentials.

Pure preparation and historical semantic reading have separate tests. These
cases isolate CAS/receipt/publication behavior with an explicit read seam.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import admit_retained_soxx_basis as W
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_remote import RemoteGitStore, STORE_URL


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.PIPE).decode().strip()

class Transport:
    def __init__(self,repo,remote):self.repo,self.remote,self.pushes=repo,remote,[];self.fail_push=None
    def metadata(self):
        return dict(id=1335101677,private=True,full_name='yuatom/stock-market-data-store',default_branch='main',
                    owner=dict(id=1,login='yuatom',type='User'),permissions=dict(pull=True,push=True,admin=False))
    def git(self,*args):
        actual=[str(self.remote) if x==STORE_URL else x for x in args]
        result=subprocess.run(['git','-C',str(self.repo),*actual],capture_output=True)
        if args[0]=='push':
            self.pushes.append(args)
            if len(self.pushes)==self.fail_push:
                return subprocess.CompletedProcess(args,1,b'',b'SYNTHETIC_UNKNOWN_AFTER_APPLY')
        return result

class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.remote=self.root/'remote.git';self.repo=self.root/'repo'
        git(self.root,'init','--bare',str(self.remote));git(self.root,'clone',str(self.remote),str(self.repo))
        git(self.repo,'config','user.name','synthetic');git(self.repo,'config','user.email','test@example.invalid')
        for p in ['legacy.json','budget.json','terminal.json']:(self.repo/p).write_text('unchanged')
        git(self.repo,'add','.');git(self.repo,'commit','-m','seed');git(self.repo,'branch','-M','main');git(self.repo,'push','origin','main')
        git(self.repo,'remote','set-url','origin',STORE_URL)
        self.base=git(self.repo,'rev-parse','HEAD');self.transport=Transport(self.repo,self.remote)
        self.store=RemoteGitStore(self.repo,'synthetic-token',source_recheck=lambda:None,platform_guard=lambda:None,transport=self.transport)
        self.now=datetime(2026,11,14,15,tzinfo=timezone.utc);self.compute='a'*40
        basis='b'*64;admission=B.encode({'synthetic':True});diagnostic=B.encode({'synthetic':'diagnostic'})
        end=datetime(2026,11,11)
        records=[dict(datetime=(end-timedelta(days=317-i)).strftime('%Y-%m-%d')) for i in range(318)]
        snapshot=dict(records=records,baseline_evidence_sha256=B.sha(diagnostic))
        snapshot_raw=B.encode(snapshot)
        catalog=dict(schema_version=1,provider=B.PROVIDER,symbol='SOXX',basis_id=basis,
                     snapshot_blob_sha=B.blob_sha(snapshot_raw),admission_blob_sha=B.blob_sha(admission))
        updates={B.catalog_path(B.PROVIDER,'SOXX'):B.encode(catalog),
            B.snapshot_path(B.PROVIDER,'SOXX',basis):snapshot_raw,
            B.admission_path(B.PROVIDER,'SOXX',catalog['admission_blob_sha']):admission,W.DIAGNOSTIC_PATH:diagnostic}
        manifest=dict(new_basis_id=basis,original_diagnostic_raw_recovered=False,
                      objects=[dict(path=p,blob_sha=B.blob_sha(raw),sha256=B.sha(raw)) for p,raw in sorted(updates.items())])
        updates[W.MANIFEST_PATH]=B.encode(manifest)
        self.prepared=dict(basis_id=basis,catalog=catalog,snapshot=snapshot,updates=updates,reconstruction_audit=manifest)
        self.progress={}
    def publish(self,prepared=None):
        with self.store.lock(),patch.object(B,'read_price_view',return_value=dict(record_count=318,volume_status='BLOCKED_UNRESOLVED')):
            return W.publish_prepared(self.store,prepared or self.prepared,expected_head=self.base,
                compute_sha=self.compute,now=self.now,progress=self.progress)
    def test_real_cas_receipt_binding_and_zero_legacy_changes(self):
        result=self.publish();head=result['selector']['read_sha'];receipt=result['receipt_commit']
        self.assertEqual(git(self.remote,'rev-parse','main'),head)
        self.assertEqual(git(self.remote,'rev-parse',result['receipt_ref']),receipt)
        for sha in [head,receipt]:self.assertEqual(git(self.remote,'rev-parse','refs/tags/market-data-read/'+sha),sha)
        self.assertEqual(set(git(self.repo,'diff-tree','--no-commit-id','--name-only','-r',self.base,head).splitlines()),set(self.prepared['updates']))
        for p in ['legacy.json','budget.json','terminal.json']:self.assertEqual(git(self.repo,'show',head+':'+p),'unchanged')
        selector,_=B.load_consumer_receipt(self.repo,result['receipt_commitment'],self.compute,now=self.now.isoformat())
        self.assertEqual(selector,result['selector']);self.assertEqual(result['requests_attempted'],0)
        self.assertEqual(len(self.transport.pushes),2)
        self.assertNotIn('nonce',json.dumps(result));self.assertNotIn('nonce',json.dumps(self.progress))
    def test_changed_expected_head_rejected_before_any_push(self):
        self.base='f'*40
        with self.assertRaises(Exception):self.publish()
        self.assertEqual(self.transport.pushes,[])
    def test_unrequested_extra_object_rejected(self):
        bad=copy.deepcopy(self.prepared);bad['updates']['legacy.json']=b'rewritten'
        with self.assertRaises(Exception):self.publish(bad)
        self.assertEqual(self.transport.pushes,[])
    def test_manifest_or_diagnostic_tamper_rejected_before_push(self):
        for path in [W.MANIFEST_PATH,W.DIAGNOSTIC_PATH]:
            bad=copy.deepcopy(self.prepared);bad['updates'][path]=B.encode({'tampered':True})
            with self.assertRaises(Exception):self.publish(bad)
        self.assertEqual(self.transport.pushes,[])
    def test_nonhistorical_cutoff_rejected(self):
        bad=copy.deepcopy(self.prepared);bad['snapshot']['records'][-1]['datetime']='2026-11-13'
        with self.assertRaises(Exception):self.publish(bad)
        self.assertEqual(self.transport.pushes,[])
    def test_main_unknown_does_not_publish_receipt_or_retry(self):
        self.transport.fail_push=1
        with self.assertRaises(Exception):self.publish()
        self.assertEqual(len(self.transport.pushes),1)
        self.assertEqual(self.progress['stage'],'MAIN_PUBLICATION_ATTEMPTED')
        self.assertEqual(git(self.remote,'for-each-ref','--format=%(refname)','refs/heads/basis-read-receipts'), '')
    def test_receipt_unknown_keeps_main_real_and_stops(self):
        self.transport.fail_push=2
        with self.assertRaises(Exception):self.publish()
        self.assertEqual(len(self.transport.pushes),2)
        self.assertEqual(self.progress['stage'],'MAIN_DURABLE_RECEIPT_NOT_YET_PUBLISHED')
        self.assertEqual(git(self.remote,'rev-parse','main'),self.progress['candidate_commit'])
    def test_historical_reader_rejects_before_publication(self):
        with self.store.lock(),patch.object(B,'read_price_view',side_effect=ValueError('synthetic historical rejection')):
            with self.assertRaises(Exception):W.publish_prepared(self.store,self.prepared,expected_head=self.base,compute_sha=self.compute,now=self.now,progress=self.progress)
        self.assertEqual(self.transport.pushes,[])

class WorkflowTests(unittest.TestCase):
    def test_nonworkflow_execution_stops_before_store_access(self):
        with patch.dict(os.environ,{},clear=True),patch.object(W,'RemoteGitStore') as constructor:
            with self.assertRaises(Exception):W.execute('/not-used','a'*40,{})
            constructor.assert_not_called()

    def test_manual_only_shared_owner_no_provider_secret(self):
        import yaml
        raw=(Path(__file__).resolve().parents[1]/'.github/workflows/retained-soxx-initial-admission.yml').read_text()
        w=yaml.safe_load(raw)
        self.assertEqual(set(w[True]),{'workflow_dispatch'})
        self.assertEqual(w['concurrency']['group'],'market-data-collector')
        self.assertFalse(w['concurrency']['cancel-in-progress'])
        self.assertNotIn('TWELVE_DATA',raw)
        self.assertIn('persist-credentials: false',raw)
        contract=yaml.safe_load((Path(__file__).resolve().parents[1]/'config/store-publication-durability.yaml').read_text())
        entry=contract['writer_scope']['guarded_manual_wrappers']['.github/workflows/retained-soxx-initial-admission.yml']
        self.assertEqual(entry['publication_helper'],'scripts/daily_basis_remote.py#RemoteGitStore')
        self.assertEqual(entry['provider_requests'],0)
        self.assertEqual(entry['unknown_outcome'],'stop_inspect_no_retry')
