from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
import yaml


class OrdinaryRepairWorkflowBoundary(unittest.TestCase):
    def test_eod_repair_uses_exact_path_and_never_enters_legacy_rebase(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/market-data-collector-runtime.yml').read_text())
        step, = [s for j in workflow['jobs'].values() for s in j.get('steps', ())
                 if s.get('name') == 'Persist private Store only']
        run = step['run']
        block=run.split("<<'PRIVATE_SCRIPT'\n",1)[1].split('\nPRIVATE_SCRIPT',1)[0]
        with TemporaryDirectory() as temp:
            for repair in (False,True):
                with self.subTest(repair=repair):
                    expected='data/market-data/collector-state/2026-11/ordinary-gap-'+'3'*64+'.json'
                    path=expected if repair else 'data/market-data/daily.json'
                    harness=f'''
                    export MODE=previous_session_eod PUBLICATION_MANIFEST_PATH=collector-state/synthetic-publication.json
                    cd "{temp}"
                    git() {{
                      case "$1" in
                        rev-parse) echo {'1'*40};;
                        diff)
                          [[ "$*" != *--quiet* ]] || return 1
                          printf '%s\n' '{path}';;
                        pull) echo LEGACY_REBASE;;
                        push) :;;
                      esac
                    }}
                    python() {{ echo "EXACT_PUSH $*"; }}
                    '''
                    output=subprocess.check_output(['bash','-c',harness+'\n'+block],text=True)
                    self.assertEqual('EXACT_PUSH' in output,repair)
                    self.assertEqual('LEGACY_REBASE' in output,not repair)
