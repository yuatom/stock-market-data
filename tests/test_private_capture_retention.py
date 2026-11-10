"""Real temporary filesystem tests for create-only private evidence retention."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_raw_response_evidence import request, raw, OBSERVED
from raw_response_evidence import build_envelope

ROOT = Path(__file__).resolve().parents[1]


class PrivateRetentionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT/'scripts/private_capture_retention.py').is_file(), 'private retention implementation absent')
        self.m = importlib.import_module('private_capture_retention')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.compute = self.parent/'compute'; self.compute.mkdir()
        self.store = self.parent/'store'; self.store.mkdir()
        self.destination = self.parent/'capture'

    def sink(self, destination=None):
        value = self.m.PrivateCaptureRetention(destination or self.destination, request(),
            excluded_roots=(self.compute, self.store))
        self.addCleanup(value.close)
        return value

    def test_retains_exact_private_bytes_and_completion_last(self):
        sink = self.sink(); sink.claim_attempt()
        self.assertEqual(sink.retain_raw(raw()), hashlib.sha256(raw()).hexdigest())
        self.assertFalse((self.destination/'completion.json').exists())
        envelope = build_envelope(raw(), request(), OBSERVED)
        self.assertEqual(sink.retain_envelope(envelope), hashlib.sha256(envelope).hexdigest())
        self.assertEqual((self.destination/'response.raw').read_bytes(), raw())
        self.assertEqual((self.destination/'envelope.json').read_bytes(), envelope)
        receipt = json.loads((self.destination/'completion.json').read_bytes())
        self.assertFalse(receipt['hosted_durability_authenticated'])
        self.assertFalse(receipt['market_fact_authority'])
        self.assertEqual(receipt['raw_response_sha256'], hashlib.sha256(raw()).hexdigest())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        for p in self.destination.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(p.name for p in self.destination.iterdir()),
            {'request.json','attempt.json','response.raw','envelope.json','completion.json'})

    def test_requires_attempt_and_never_overwrites_or_resumes_existing_directory(self):
        sink = self.sink()
        with self.assertRaises(self.m.RetentionError): sink.retain_raw(raw())
        with self.assertRaises(self.m.RetentionError): self.sink()
        other = self.sink(self.parent/'other'); other.claim_attempt()
        with self.assertRaises(self.m.RetentionError): other.claim_attempt()
        third = self.sink(self.parent/'third'); third.claim_attempt(); third.retain_raw(raw())
        before = (self.parent/'third/response.raw').read_bytes()
        with self.assertRaises(self.m.RetentionError): third.retain_raw(b'changed')
        self.assertEqual((self.parent/'third/response.raw').read_bytes(), before)

    def test_rejects_public_parent_symlink_and_checkout_overlap(self):
        public = self.parent/'public'; public.mkdir(mode=0o755); public.chmod(0o755)
        link = self.parent/'link'; link.symlink_to(self.parent, target_is_directory=True)
        for dest in (public/'capture', link/'capture', self.compute/'capture', self.store/'capture', self.parent/'..'/'escape'):
            with self.subTest(dest=dest), self.assertRaises(self.m.RetentionError): self.sink(dest)
        self.assertFalse((self.compute/'capture').exists())
        self.assertFalse((self.store/'capture').exists())

    def test_directory_replacement_and_hardlink_are_rejected(self):
        sink = self.sink(); sink.claim_attempt()
        moved = self.parent/'moved'; self.destination.rename(moved); self.destination.mkdir(mode=0o700)
        with self.assertRaises(self.m.RetentionError): sink.retain_raw(raw())
        self.assertFalse((moved/'response.raw').exists())
        second = self.sink(self.parent/'second'); second.claim_attempt(); second.retain_raw(raw())
        os.link(self.parent/'second/response.raw', self.parent/'extra-link')
        with self.assertRaises(self.m.RetentionError): second.retain_envelope(build_envelope(raw(),request(),OBSERVED))
        self.assertFalse((self.parent/'second/completion.json').exists())

    def test_wrong_envelope_or_changed_raw_never_completes(self):
        for kind in ('request','raw'):
            with self.subTest(kind=kind):
                dest = self.parent/kind; sink = self.sink(dest); sink.claim_attempt(); sink.retain_raw(raw())
                req = request()
                if kind == 'request': req['dp'] = 8
                else: (dest/'response.raw').write_bytes(b'changed')
                with self.assertRaises(self.m.RetentionError): sink.retain_envelope(build_envelope(raw(),req,OBSERVED))
                self.assertFalse((dest/'completion.json').exists())

    def test_unknown_fsync_failure_stops_without_acknowledgment_or_retry(self):
        sink = self.sink(); sink.claim_attempt()
        with patch.object(os, 'fsync', side_effect=OSError('PRIVATE-secret')):
            with self.assertRaises(self.m.RetentionError) as caught: sink.retain_raw(raw())
        self.assertNotIn('PRIVATE-secret',str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(self.m.RetentionError): sink.retain_raw(raw())
        self.assertFalse((self.destination/'completion.json').exists())

    def test_empty_oversize_and_wrong_type_fail_before_file_write(self):
        for n,value in enumerate((b'', b'x'*(4*1024*1024+1), 'text')):
            sink = self.sink(self.parent/str(n)); sink.claim_attempt()
            with self.assertRaises(self.m.RetentionError): sink.retain_raw(value)
            self.assertFalse((self.parent/str(n)/'response.raw').exists())

    def test_two_creators_only_one_can_claim_destination(self):
        import concurrent.futures
        def create():
            try:
                sink=self.m.PrivateCaptureRetention(self.destination,request(),excluded_roots=(self.compute,self.store))
                sink.claim_attempt(); sink.close(); return True
            except self.m.RetentionError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:create(),range(2)))
        self.assertEqual(sorted(results),[False,True])

    def test_failed_duplicate_transition_cannot_continue_to_raw_write(self):
        sink=self.sink();sink.claim_attempt()
        with self.assertRaises(self.m.RetentionError):sink.claim_attempt()
        with self.assertRaises(self.m.RetentionError):sink.retain_raw(raw())
        self.assertFalse((self.destination/'response.raw').exists())

    def test_create_only_file_collision_and_request_tamper_do_not_complete(self):
        sink=self.sink();sink.claim_attempt()
        (self.destination/'response.raw').write_bytes(b'preexisting')
        with self.assertRaises(self.m.RetentionError):sink.retain_raw(raw())
        self.assertEqual((self.destination/'response.raw').read_bytes(),b'preexisting')
        other=self.sink(self.parent/'tamper');other.claim_attempt()
        (self.parent/'tamper/request.json').write_bytes(b'changed')
        with self.assertRaises(self.m.RetentionError):other.retain_raw(raw())
        self.assertFalse((self.parent/'tamper/response.raw').exists())

    def test_double_slash_alias_cannot_bypass_protected_checkout(self):
        for root in (self.compute,self.store):
            root.chmod(0o700)
            alias=Path('/'+str(root/'capture'))
            with self.subTest(root=root),self.assertRaises(self.m.RetentionError):self.sink(alias)
            self.assertFalse((root/'capture').exists())
