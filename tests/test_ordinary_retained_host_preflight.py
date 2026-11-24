"""Hosted ordinary retained route smoke; never dispatches or touches Store."""
from pathlib import Path
import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FILE = ".github/workflows/reconcile-retained-daily.yml"
REPOSITORY = "yuatom/stock-market-data"
COMMITMENT = "b"*64
INPUT = "ordinary_retained_commitment"
WORKFLOW_REF = REPOSITORY+"/"+FILE+"@refs/heads/main"


class HostedRetained(unittest.TestCase):
    def read(self):
        return yaml.load((ROOT/FILE).read_text(), Loader=yaml.BaseLoader)

    def test_dispatch_selectors_are_exclusive_on_existing_queue(self):
        w=self.read()
        self.assertEqual(set(w["on"]),{"workflow_dispatch"})
        inputs=w["on"]["workflow_dispatch"]["inputs"]
        self.assertIn(INPUT,inputs)
        self.assertEqual(set(inputs),{"expected_compute_sha","request_json",INPUT})
        self.assertTrue(all(i["required"]=="false" for i in inputs.values()))
        original=w["jobs"]["reconcile"]
        new=w["jobs"]["ordinary_retained_preflight"]
        self.assertEqual(original["concurrency"],new["concurrency"])
        self.assertEqual(new["concurrency"]["group"],"market-data-collector")
        self.assertEqual(new["concurrency"]["cancel-in-progress"],"false")
        self.assertEqual(new["concurrency"]["queue"],"max")
        self.assertIn("inputs."+INPUT+" == ''",original["if"])
        self.assertIn("inputs."+INPUT+" != ''",new["if"])
        self.assertIn("github.actor == 'yuatom'",new["if"])
        self.assertIn("github.triggering_actor == 'yuatom'",new["if"])
        self.assertIn("github.run_attempt == '1'",new["if"])
        self.assertIn("Validate exact append request before private checkouts",
                      [s.get("name") for s in original["steps"]])
        self.assertTrue(any("scripts/reconcile_retained_daily.py" in s.get("run","")
                            and "--request-json" in s["run"] for s in original["steps"]))
        self.assertEqual(w["permissions"],{"contents":"read"})
        yaml_job=yaml.dump(new)
        self.assertNotIn("secrets.",yaml_job)
        self.assertNotIn("schedule:",yaml_job)
        self.assertNotIn("request_json",yaml_job)

    def test_actual_cli_fails_closed_without_registered_issuer(self):
        w=self.read()
        new=w["jobs"]["ordinary_retained_preflight"]
        [command]=[s["run"] for s in new["steps"] if "run" in s]
        self.assertEqual(command,"python -B -m scripts.run_ordinary_retained_preflight")
        pin="a"*40
        fixture_env=dict(PATH=os.defpath,GITHUB_ACTIONS="true",GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_REPOSITORY=REPOSITORY,GITHUB_REPOSITORY_OWNER="yuatom",GITHUB_ACTOR="yuatom",
            GITHUB_TRIGGERING_ACTOR="yuatom",GITHUB_REF="refs/heads/main",GITHUB_SHA=pin,
            GITHUB_WORKFLOW_SHA=pin,GITHUB_WORKFLOW_REF=WORKFLOW_REF,GITHUB_RUN_ID="45123",
            GITHUB_RUN_ATTEMPT="1")
        base={"ref":"refs/heads/main","repository":{"full_name":REPOSITORY},"sender":{"login":"yuatom"},
              "inputs":{INPUT:COMMITMENT,"request_json":"","expected_compute_sha":""}}
        with tempfile.TemporaryDirectory() as d:
            event=Path(d)/"event.json"
            def test(expected,event_body=base,**extras):
                event.write_text(json.dumps(event_body))
                argv=shlex.split(command)
                got=subprocess.run([sys.executable,*argv[1:]],cwd=ROOT,env={
                    **fixture_env, "GITHUB_EVENT_PATH":str(event),**extras},
                    text=True,capture_output=True, check=False)
                self.assertEqual(got.returncode,1)
                self.assertEqual(got.stdout,"")
                self.assertEqual(got.stderr,expected+"\n")
                self.assertEqual(set(x.name for x in Path(d).iterdir()),{"event.json"})
            test("ORDINARY_RETAINED_HELD_PRIVATE_ISSUER_UNREGISTERED")
            test("ORDINARY_RETAINED_HELD_PRIVATE_ISSUER_UNREGISTERED",ORDINARY_RETAINED_ENABLE="1")
            rejected="ORDINARY_RETAINED_PREFLIGHT_REJECTED"
            test(rejected, GITHUB_RUN_ATTEMPT="2")
            test(rejected, GITHUB_WORKFLOW_SHA="f"*40)
            test(rejected, GITHUB_TRIGGERING_ACTOR="actor-mismatch")
            test(rejected, GITHUB_EVENT_NAME="schedule")
            test(rejected, GITHUB_RUN_ID="１２３")
            test(rejected,dict(base,repository={"full_name":"other/stock-market-data"}))
            test(rejected,dict(base,inputs={INPUT:COMMITMENT,"request_json":"PRIVATE-SENTINEL"}))
            test(rejected,dict(base,inputs={INPUT:"PRIVATE-SENTINEL"}))
            test(rejected,dict(base,inputs={INPUT:"0"*64}))
            test(rejected,dict(base,inputs={INPUT:COMMITMENT,"private_unrelated":"PRIVATE-SENTINEL"}))
        prog=(ROOT/"scripts/run_ordinary_retained_preflight.py").read_text()
        imports=[node for node in ast.walk(ast.parse(prog)) if isinstance(node,(ast.Import,ast.ImportFrom))]
        self.assertTrue(all((not isinstance(n,ast.ImportFrom) or n.module in {"pathlib"})
                             and all(a.name in {"json","os","re","sys","Path"} for a in n.names)
                             for n in imports))


if __name__=="__main__": unittest.main()
