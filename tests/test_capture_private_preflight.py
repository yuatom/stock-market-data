"""Real synthetic budget/source Git guard with private filesystem retention."""
import copy
import hashlib
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_guarded_history_recovery as fixture
from test_raw_response_evidence import request, body
from test_capture_only_response import access
import market_data_collectors as collectors


class CapturePrivatePreflightTests(fixture.RecoveryIntegrationFixture, unittest.TestCase):
    def setUp(self):
        self.assertTrue((fixture.ROOT/'scripts/capture_private_response.py').is_file(), 'capture private preflight absent')
        super().setUp()
        self.runner=importlib.import_module('capture_private_response')
        self.destination=self.root/'private-capture'
        self.req=request();self.req.update(start_date='2026-10-13',end_date='2026-10-16')
        payload=body();payload['values'][0]['datetime']='2026-10-15'
        self.raw=(json.dumps(payload,indent=2)+'\n\n').encode()
        self.writer=self.a.StorePublisher(self.repo,self.base,fixture.DAY,'991-1')
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return fixture.NOW if tz is None else fixture.NOW.astimezone(tz)
        with patch.object(self.h.collectors,'datetime',Clock):
            self.debit=self.a.debit_before_request(self.h,self.writer,self.repo/'data/market-data',fixture.DAY,
                self.config['collector']['budgets']['twelve_data_basic'],clock=lambda:fixture.NOW,
                compute=self.compute,compute_sha=self.compute_sha,deadline=time.monotonic()+60)
        self.after_debit=fixture.git(self.repo,'rev-parse','HEAD')
        self.scope=dict(schema_version=1,mode='capture_only',request=self.req,destination=str(self.destination),
            request_id='991-1',compute_sha=self.compute_sha,execution_day=fixture.DAY,
            valid_from=(fixture.NOW-timedelta(seconds=30)).isoformat(),
            valid_until=(fixture.NOW+timedelta(seconds=30)).isoformat(),request_limit=1,credit_limit=1,
            debit_commit_sha=self.debit.store_commit_sha,budget_blob_sha=self.debit.budget_blob_sha,
            budget_minute=self.debit.minute)

    def call(self,**changes):
        args=dict(scope=self.scope,request=self.req,destination=self.destination,writer=self.writer,
            debit=self.debit,compute=self.compute,compute_sha=self.compute_sha,execution_day=fixture.DAY,
            deadline=time.monotonic()+60,api_key='synthetic-key',access=access(),clock=lambda:fixture.NOW)
        args.update(changes)
        return self.runner.capture_with_private_retention(**args)

    def test_actual_guard_one_request_local_retention_and_no_market_write(self):
        calls=[]
        def transport(url,**kw):
            calls.append(url)
            self.assertTrue((self.destination/'attempt.json').exists())
            self.assertEqual(self.current_budget()['credits'],1)
            return self.raw
        with patch.object(collectors,'_http_raw',side_effect=transport): result=self.call()
        self.assertEqual(len(calls),1)
        self.assertEqual(result['status'],'CAPTURE_RETAINED_UNQUALIFIED')
        self.assertFalse(result['retention_durability_authenticated'])
        self.assertEqual((self.destination/'response.raw').read_bytes(),self.raw)
        self.assertEqual(result['raw_response_sha256'],hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(json.loads((self.destination/'attempt.json').read_bytes())['scope_sha256'],result['scope_sha256'])
        self.assertEqual(fixture.git(self.repo,'rev-parse','HEAD'),self.after_debit)
        self.assert_no_remote_facts()
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('retry forbidden')):
            with self.assertRaises(Exception): self.call()

    def test_scope_drift_or_expiry_stops_before_destination_or_transport(self):
        cases=[{'request_limit':2},{'credit_limit':0},{'unknown':'field'},
            {'compute_sha':'a'*40},{'debit_commit_sha':'b'*40},{'budget_blob_sha':'c'*40},
            {'budget_minute':'2026-10-18T12:01Z'},{'execution_day':'2026-10-19'},
            {'valid_until':fixture.NOW.isoformat()},
            {'valid_from':(fixture.NOW+timedelta(seconds=1)).isoformat()},
            {'destination':str(self.root/'other')}, {'request_id':'different'}]
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('transport forbidden')):
            for change in cases:
                with self.subTest(change=change),self.assertRaises(self.runner.CaptureBoundaryError):
                    self.call(scope=dict(self.scope,**change))
                self.assertFalse(self.destination.exists())
            changed=dict(self.req,dp=11)
            with self.assertRaises(self.runner.CaptureBoundaryError): self.call(request=changed)
        self.assertFalse(self.destination.exists())

    def test_official_compute_drift_burns_attempt_and_stops_before_http(self):
        (self.compute/'dirty.txt').write_text('synthetic drift')
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('transport forbidden')):
            with self.assertRaises(Exception): self.call()
        self.assertTrue((self.destination/'attempt.json').exists())
        self.assertFalse((self.destination/'response.raw').exists())
        self.assertEqual(self.current_budget()['credits'],1)

    def test_official_budget_blob_drift_and_expired_minute_still_block(self):
        path=self.repo/self.writer.budget_relative
        path.write_text(path.read_text()+' ')
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('transport forbidden')):
            with self.assertRaises(Exception): self.call()
        self.assertFalse((self.destination/'response.raw').exists())
        fixture.git(self.repo,'checkout','--',self.writer.budget_relative)
        later=fixture.NOW.replace(second=58)
        scope=dict(self.scope,destination=str(self.root/'later'),valid_until=(later+timedelta(seconds=30)).isoformat())
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('transport forbidden')):
            with self.assertRaises(Exception): self.call(scope=scope,destination=self.root/'later',clock=lambda:later)
        self.assertFalse((self.root/'later/response.raw').exists())

    def test_accepted_malformed_body_is_retained_without_completion_or_retry(self):
        with patch.object(collectors,'_http_raw',return_value=b'{invalid') as http:
            with self.assertRaises(Exception): self.call()
            self.assertEqual(http.call_count,1)
        self.assertEqual((self.destination/'response.raw').read_bytes(),b'{invalid')
        self.assertFalse((self.destination/'completion.json').exists())
        with patch.object(collectors,'_http_raw',side_effect=AssertionError('retry forbidden')):
            with self.assertRaises(Exception): self.call()

    def test_scope_cannot_expire_during_guard_then_allow_http(self):
        ticks=[fixture.NOW]*3+[fixture.NOW+timedelta(seconds=31)]*10
        def clock(): return ticks.pop(0)
        original=self.a.guard_provider_request
        def guard(*args,**kwargs):
            original(*args,**kwargs)
            ticks[:]=[fixture.NOW+timedelta(seconds=31)]*10
        with patch.object(self.a,'guard_provider_request',side_effect=guard),patch.object(collectors,'_http_raw',side_effect=AssertionError('transport forbidden')):
            with self.assertRaises(Exception):self.call(clock=clock)
        self.assertFalse((self.destination/'response.raw').exists())

    def test_transport_error_retains_no_secret_and_never_refunds(self):
        with patch.object(collectors,'_http_raw',side_effect=RuntimeError('PRIVATE-secret')):
            with self.assertRaises(Exception) as caught:self.call()
        self.assertNotIn('PRIVATE-secret',str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(self.current_budget()['credits'],1)
        self.assertFalse((self.destination/'completion.json').exists())

    def close_fault(self):
        """Inject only the real sink close syscall, not other file lifecycles."""
        original=self.runner.PrivateCaptureRetention.close
        real_close=os.close
        def fault(sink):
            target=sink._fd
            def close(fd):
                real_close(fd)
                if fd==target:raise OSError('PRIVATE-secret-close-failure')
            with patch.object(os,'close',side_effect=close):return original(sink)
        return patch.object(self.runner.PrivateCaptureRetention,'close',fault)

    def test_close_failure_after_completion_is_sanitized_and_not_success(self):
        with self.close_fault(),patch.object(collectors,'_http_raw',return_value=self.raw):
            with self.assertRaises(Exception) as caught:self.call()
        self.assertNotIn('PRIVATE-secret-close-failure',str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue((self.destination/'completion.json').exists())
        self.assertEqual(self.current_budget()['credits'],1)

    def test_close_failure_does_not_mask_existing_transport_failure(self):
        with self.close_fault(),patch.object(collectors,'_http_raw',side_effect=RuntimeError('PRIVATE-transport')):
            with self.assertRaises(Exception) as caught:self.call()
        self.assertEqual(getattr(caught.exception,'phase',None),'transport')
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
