from datetime import datetime,timezone
from unittest import TestCase
from unittest.mock import patch
from scripts import emit_retained_pre_backup_delivery as D
from scripts import retained_price_only_slots as S
import base64

class Delivery(TestCase):
    def setUp(self):
        self.sha='a'*40
        self.now='2026-01-13T11:00:00Z'
        self.ctx=dict(repository=D.STORE,repository_id=44,owner_id=101,actor_id=101,run_id=1001,
            run_attempt=1,workflow_path=D.WORKFLOW,logical_job=D.JOB,workflow_source_sha='b'*40)
        self.producer=dict(repository=D.COMPUTE,repository_id=66,owner_id=101,actor_id=101,workflow_id=700,
            workflow_path=D.PRODUCER_WORKFLOW,job_id=701,run_id=222,run_attempt=1,source_sha=self.sha)
        self.artifact=dict(compute_sha=self.sha,policy_blob='f'*40,policy_sha256='e'*64,published_store_sha='d'*40,
            previous_session='2026-01-12',claim_commit='c'*40,claim_blob='b'*40,run_id='222',
            receipt_commit='a'*40,source_commit='7'*40,receipt_commitment='8'*64,sources=['synthetic_a','synthetic_b'])
    def receipt(self,**kw):
        return S.encode(dict(schema_version=1,kind='private_retained_pre_backup_read_receipt',
            purpose='prior_session_close_only',compute_sha=self.sha,workflow_source_sha='b'*40,
            nonce='9'*64,valid_from='2026-01-13T10:00:00Z',valid_until='2026-01-13T14:00:00Z',
            producer=self.producer,artifact=self.artifact)|kw)
    def test_opaque_receipt_binds_public_source_to_private_delivery_and_expiry(self):
        raw=self.receipt()
        doc=D.bound_receipt(raw,D.commitment(raw),self.sha,self.ctx,self.now)
        self.assertEqual(doc['artifact']['run_id'],'222')
        for changes in (dict(producer=self.producer|{'run_id':333}),dict(artifact=self.artifact|{'compute_sha':'e'*40}),
                        dict(valid_until='2026-01-13T10:59:00Z'),dict(workflow_source_sha='e'*40)):
            altered=self.receipt(**changes)
            with self.assertRaises(D.DeliveryHold):
                D.bound_receipt(altered,D.commitment(altered),self.sha,self.ctx,self.now)
    def test_private_log_envelope_only_after_artifact_readback(self):
        raw=self.receipt()
        receipt=D.bound_receipt(raw,D.commitment(raw),self.sha,self.ctx,self.now)
        with patch.object(D,'read_receipt',return_value=receipt), patch.object(D,'verify_artifact') as verify:
            line=D.build_delivery(compute=None,store=None,compute_sha=self.sha,
                approval=D.commitment(raw),delivery=self.ctx,now=self.now)
        verify.assert_called_once()
        value=S.parse(base64.b64decode(line.removeprefix(D.MARKER)))
        self.assertEqual(value['delivery']['repository'],D.STORE)
        self.assertEqual(value['producer']['repository'],D.COMPUTE)
        self.assertEqual(value['artifact']['run_id'],'222')
        self.assertEqual(value['verified_at'],self.now)
        self.assertNotIn('job_id',value['delivery'])
        with patch.object(D,'read_receipt',return_value=receipt),patch.object(D,'verify_artifact',side_effect=D.DeliveryHold),self.assertRaises(D.DeliveryHold):
            D.build_delivery(compute=None,store=None,compute_sha=self.sha,approval=D.commitment(raw),delivery=self.ctx,now=self.now)
    def test_unsupported_visibility_or_ambiguous_namespace_never_delivers(self):
        raw=self.receipt()
        for ctx in (self.ctx|dict(repository=D.COMPUTE),self.ctx|dict(run_attempt=2),self.ctx|dict(actor_id=102)):
            with self.assertRaises(D.DeliveryHold):D.bound_receipt(raw,D.commitment(raw),self.sha,ctx,self.now)
        with patch.object(D,'read_ref',return_value='a'*40),self.assertRaises(D.DeliveryHold):
            D.resolve_purpose(None,self.sha,D.commitment(raw))

    def test_real_local_private_git_delivery_reads_exact_claim_and_retention(self):
        from pathlib import Path
        from scripts import retained_price_only_source_policy as P
        import test_retained_slots as W
        h=W.Claims();h.setUp();self.addCleanup(h.doCleanups)
        compute=h.root/'compute';compute.mkdir();W.git(compute,'init','-qb','main')
        config=compute/P.POLICY_PATH;config.parent.mkdir()
        profiles=[dict(source_id=s,provider_identity=p,role='corroboration_only',
            adapter='offline_public_web_crosscheck',origin=o,license_sha256=None,valid_from=None,valid_until=None)
            for s,(p,o) in P.WEB.items()]
        profiles.extend(dict(source_id=s,provider_identity='provider_'+s,role='admission_eligible',
            adapter=P.LICENSED,origin=url.removesuffix('/price'),license_sha256='e'*64,
            valid_from='2026-11-01T00:00:00Z',valid_until='2026-11-07T00:00:00Z') for s,url in h.observations.items())
        policy=S.encode(dict(schema_version=1,kind='retained_price_only_source_eligibility',profiles=profiles))
        config.write_bytes(policy);W.git(compute,'add','.');W.git(compute,'commit','-qm','Synthetic release')
        sha=W.git(compute,'rev-parse','HEAD').stdout.strip()
        with patch.object(W,'IDENT',W.IDENT|{'symbol':'SOXX'}):row,packages=W.package(h.repo,W.D1)
        for e in row['evidence']:
            contents=S.parse(packages[e['path']]);contents['kind']='licensed_regular_daily_price_observation'
            encoded=S.encode(contents);packages[e['path']]=encoded;e.update(blob_sha=S.blob(encoded),sha256=S.sha(encoded))
        plan=S.plan(h.repo,h.base,((row,packages),),source_urls=h.observations,session_close={W.D1:'16:00'},
            legacy_occupied=set(),request_id='101-1',observation_kind='licensed_regular_daily_price_observation')
        proof=dict(compute_sha=sha,run_id='222',receipt_commit='1'*40,source_commit='2'*40,receipt_commitment='3'*64)
        market=S.commit_atomically(h.repo,plan,before_push=lambda:True,publication_proof=proof)
        replica=h.root/'private-review';W.git(h.root,'clone','-q',str(h.store),str(replica))
        W.git(replica,'remote','set-url','origin','https://github.com/yuatom/stock-market-data-store')
        claim_ref=S.slot_key(row).replace('refs/heads/','refs/remotes/origin/',1)
        claim_oid=W.git(replica,'rev-parse',claim_ref).stdout.strip()
        claim_blob=W.git(replica,'rev-parse',claim_oid+':claim.json').stdout.strip()
        now='2026-11-04T01:00:00Z'
        artifact=dict(policy_blob=S.blob(policy),policy_sha256=S.sha(policy),published_store_sha=market,
            previous_session=W.D1,claim_commit=claim_oid,claim_blob=claim_blob,sources=sorted(h.observations))|proof
        raw=self.receipt(compute_sha=sha,artifact=artifact,producer=self.producer|{'source_sha':sha},
            valid_from='2026-11-04T00:00:00Z',valid_until='2026-11-04T02:00:00Z')
        approved_ref=D.refs(sha,D.commitment(raw))[1]
        receipt_oid=S._commit(replica,market,{D.RECEIPT:raw})
        W.git(replica,'update-ref',approved_ref,receipt_oid)
        line=D.build_delivery(compute=compute,store=replica,compute_sha=sha,
            approval=D.commitment(raw),delivery=self.ctx,now=now)
        expected=S.parse(base64.b64decode(line.removeprefix(D.MARKER)))
        self.assertEqual(expected['artifact'],artifact)
        # A later commit may reuse this literal manifest path, even with a
        # copied request identifier. Its bytes cannot certify the older batch.
        manifest=S.parse(S.read(replica,market,plan.manifest))
        manifest['required_outputs'][0]['blob_sha']='0'*40
        later=S._commit(replica,market,{plan.manifest:S.encode(manifest)})
        W.git(replica,'update-ref','refs/heads/main',later,market)
        W.git(replica,'tag','market-data-read/'+later,later)
        old_output=D.build_delivery(compute=compute,store=replica,compute_sha=sha,
            approval=D.commitment(raw),delivery=self.ctx,now=now)
        self.assertEqual(old_output,line)
        forged=self.receipt(compute_sha=sha,artifact=artifact|{'published_store_sha':later},
            producer=self.producer|{'source_sha':sha},
            valid_from='2026-11-04T00:00:00Z',valid_until='2026-11-04T02:00:00Z')
        forged_oid=S._commit(replica,later,{D.RECEIPT:forged})
        W.git(replica,'update-ref',D.refs(sha,D.commitment(forged))[1],forged_oid)
        with self.assertRaises(D.DeliveryHold):
            D.build_delivery(compute=compute,store=replica,compute_sha=sha,
                approval=D.commitment(forged),delivery=self.ctx,now=now)
        W.git(replica,'update-ref','-d',claim_ref)
        with self.assertRaises(D.DeliveryHold):
            D.build_delivery(compute=compute,store=replica,compute_sha=sha,
                approval=D.commitment(raw),delivery=self.ctx,now=now)
