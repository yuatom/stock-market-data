"""Run the real Persist shell body + quiet wrapper + proof verifier offline."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_deterministic_metric_proof import _seed_intraday_stage


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/market-data-collector-runtime.yml'
PROOF = 'build_deterministic_metric_proof.py'
SCRIPTS = (
    PROOF, 'market_data_store.py', 'quiet_private_store.py',
    'run_private_authority_route.py', 'private_authority_materialize.py',
    'private_authority_preflight.py', 'private_authority_input.py',
    'private_authority_promotion.py', 'private_authority_receipt.py',
    'private_authority_receipt_transport.py', 'private_authority_transport.py',
    'eleven_symbol_append.py', 'raw_response_evidence.py',
)
PRIVATE_TEXT = 'opaque-synthetic-do-not-emit'
STORE_ORIGIN = 'https://github.com/yuatom/stock-market-data-store.git'


def persist_program():
    body = WORKFLOW.read_text().split('      - name: Persist private Store only\n', 1)[1]
    body = body.split('      - name: Verify durable Store publication\n', 1)[0]
    return '\n'.join(line[10:] for line in body.split('        run: |\n', 1)[1].splitlines()) + '\n'


class PrivateStoreProofRecheckTests(unittest.TestCase):
    def invoke(self, *, invalid_proof, push_status):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            scripts = root / 'compute' / 'scripts'
            repo = root / 'store'
            tools = root / 'bin'
            home = root / 'home'
            for folder in (scripts, repo, tools, home):
                folder.mkdir(parents=True)
            for script in SCRIPTS:
                shutil.copyfile(ROOT / 'scripts' / script, scripts / script)
            (tools / 'python').symlink_to(sys.executable)
            real_git = shutil.which('git')
            self.assertIsNotNone(real_git)
            # This guard has no network fallback. The real push helper is not copied.
            git_guard = tools / 'git'
            git_guard.write_text(
                '#!/bin/bash\n'
                'case "$*" in *" push"*|push*|pull*|fetch*|clone*|*ls-remote*) exit 99;; esac\n'
                'exec "$OFFLINE_TEST_GIT" "$@"\n'
            )
            git_guard.chmod(0o700)
            (scripts / 'push_proof_store_commit.py').write_text(
                'from pathlib import Path\nimport sys\n'
                'Path("push-attempted").touch()\n'
                f'print({PRIVATE_TEXT!r}, file=sys.stderr)\n'
                f'raise SystemExit({push_status})\n'
            )
            env = dict(
                PATH=f'{tools}:/usr/bin:/bin', HOME=str(home),
                TMPDIR=str(root), LC_ALL='C', GIT_CONFIG_NOSYSTEM='1',
                GIT_CONFIG_GLOBAL=os.devnull, OFFLINE_TEST_GIT=real_git,
                PYTHONDONTWRITEBYTECODE='1',
                MARKET_DATA_STORE_TOKEN='synthetic_token_never_transmitted',
                MODE='close', TRADE_DATE='2026-09-05',
                MARKET_DATA_CONTRACT_SHA='1' * 40,
            )

            def command(args, *, program=None):
                return subprocess.run(
                    args, cwd=repo, env=env, input=program, timeout=15,
                    text=True, capture_output=True,
                )

            def git(*args):
                result = command(['git', '-c', 'core.hooksPath=/dev/null', *args])
                self.assertEqual(result.returncode, 0)
                return result.stdout.strip()

            git('init', '-q')
            git('remote', 'add', 'origin', STORE_ORIGIN)
            git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'empty seed', '--allow-empty')
            data_root, _ = _seed_intraday_stage(repo, stage='close')
            self.assertEqual(data_root, repo / 'data' / 'market-data')
            args = [
                'python', str(scripts / PROOF), '--store-root', str(data_root),
                '--trade-date', env['TRADE_DATE'], '--stage', env['MODE'],
                '--data-plane-commit-sha', env['MARKET_DATA_CONTRACT_SHA'],
            ]
            self.assertEqual(command(args + ['--print-path']).returncode, 0)
            if invalid_proof:
                pointer = data_root / 'proofs/deterministic-metrics/2026-09/2026-09-05/close/latest.json'
                pointer.write_text('{' + PRIVATE_TEXT)
            result = command(['/bin/bash', '-euo', 'pipefail'], program=persist_program())
            self.assertEqual(git('rev-list', '--count', 'HEAD'), '2')
            self.assertEqual((repo / 'push-attempted').exists(), not invalid_proof)
            self.assertEqual(result.stderr, '')
            self.assertNotIn(PRIVATE_TEXT, result.stdout)
            return result.returncode, result.stdout.strip()

    def test_proof_recheck_failure_is_identified_without_attempting_push(self):
        self.assertEqual(self.invoke(invalid_proof=True, push_status=0),
                         (2, 'PRIVATE_STORE_PROOF_RECHECK_STOPPED'))

    def test_restricted_push_cannot_reuse_reserved_status_or_private_text(self):
        self.assertEqual(self.invoke(invalid_proof=False, push_status=78),
                         (2, 'PRIVATE_STORE_OPERATION_STOPPED'))

    def test_success_preserves_existing_status(self):
        self.assertEqual(self.invoke(invalid_proof=False, push_status=0),
                         (0, 'PRIVATE_STORE_OPERATION_COMPLETE'))


if __name__ == '__main__':
    unittest.main()
