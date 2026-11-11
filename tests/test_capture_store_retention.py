"""Private Store capture persistence tested only against synthetic local Git."""
import hashlib
import importlib
import unittest
from unittest.mock import patch
import test_guarded_history_recovery as f

class CaptureStoreTests(f.RecoveryIntegrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue((f.ROOT/'scripts/capture_store_retention.py').exists(),'private Store adapter missing')
        self.m=importlib.import_module('capture_store_retention')
        self.grant='a'*64

    def write(self, **changes):
        args=dict(repo=self.repo,expected_commit=self.base,approval_sha256=self.grant,
            symbol='AAA',name='response.raw',payload=b'{"synthetic":true}',execution_day=f.DAY,
            request_id='994-1', forbidden_bytes=(b'synthetic-secret',))
        args.update(changes)
        return self.m.retain_capture_object(**args)

    def test_create_only_exact_private_bytes_and_durable_commit(self):
        result=self.write()
        path='capture-evidence/'+self.grant+'/AAA/response.raw'
        self.assertEqual(f.git(self.repo,'show',result['store_commit_sha']+':'+path),'{"synthetic":true}')
        self.assertEqual(result['sha256'],hashlib.sha256(b'{"synthetic":true}').hexdigest())
        self.assertEqual(f.git(self.store_remote,'rev-parse','refs/tags/market-data-read/'+result['store_commit_sha']),result['store_commit_sha'])
        self.assertEqual(f.git(self.repo,'diff','--name-only',self.base,result['store_commit_sha']),path)
        with self.assertRaises(self.m.CaptureStoreError): self.write(expected_commit=result['store_commit_sha'])
        self.assertEqual(f.git(self.repo,'diff','--name-only',self.base,result['store_commit_sha'],'--','data/market-data'),'')

    def test_reject_arbitrary_paths_credentials_and_oversize_before_commit(self):
        cases=[{'name':'../../market.json'},{'symbol':'../AAA'}, {'approval_sha256':'bad'},
            {'payload':b'synthetic-secret'}, {'payload':b'x'*(4*1024*1024+1)}, {'name':'history.json'}]
        for change in cases:
            with self.subTest(change=str(change)[:100]),self.assertRaises(self.m.CaptureStoreError):self.write(**change)
            self.assertEqual(f.git(self.repo,'rev-parse','HEAD'),self.base)

    def test_outside_dirty_change_prevents_any_publication(self):
        (self.repo/'unrelated.txt').write_text('no')
        with self.assertRaises(self.m.CaptureStoreError):self.write()
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.base)

    def test_symlink_parent_prevents_write(self):
        (self.repo/'capture-evidence').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(self.m.CaptureStoreError):self.write()
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.base)

    def test_uncertain_push_is_not_retried(self):
        real=self.a.git
        attempts=[]
        def git(repo,*args):
            if 'push' in args:
                attempts.append(args)
                raise RuntimeError('secret')
            return real(repo,*args)
        with patch.object(self.a,'git',side_effect=git):
            with self.assertRaises(self.m.CaptureStoreError) as caught:self.write()
        self.assertEqual(len(attempts),1)
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),self.base)

    def test_chain_claim_request_attempt_raw_terminal_without_market_changes(self):
        head=self.base
        for name in ('claim.json','request.json','attempt.json','response.raw','envelope.json','terminal.json','completion.json'):
            result=self.write(expected_commit=head,name=name)
            self.assertNotEqual(result['store_commit_sha'],head)
            head=result['store_commit_sha']
        self.assertEqual(f.git(self.repo,'diff','--name-only',self.base,head,'--','data/market-data'),'')
        with self.assertRaises(self.m.CaptureStoreError):self.write(expected_commit=self.base,name='request.json')

    def test_retention_failure_after_push_remains_uncertain_without_retry(self):
        import verify_store_publication as publication
        original=publication._PublicationTransport.once
        attempts=[]
        def fail_tag(instance,*args):
            if 'push' in args:
                attempts.append(args)
                raise RuntimeError('private')
            return original(instance,*args)
        with patch.object(publication._PublicationTransport,'once',new=fail_tag):
            with self.assertRaises(self.m.CaptureStoreError):self.write()
            self.assertEqual(len(attempts),1)
        head=f.git(self.store_remote,'rev-parse','main')
        self.assertNotEqual(head,self.base)
        with self.assertRaises(self.m.CaptureStoreError):self.write(expected_commit=head)

    def test_attributes_cannot_transform_literal_evidence_bytes(self):
        (self.repo/'.git/info/attributes').write_text('capture-evidence/** text eol=lf\n')
        raw=b'{"synthetic":true}\r\n'
        result=self.write(payload=raw)
        import subprocess
        actual=subprocess.check_output(['git','-C',str(self.store_remote),'cat-file','blob',result['blob_sha']])
        self.assertEqual(actual,raw)

    def test_commit_hook_cannot_run_or_add_unrelated_bytes(self):
        hook=self.repo/'.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf altered > unrelated.txt\ngit add unrelated.txt\n')
        hook.chmod(0o700)
        result=self.write()
        self.assertFalse((self.repo/'unrelated.txt').exists())
        self.assertEqual(f.git(self.repo,'diff','--name-only',self.base,result['store_commit_sha']),result['path'])

    def test_prepush_hook_is_not_run_for_main_or_retention_tag(self):
        hook=self.repo/'.git/hooks/pre-push'
        hook.write_text('#!/bin/sh\nprintf leaked > prepush-marker.txt\nexit 1\n')
        hook.chmod(0o700)
        result=self.write()
        self.assertFalse((self.repo/'prepush-marker.txt').exists())
        self.assertEqual(f.git(self.store_remote,'rev-parse','main'),result['store_commit_sha'])

    def test_reference_transaction_hook_is_not_run(self):
        hook=self.repo/'.git/hooks/reference-transaction'
        hook.write_text('#!/bin/sh\nprintf leaked > reference-marker.txt\n')
        hook.chmod(0o700)
        self.write()
        self.assertFalse((self.repo/'reference-marker.txt').exists())

    def test_fsmonitor_command_is_not_run(self):
        monitor=self.root/'monitor.sh'
        monitor.write_text('#!/bin/sh\nprintf leaked > fsmonitor-marker.txt\n')
        monitor.chmod(0o700)
        f.git(self.repo,'config','core.fsmonitor',str(monitor))
        self.write()
        self.assertFalse((self.repo/'fsmonitor-marker.txt').exists())
