"""Local contract-test launcher must preserve the former workflow command."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/run_contract_tests.sh'

class LocalContractEntrypointTests(unittest.TestCase):
    def test_calls_preflight_and_full_pytest(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'python'
            fake.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "ARG:%s\\n" "$@"\n')
            fake.chmod(0o755)
            result = subprocess.run(['/bin/sh', str(ENTRY)], cwd=temporary,
                                    env={'PATH':'/usr/bin:/bin', 'PYTHON':str(fake)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['ARG:-m', 'ARG:pytest', 'ARG:tests', 'ARG:-v'])

    def test_failed_preflight_stops_before_pytest(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'python'
            fake.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 23; fi\nprintf "SHOULD_NOT_RUN\\n"\n')
            fake.chmod(0o755)
            result = subprocess.run(['/bin/sh', str(ENTRY)], cwd=temporary,
                                    env={'PATH':'/usr/bin:/bin', 'PYTHON':str(fake)},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('SHOULD_NOT_RUN', result.stdout)
            self.assertIn('Python 3.12', result.stderr)
