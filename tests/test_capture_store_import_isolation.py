"""Ensure expanded capture test collection cannot replace the canonical Store."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class CaptureStoreImportIsolationTests(unittest.TestCase):
    def test_rebase_and_proof_import_orders_keep_real_validation_observable(self):
        for first,second in [('rebase_market_data_baseline','test_deterministic_metric_proof'),
                             ('test_deterministic_metric_proof','rebase_market_data_baseline')]:
            with self.subTest(first=first):
                script='\n'.join([
                    'import importlib,sys,unittest',
                    f'sys.path[:0]={[str(ROOT/"scripts"),str(ROOT/"tests")]!r}',
                    f'importlib.import_module({first!r})',
                    f'importlib.import_module({second!r})',
                    'names=["test_daily_baseline_rebase_window.DailyBaselineRebaseWindowTests.test_every_selected_bar_uses_canonical_store_validation",',
                    '"test_replacement_rejection_diagnostics.UnexpectedFailureTests.test_unexpected_normalizer_and_canonical_failures_are_unclassified",',
                    '"test_value_free_revision_diagnostics.ValueFreeRevisionTests.test_per_field_serialization_failure_retains_original_rejection"]',
                    'suite=unittest.defaultTestLoader.loadTestsFromNames(names)',
                    'result=unittest.TextTestRunner().run(suite)',
                    'raise SystemExit(0 if result.wasSuccessful() else 1)',
                ])
                result=subprocess.run([sys.executable,'-B','-c',script],cwd=ROOT,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
