"""Execute the exact scheduled invocation step with a synthetic clock and pins."""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ScheduledCloseInvocationTests(unittest.TestCase):
    def resolve(self, raw_time, cron, *, event='schedule', manual_date=''):
        workflow = yaml.safe_load(
            (ROOT / '.github/workflows/market-data-collector-runtime.yml').read_text()
        )
        step = next(s for s in workflow['jobs']['collect']['steps'] if s.get('id') == 'invocation')
        fixed_time = datetime.fromisoformat(raw_time)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_time.astimezone(tz) if tz else fixed_time.replace(tzinfo=None)

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / 'outputs'
            pin = directory / 'market-data-store-input-sha'
            env = dict(EVENT_NAME=event, EVENT_SCHEDULE=cron,
                       MANUAL_MODE='close', MANUAL_TRADE_DATE=manual_date,
                       GITHUB_OUTPUT=str(output), RUNNER_TEMP=name)
            with patch.dict(os.environ, env, clear=True), patch('datetime.datetime', Clock), \
                    patch.object(subprocess, 'check_output', side_effect=['a'*40, 'b'*40]), \
                    patch('builtins.print') as printed:
                failure = None
                try:
                    exec(compile(step['run'], 'collector invocation', 'exec'), {})
                except SystemExit as exc:
                    failure = exc.code
            values = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
            record = json.loads(printed.call_args.args[0]) if printed.called else None
            return failure, values, pin.exists(), record

    def test_actual_cross_midnight_is_not_assigned_to_today_or_yesterday(self):
        failure, outputs, pin, record = self.resolve('2026-11-24T00:11:29.609769-05:00', '6 16 * * 1-5')
        self.assertEqual(failure, 'SCHEDULED_CLOSE_BEFORE_INFERRED_NOMINAL')
        self.assertFalse(outputs)
        self.assertFalse(pin)
        self.assertIsNone(record)

    def test_initial_retry_and_final_each_require_own_nominal_minute(self):
        cases = [('6', 'close'), ('21', 'close_retry'), ('36', 'close_retry'), ('51', 'close_final')]
        for minute, mode in cases:
            with self.subTest(minute=minute):
                cron = f'{minute} 16 * * 1-5'
                at = f'2026-11-24T16:{int(minute):02}:00-05:00'
                before = f'2026-11-24T16:{int(minute)-1:02}:59-05:00'
                failure, outputs, pin, _ = self.resolve(before, cron)
                self.assertEqual((failure, outputs, pin), ('SCHEDULED_CLOSE_BEFORE_INFERRED_NOMINAL', {}, False))
                failure, outputs, pin, record = self.resolve(at, cron)
                self.assertIsNone(failure)
                self.assertEqual((outputs['mode'], outputs['trade_date'], pin), (mode, '2026-11-24', True))
                self.assertEqual(record['schedule_delivery_observation']['nominal_to_invocation_observed_seconds'], 0)

    def test_summer_new_york_cutoff_and_late_telemetry_keep_existing_semantics(self):
        ok = self.resolve('2026-07-15T20:09:01+00:00', '6 16 * * 1-5')
        self.assertIsNone(ok[0])
        self.assertEqual(ok[1]['trade_date'], '2026-07-15')
        observation = ok[3]['schedule_delivery_observation']
        self.assertEqual(observation['nominal_et_at_resolved_trade_date'], '2026-07-15T16:06:00-04:00')
        self.assertEqual(observation['nominal_to_invocation_observed_seconds'], 181)
        self.assertTrue(observation['late_more_than_two_minutes'])

    def test_non_close_schedule_and_explicit_manual_previous_date_unchanged(self):
        early = '2026-11-24T00:11:29-05:00'
        pre = self.resolve(early, '20 7 * * 1-5')
        self.assertEqual((pre[0], pre[1]['mode'], pre[1]['trade_date']), (None, 'premarket', '2026-11-24'))
        chosen = self.resolve(early, '', event='workflow_dispatch', manual_date='2026-11-23')
        self.assertEqual((chosen[0], chosen[1]['mode'], chosen[1]['trade_date']), (None, 'close', '2026-11-23'))
        self.assertIsNone(chosen[3]['schedule_delivery_observation'])
