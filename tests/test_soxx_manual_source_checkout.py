"""Real local clones exercise unchanged source guard; no network/provider calls."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml
from scripts import run_private_authority_route as source
from scripts import run_one_shot_soxx_basis_gap as manual

ROOT=Path(__file__).resolve().parents[1]

def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()

class ManualSourceCheckoutTests(unittest.TestCase):
    def test_actual_shallow_rejected_full_clone_accepted_by_same_source_guard(self):
        self.assertIs(manual._source,source._source)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();origin=root/'origin';origin.mkdir()
            git(origin,'init','-q','-b','main');git(origin,'config','user.name','test');git(origin,'config','user.email','test@example.invalid')
            for n in range(2):
                (origin/'source.txt').write_text(str(n));git(origin,'add','source.txt');git(origin,'commit','-qm','synthetic source')
            sha=git(origin,'rev-parse','HEAD')
            shallow=root/'shallow';full=root/'full'
            subprocess.run(['git','clone','-q','--depth=1',origin.as_uri(),str(shallow)],check=True)
            subprocess.run(['git','clone','-q',origin.as_uri(),str(full)],check=True)
            for repo in (shallow,full):git(repo,'remote','set-url','origin',source.COMPUTE_URL)
            self.assertTrue((shallow/'.git/shallow').is_file());self.assertFalse((full/'.git/shallow').exists())
            self.assertEqual(git(shallow,'rev-parse','HEAD'),sha);self.assertEqual(git(full,'rev-parse','HEAD'),sha)
            # Only the network remote-main read is replaced. Every local config,
            # source identity/cleanliness/history check runs real production code.
            with patch.object(source,'_remote_main',return_value=sha) as remote:
                with self.assertRaises(ValueError):manual._source(shallow,sha)
                remote.assert_not_called()
                manual._source(full,sha)
                remote.assert_called_once_with(full)

    def test_workflow_compute_exact_pin_and_full_history(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/one-shot-soxx-basis-gap.yml').read_text())
        checkouts=[x['with'] for x in workflow['jobs']['soxx_gap']['steps'] if x.get('uses')=='actions/checkout@v4']
        compute=next(x for x in checkouts if x['path']=='compute')
        self.assertEqual(compute['ref'],'${{ inputs.expected_compute_sha }}')
        self.assertEqual(compute['fetch-depth'],0);self.assertIs(compute['persist-credentials'],False)
        store=next(x for x in checkouts if x['path']=='store')
        self.assertEqual(store['fetch-depth'],0);self.assertIs(store['persist-credentials'],False)

if __name__=='__main__':unittest.main()
