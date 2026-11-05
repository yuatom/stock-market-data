"""Local contract-test launcher must preserve the former workflow command."""
import os
from pathlib import Path
import subprocess
import sys
import shlex
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

    def test_optimized_python_cannot_bypass_unsupported_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'python'
            for setting,flag in [(None,''),('0',''),('1',''),(None,'-O')]:
                fake.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exec ' + shlex.quote(sys.executable) +
                    ' ' + flag + ' -c "import sys; sys.version_info=(3,10,0); $2"; fi\nprintf "PYTEST_MUST_NOT_RUN\\n"\n')
                fake.chmod(0o755)
                env={'PATH':'/usr/bin:/bin','PYTHON':str(fake)}
                if setting is not None: env['PYTHONOPTIMIZE']=setting
                result = subprocess.run(['/bin/sh', str(ENTRY), '--version'], cwd=temporary,
                    env=env, capture_output=True, text=True)
                with self.subTest(setting=setting,flag=flag):
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn('PYTEST_MUST_NOT_RUN', result.stdout)

    def test_pytest_exit_codes_propagate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / 'python'
            fake.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nexit "$TEST_EXIT"\n')
            fake.chmod(0o755)
            for code in (1, 2, 5):
                result=subprocess.run(['/bin/sh',str(ENTRY)],cwd=temporary,
                    env={'PATH':'/usr/bin:/bin','PYTHON':str(fake),'TEST_EXIT':str(code)},capture_output=True,text=True)
                with self.subTest(code=code): self.assertEqual(result.returncode,code)
