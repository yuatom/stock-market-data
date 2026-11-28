from pathlib import Path
from dataclasses import replace
from base64 import b64decode,b64encode
from scripts.daily_basis_delivery import MARKER
from tempfile import TemporaryDirectory
from unittest import TestCase
from scripts import provider_basis_snapshot as B
from scripts.daily_basis_store import LocalGitStore
from scripts.hosted_v2_durable_candidate import record_preproof, read_preproof
from test_hosted_verifier_candidate import fixture, git

def retain_real_receipt(store,x):
    expected,run,jobs,log,identity=x['ready']
    selector=identity[2]
    raw=B.encode(dict(schema_version=1,kind='basis_consumer_receipt',compute_sha=expected.compute_sha,
        selector=selector,admission_blob_sha=selector['admission_blob_sha'],nonce='e'*64,
        valid_from='2026-01-07T14:00:00Z',valid_until='2026-01-07T14:16:00Z'))
    digest=B.receipt_commitment(raw)
    ref=f'refs/heads/basis-read-receipts/{expected.compute_sha}/{digest}'
    oid=store.commit(store.head(),{'basis-authority/receipt.json':raw},replace=set())
    store.cas(ref,oid,'0'*40)
    blob=B.blob_sha(raw)
    value=B._parse(b64decode(log['text'][len(MARKER):]))
    value.update(receipt_ref=ref,receipt_commitment=digest,receipt_commit=oid,receipt_blob_sha=blob)
    x['ready']=(replace(expected,receipt_commitment=digest),run,jobs,
        dict(log,text=MARKER+b64encode(B.encode(value)).decode()),(oid,blob,selector))

class DurableHostedTest(TestCase):
    def test_real_create_only_then_fresh_read_and_expiry(self):
        with TemporaryDirectory() as scratch:
            x=fixture(scratch)
            git(scratch,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
            git(scratch,'branch','-M','main')
            store=LocalGitStore(Path(scratch).resolve())
            git(scratch,"update-ref","refs/heads/basis-consumer-delivery/"+x["ready"][0].workflow_source_sha, x["ready"][0].workflow_source_sha)
            with store.lock(): retain_real_receipt(store,x)
            expected,_,_,_,identity=x['ready']
            attempt='refs/heads/basis-daily-readiness-attempt/'+expected.compute_sha+'/'+expected.receipt_commitment
            now=B._stamp(x['provider_gate_at'])
            request='51-1'
            with store.lock():
                parent=store.head()
                claim=store.commit(parent,{'basis-authority/readiness-attempt.json':B.encode(dict(
                    schema_version=1,kind='scheduled_basis_readiness_attempt',request_id=request,
                    receipt_commit=identity[0],claimed_at='2026-01-07T14:00:58Z'))},replace=set())
                store.cas(attempt,claim,'0'*40)
                call=dict(profile=x['profile'],repository=x['repository'],ready=x['ready'],
                    anchored_workflow_blob=x['anchored_workflow_blob'],
                    ready_dispatched_at=x['ready_dispatched_at'],
                    request_id=request)
                pin=record_preproof(store=store,clock=lambda:now,**call)
                self.assertEqual(store.head(),parent)
                with self.assertRaises(B.BasisError): record_preproof(store=store,clock=lambda:now,**call)
            reopen=LocalGitStore(Path(scratch).resolve())
            with reopen.lock():
                proof=read_preproof(store=reopen,expected=expected,expected_pin=pin,request_id=request)
                self.assertEqual(proof['receipt_commit'],identity[0])
                self.assertEqual(proof['job_id'],52)
                with self.assertRaises(B.BasisError):
                    read_preproof(store=reopen,expected=expected,expected_pin='f'*40,request_id=request)
                with self.assertRaises(B.BasisError):
                    read_preproof(store=reopen,expected=expected,expected_pin=pin,request_id='other')

    def test_no_proof_for_mixed_profile_or_expired_gate(self):
        with TemporaryDirectory() as scratch:
            x=fixture(scratch)
            git(scratch,'remote','add','origin','https://github.com/yuatom/stock-market-data-store')
            git(scratch,'branch','-M','main')
            store=LocalGitStore(Path(scratch).resolve())
            git(scratch,"update-ref","refs/heads/basis-consumer-delivery/"+x["ready"][0].workflow_source_sha, x["ready"][0].workflow_source_sha)
            with store.lock(): retain_real_receipt(store,x)
            expected,_,_,_,identity=x['ready']
            attempt='refs/heads/basis-daily-readiness-attempt/'+expected.compute_sha+'/'+expected.receipt_commitment
            with store.lock():
                claim=store.commit(store.head(),{'basis-authority/readiness-attempt.json':B.encode(dict(
                    schema_version=1,kind='scheduled_basis_readiness_attempt',request_id='51-1',
                    receipt_commit=identity[0],claimed_at='2026-01-07T14:00:58Z'))},replace=set())
                store.cas(attempt,claim,'0'*40)
                opts=dict(profile=x['profile'],repository=x['repository'],ready=x['ready'],
                    anchored_workflow_blob=x['anchored_workflow_blob'],
                    ready_dispatched_at=x['ready_dispatched_at'],
                    request_id='51-1')
                for changes in ({'clock': lambda: B._stamp('2026-01-07T14:08:01Z')},
                                {'anchored_workflow_blob':'1'*40}):
                    with self.assertRaises(B.BasisError):
                        record_preproof(**(dict(opts,store=store,clock=lambda:B._stamp(x['provider_gate_at']))|changes))
                self.assertIsNone(store.ref(attempt.replace('-attempt/', '-proof/')))
