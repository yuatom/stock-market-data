from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import summarize_collector_failure as summary


class CollectorFailureSummaryTest(unittest.TestCase):
    def summarize(self, data, exit_code=1):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stderr.txt"
            path.write_bytes(data)
            return summary.summarize_failure(exit_code, path)

    def test_fixed_exception_vocabulary_does_not_echo_details(self):
        result = self.summarize(b'Traceback (most recent call last):\nmarket_data_collection.CoverageContractError: private-detail\n')
        self.assertEqual(result["observed_exception_class"], "CoverageContractError")
        self.assertNotIn("private-detail", json.dumps(result))
        self.assertEqual(set(result), {"status", "exit_code", "observed_exception_class", "error_read_status", "error_tail_truncated"})

    def test_unknown_or_embedded_exception_text_is_not_promoted(self):
        for text in (b'UnlistedError: private-detail\n', b'  CoverageContractError: private-detail\n',
                     b'ValueError: first\nprivate-detail\n', b'other.CoverageContractError: detail\n'):
            with self.subTest(text=text):
                self.assertEqual(self.summarize(text)["observed_exception_class"], "unknown")

    def test_only_terminal_exception_name_not_prior_chain_is_selected(self):
        result = self.summarize(b'ValueError: first\n\nRuntimeError: second\n')
        self.assertEqual(result["observed_exception_class"], "RuntimeError")

    def test_bounded_tail_and_non_utf8_remain_safe(self):
        result = self.summarize(b'private-detail\xff' * 2000 + b'\nValueError: tail-private-detail\n', 23)
        self.assertTrue(result["error_tail_truncated"])
        self.assertEqual(result["exit_code"], 23)
        self.assertEqual(result["observed_exception_class"], "ValueError")
        self.assertNotIn("private-detail", json.dumps(result))

    def test_partial_tail_line_not_treated_as_exception(self):
        result = self.summarize(b'x' * 9000 + b'CoverageContractError: detail')
        self.assertEqual(result["observed_exception_class"], "unknown")

    def test_empty_or_missing_log_has_no_invented_cause(self):
        self.assertEqual(self.summarize(b'')["observed_exception_class"], "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            result = summary.summarize_failure(7, Path(tmp) / 'absent')
        self.assertEqual(result["error_read_status"], "unavailable")
        self.assertEqual(result["exit_code"], 7)

    def test_success_and_invalid_exit_codes_rejected(self):
        for code in (0, -1, 256):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.summarize(b'', code)

    def run_step(self, mode, exit_code, helper_fails=False):
        workflow = yaml.safe_load((ROOT / '.github/workflows/market-data-collector-runtime.yml').read_text())
        step = next(s for s in workflow['jobs']['collect']['steps'] if s['name'] == 'Collect qualified facts into private Store checkout')
        script = step['run']
        with tempfile.TemporaryDirectory() as tmp:
            # Isolate only temporary log paths; retain the real workflow shell.
            script = script.replace('/tmp/collector-output.txt', f'{tmp}/collector-output.txt')
            script = script.replace('/tmp/collector-error.txt', f'{tmp}/collector-error.txt')
            interpreter = shlex.quote(sys.executable)
            setup = f'''python() {{
  if [[ "$1" == 'scripts/summarize_collector_failure.py' ]]; then
    if [[ "${{HELPER_FAILS}}" == '1' ]]; then return 99; fi
    {interpreter} "$@"
  elif [[ "$1" == '-' ]]; then
    {interpreter} "$@"
  else
    if [[ "${{TEST_COLLECTOR_EXIT}}" == '0' ]]; then
      printf '%s\\n' '{{"status":"ok","snapshot_written":true,"private_detail":"not-public"}}'
    else
      printf '%s\\n' 'market_data_collection.CoverageContractError: private-detail' >&2
    fi
    return "${{TEST_COLLECTOR_EXIT}}"
  fi
}}
'''
            env = dict(os.environ, MODE=mode, TRADE_DATE='2026-09-30', PROBE_PATH='test-only',
                       MAINTENANCE_AUTHORIZED='true', TEST_COLLECTOR_EXIT=str(exit_code),
                       HELPER_FAILS='1' if helper_fails else '0')
            return subprocess.run(['bash', '-c', setup + script + '\necho SUBSEQUENT_STEP_REACHED\n'],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)

    def test_each_collector_branch_preserves_nonzero_exit_and_stops(self):
        modes = ['nasdaq_extended_probe', 'premarket', 'premarket_probe_replay',
                 'daily_baseline_init', 'daily_baseline_rebase', 'historical_context_repair']
        for index, mode in enumerate(modes, start=2):
            with self.subTest(mode=mode):
                result = self.run_step(mode, index)
                self.assertEqual(result.returncode, index, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['exit_code'], index)
                self.assertEqual(payload['observed_exception_class'], 'CoverageContractError')
                self.assertNotIn('private-detail', result.stdout + result.stderr)
                self.assertNotIn('SUBSEQUENT_STEP_REACHED', result.stdout)

    def test_helper_failure_still_preserves_collector_failure(self):
        result = self.run_step('historical_context_repair', 17, helper_fails=True)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(json.loads(result.stdout)['summary_status'], 'unavailable')
        self.assertNotIn('SUBSEQUENT_STEP_REACHED', result.stdout)

    def test_success_keeps_existing_summary_and_continues(self):
        result = self.run_step('historical_context_repair', 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0]), {'status': 'ok', 'snapshot_written': True})
        self.assertIn('SUBSEQUENT_STEP_REACHED', result.stdout)
        self.assertNotIn('not-public', result.stdout)


if __name__ == '__main__':
    unittest.main()
