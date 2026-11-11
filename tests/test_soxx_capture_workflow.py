import unittest,yaml
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SoxxWorkflowTests(unittest.TestCase):
    def test_manual_only_same_repository_lock_and_no_artifacts(self):
        path=ROOT/'.github/workflows/soxx-capture-only.yml'
        self.assertTrue(path.exists(),'official capture workflow missing')
        raw=path.read_text();doc=yaml.safe_load(raw)
        trigger=doc.get('on',doc.get(True))
        self.assertEqual(set(trigger),{'workflow_dispatch'})
        self.assertEqual(doc['concurrency'],{'group':'market-data-collector','cancel-in-progress':False,'queue':'max'})
        self.assertEqual(doc['permissions'],{'contents':'read'})
        self.assertNotIn('upload-artifact',raw)
        self.assertNotIn('continue-on-error',raw)
        self.assertIn('run_soxx_capture.py',raw)

    def test_unverified_private_authority_channel_blocks_before_secrets(self):
        raw=(ROOT/'.github/workflows/soxx-capture-only.yml').read_text();doc=yaml.safe_load(raw)
        trigger=doc.get('on',doc.get(True))
        self.assertEqual(set(trigger['workflow_dispatch']['inputs']),{'expected_compute_sha'})
        self.assertNotIn('secrets.',raw)
        self.assertNotIn('approval_sha256:',raw)
        self.assertIn('PRIVATE_AUTHORITY_CHANNEL_NOT_CONFIGURED',raw)
        self.assertIn('exit 2',raw)
