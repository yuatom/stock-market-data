"""Synthetic exceptions only; no collection, Git or network."""
import contextlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import run_fixed_guarded_candidate as runner
class FailureSummaryTests(unittest.TestCase):
    def test_runner_distinguishes_timeout_and_value_error_without_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'invocation.json';path.write_text('{}')
            for exc,name in [(TimeoutError('SECRET payload'),'TimeoutError'),(ValueError('SECRET token'),'ValueError')]:
                out=io.StringIO()
                with patch.dict(runner.os.environ,{'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}),patch.object(runner,'run',side_effect=exc),contextlib.redirect_stdout(out):
                    self.assertEqual(runner.main(['--invocation',str(path),'--','--store-root','/synthetic/data/market-data']),2)
                value=json.loads(out.getvalue());self.assertEqual(value['exception_class'],name)
                self.assertEqual(value['failure_stage'],'runner_execution')
                self.assertNotIn('SECRET',out.getvalue())
    def test_unknown_exception_name_is_not_printed(self):
        from fixed_failure_summary import summary
        SecretException=type('SECRET_DYNAMIC_CLASS',(Exception,),{})
        value=summary(SecretException('SECRET'),'SECRET_DYNAMIC_STAGE')
        self.assertEqual(value['exception_class'],'unknown');self.assertEqual(value['failure_stage'],'unknown')
        self.assertNotIn('SECRET',json.dumps(value))
    def test_public_reader_rejects_arbitrary_file_fields(self):
        from fixed_failure_summary import read_summary
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'summary.json';path.write_text(json.dumps(dict(status='SECRET',exception_class='SECRET',failure_stage='SECRET',message='SECRET')))
            value=read_summary(path)
            self.assertNotIn('SECRET',json.dumps(value))
            self.assertEqual(value['exception_class'],'unknown')
    def test_same_name_in_unrecognized_module_is_unknown(self):
        from fixed_failure_summary import summary
        Spoof=type('ValueError',(Exception,),{'__module__':'SECRET_MODULE'})
        self.assertEqual(summary(Spoof('SECRET'),'collection')['exception_class'],'unknown')
    def test_real_input_failure_phase_and_private_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'invocation.json';outpath=Path(tmp)/'failure.json'
            path.write_text(json.dumps(dict(mode='open_15m',trade_date='2026-11-17',market_data_contract_sha='a'*40)))
            out=io.StringIO()
            with patch.dict(runner.os.environ,{'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1','MARKET_DATA_STORE_TOKEN':'SYNTHETIC'}),patch.object(runner,'git_environment',return_value={}),patch.object(runner,'check_store_remote',side_effect=TimeoutError('SECRET_REMOTE_URL')),contextlib.redirect_stdout(out):
                code=runner.main(['--invocation',str(path),'--failure-summary',str(outpath),'--','--mode','open_15m','--trade-date','2026-11-17','--store-root','/synthetic/data/market-data'])
            self.assertEqual(code,2)
            value=json.loads(outpath.read_bytes());self.assertEqual(value['failure_stage'],'input_validation');self.assertEqual(value['exception_class'],'TimeoutError')
            self.assertEqual(outpath.stat().st_mode&0o777,0o600)
            self.assertNotIn('SECRET',out.getvalue()+outpath.read_text())
    def test_workflow_emits_only_sanitized_summary_not_private_log(self):
        import yaml
        workflow=yaml.safe_load((Path(runner.ROOT)/'.github/workflows/market-data-collector-runtime.yml').read_text())
        step=next(s for s in workflow['jobs']['collect']['steps'] if s['name']=='Collect and publish guarded fixed windows')
        self.assertIn('python scripts/fixed_failure_summary.py --summary',step['run'])
        self.assertNotIn('cat "$RUNNER_TEMP/fixed-collector-private.log"',step['run'])
        self.assertNotIn('continue-on-error',step)
    def test_reader_bounds_size_and_unknown_values(self):
        from fixed_failure_summary import read_summary
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'s.json'
            for raw in ['x'*5000,'not json',json.dumps(dict(exception_class=['SECRET'],failure_stage='collection'))]:
                path.write_text(raw)
                self.assertEqual(read_summary(path)['exception_class'],'unknown')
