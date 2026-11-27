"""Synthetic Git-only PROOF: exact finite PRE, then independently anchored early Close."""
import json
import unittest
from test_premarket_one_hop import PremarketOneHop
import soxx_appendix_exact_preflight as P
import publish_soxx_web_appendix as W

class FiniteClose(unittest.TestCase):
    def fixture(self,*,fault=None):
        f=PremarketOneHop()
        self.addCleanup(f.doCleanups)
        root,v1=f.build('2035-01-25',native_selection=True)
        import subprocess
        def g(*a):
            return subprocess.check_output(['git','-C',str(root),*a],stderr=subprocess.DEVNULL,text=True).strip()
        def write(path,obj):
            p=root/path
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(json.dumps(obj))
        day='2035-01-25'; month=day[:7]; start='12:45'; end='13:00'; sha='8'*40
        phase={'start':start,'end':end}
        stem='data/market-data/'
        close_session=stem+f'sessions/{month}/{day}/regular/1245-1300/twelve_data_basic/'
        close_snap=stem+f'snapshots/{month}/{day}/close/'
        proof=stem+f'proofs/deterministic-metrics/{month}/{day}/close/'
        collector=stem+f'collector-state/{month}/'
        inv=stem+'collector-state/market-data-inventory.json'
        terminal=collector+day+'-close-112233445566abcd-publication.json'
        snap=close_snap+'close-140100-et.json'; pr=proof+sha+'/close-140100-et.json'
        missing=collector+day+'-close-missing.json'; budget=collector+day+'-twelve-data-budget.json'
        symbols=[f'T{i}' for i in range(32)]+['SOXX']
        outputs={}; refs=[]
        for symbol in symbols:
            fn='close-'+symbol.lower()+'-140100-et'
            path=close_session+fn+'.json'
            row=dict(schema_version=1,provider='twelve_data_basic',trade_date=day,session='regular',
                     window=phase,capture_id=fn,qualified_facts=[{'symbol':symbol}],missing_symbols=[])
            if fault=='capture_window' and symbol=='SOXX': row['window']={'start':'12:40','end':'13:00'}
            if fault=='producer' and symbol=='SOXX': row['provider']='foreign'
            write(path,row)
            outputs[path]=g('hash-object',path)
            refs.append({'blob_sha':outputs[path],'kind':'regular_intraday_capture','path':path.removeprefix(stem),
                         'provider':'twelve_data_basic','reason_class':'success','symbol':symbol,'window':start+'-'+end})
        write(snap,dict(schema_version=1,snapshot_id='close-140100-et',trade_date=day,stage='close',target_window=phase,
                        data_refs=refs,missing=[],coverage={'symbols_requested_in_increment':33,'symbols_available_in_increment':33}))
        outputs[snap]=g('hash-object',snap)
        snap_ref=dict(path=snap.removeprefix(stem),blob_sha=outputs[snap],snapshot_id='close-140100-et')
        write(pr,dict(schema_version=1,proof_kind='stock_dairy_deterministic_metrics',stage='close',trade_date=day,
                      data_plane_commit_sha=('9'*40 if fault=='proof_compute' else sha),snapshot=snap_ref))
        outputs[pr]=g('hash-object',pr)
        latest_s=dict(snapshot_blob_sha=outputs[snap],snapshot_id='close-140100-et',
                      snapshot_path=snap.removeprefix(stem),schema_version=1)
        latest_p=dict(latest_s,proof_blob_sha=outputs[pr],proof_path=pr.removeprefix(stem))
        for p,o in ((close_snap+'latest.json',latest_s),(proof+'latest.json',latest_p),
                    (missing,dict(schema_version=1,trade_date=day,last_mode='close',missing=[])),
                    (budget,dict(schema_version=1,trade_date=day,fixture='budget')),
                    (inv,dict(fixture='inventory'))):
            write(p,o); outputs[p]=g('hash-object',p)
        if fault=='foreign_path':
            p=stem+'recovery/unlisted.json'; write(p,{}); outputs[p]=g('hash-object',p)
        if fault=='native':
            write(v1.native_path,{'fixture':'changed'})
        rowp=[dict(path=p.removeprefix(stem),blob_sha=b) for p,b in outputs.items()]
        if fault=='receipt': rowp.pop()
        write(terminal,dict(schema_version=1,stage=('close_retry' if fault=='unknown_terminal' else 'close'),
                            trade_date=day,request_id='close-'+day.replace('-','')+'-135501-et-01-collector',required_outputs=rowp))
        outputs[terminal]=g('hash-object',terminal)
        g('add','.');g('commit','-m','synthetic early Close')
        base=g('rev-parse','HEAD')
        from test_private_profile import fixture
        doc=fixture()
        doc.update(schema_version=2,evidence_base=v1.evidence_base,base=base,head='2'*40,
                   candidate_parents=[base],native_path=v1.native_path,native_blob=v1.native_blob,
                   native_selector_blob=g('rev-parse',v1.evidence_base+':data/market-data/series-active/daily/twelve_data_basic/SOXX.json'),
                   native_admission_blob=json.loads(g('show',v1.evidence_base+':data/market-data/series-active/daily/twelve_data_basic/SOXX.json'))['admission_blob_sha'],
                   frozen_appendix_commit='c'*40)
        doc['advance']=[dict(stage='premarket',commit=v1.base,tree=v1.advance.tree,trade_date=day,manifest=v1.advance.manifest,
                            paths={p:{'status':s,'blob':b} for p,(s,b) in v1.advance.paths.items()}),
                        dict(stage='close',commit=base,tree=g('rev-parse',base+'^{tree}'),trade_date=day,manifest=terminal,
                             paths={p:{'status':'M' if p==inv else 'A','blob':b} for p,b in outputs.items()},
                             compute_sha=sha,window=phase,snapshot_id='close-140100-et',symbols=symbols)]
        return root,doc

    def verify(self, root,doc):
        plan=P.load_profile(json.dumps(doc).encode())
        P._verify_plan_advance(root,plan)
        return plan

    def test_exact_positive_and_all_durable_inputs(self):
        plan=self.verify(*self.fixture())
        self.assertEqual(W._durable_inputs(plan),{plan.evidence_base,plan.base,plan.advance[0].publication_base})

    def test_unsafe_close_declines(self):
        for fault in ('capture_window','producer','proof_compute','foreign_path','native','receipt','unknown_terminal'):
            with self.subTest(fault=fault),self.assertRaises(ValueError):
                self.verify(*self.fixture(fault=fault))

    def test_closed_sequence_scope(self):
        root,doc=self.fixture()
        for wrong in (doc['advance'][::-1],doc['advance']+[doc['advance'][1]],doc['advance'][:1]):
            with self.subTest(length=len(wrong)),self.assertRaises(ValueError):
                self.verify(root,dict(doc,advance=wrong))

    def test_frozen_outputs_cannot_be_replaced_with_new_provenance(self):
        from dataclasses import replace
        import subprocess
        root,doc=self.fixture()
        plan=P.load_profile(json.dumps(doc).encode())
        def g(*a):
            return subprocess.check_output(['git','-C',str(root),*a],stderr=subprocess.DEVNULL,text=True).strip()
        blobs={}
        for path in plan.blobs:
            f=root/path
            f.parent.mkdir(parents=True,exist_ok=True)
            f.write_bytes(b'original fixture bytes')
            blobs[path]=g('hash-object',path)
        g('add','.');g('commit','-m','frozen four original outputs')
        frozen=g('rev-parse','HEAD')
        f=root/next(iter(blobs))
        f.write_bytes(b'new different bytes')
        altered=g('hash-object',str(f.relative_to(root)))
        g('add','.');g('commit','-m','drift fixture')
        candidate=g('rev-parse','HEAD')
        plan=replace(plan,frozen_appendix_commit=frozen,head=candidate,blobs=blobs)
        P._verify_frozen_outputs(root,plan)
        with self.assertRaisesRegex(ValueError,'FROZEN_APPENDIX_BYTES_CHANGE'):
            P._verify_frozen_outputs(root,replace(plan,blobs={**blobs,str(f.relative_to(root)):altered}))

    def test_missing_intermediate_tag_fails_before_cas(self):
        from pathlib import Path
        from unittest.mock import patch
        root,doc=self.fixture()
        plan=P.load_profile(json.dumps(doc).encode())
        missing='refs/tags/market-data-read/'+plan.advance[0].publication_base
        statuses=iter([b'7'*40,b'',b'7'*40+b'\trefs/heads/main',plan.base.encode()+b'\trefs/heads/main'])
        def read(_root,*args):
            if args[0]=='ls-remote' and args[-1].startswith('refs/tags/'):
                if args[-1]==missing:
                    raise ValueError('OBJECT_UNREADABLE')
                return args[-1].rsplit('/',1)[-1].encode()+b'\t'+args[-1].encode()
            return next(statuses)
        with patch.object(W,'_git',side_effect=read), self.assertRaisesRegex(ValueError,'OBJECT_UNREADABLE'):
            W._require_local_and_remote(root,root,'7'*40,plan)
