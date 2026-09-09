import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dynamic-candidate-runtime.yml"


class DynamicCandidateRuntimeWorkflowTests(unittest.TestCase):
    @staticmethod
    def _validator_script():
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["collect"]["steps"]
        validator = next(step for step in steps if step.get("name") == "Validate immutable request")
        return validator["run"]

    @staticmethod
    def _init_compute_repo(root: Path) -> str:
        compute = root / "compute"
        compute.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=compute, check=True)
        subprocess.run(["git", "config", "user.name", "runtime-workflow-test"], cwd=compute, check=True)
        subprocess.run(["git", "config", "user.email", "runtime-workflow-test@example.invalid"], cwd=compute, check=True)
        (compute / "contract-marker.txt").write_text("contract\n", encoding="utf-8")
        subprocess.run(["git", "add", "contract-marker.txt"], cwd=compute, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "test contract"], cwd=compute, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=compute, text=True).strip()

    @staticmethod
    def _request(stage: str, purpose: str, contract_sha: str) -> dict:
        return {
            "schema_version": 1,
            "request_id": f"runtime-{stage}-{purpose}",
            "requested_at": "2020-01-02T09:46:00-05:00",
            "trade_date": "2020-01-02",
            "stage": stage,
            "request_purpose": purpose,
            "research_repository": "yuatom/stock-dairy",
            "research_repository_commit_sha": "b" * 40,
            "market_data_contract_sha": contract_sha,
            "candidate_symbols": ["BILI"],
            "transaction_id": f"{stage}-test",
        }

    def _run_validator(self, stage: str, purpose: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            contract_sha = self._init_compute_repo(root)
            request_path = root / "control" / "requests" / "dynamic-candidate-request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text(
                json.dumps(self._request(stage, purpose, contract_sha)),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "CONTROL_REF": "a" * 40,
                    "CONTROL_BRANCH": "discovery-requests",
                    "GITHUB_OUTPUT": str(root / "github-output.txt"),
                }
            )
            return subprocess.run(
                [sys.executable, "-c", self._validator_script()],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_workflow_accepts_personal_membership_at_all_regular_stages(self):
        for stage in ("open_15m", "open_30m", "open_60m", "close"):
            with self.subTest(stage=stage):
                result = self._run_validator(stage, "personal_membership")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workflow_rejects_new_opportunity_discovery_at_open15_and_open60(self):
        for stage in ("open_15m", "open_60m"):
            with self.subTest(stage=stage):
                result = self._run_validator(stage, "opportunity_discovery")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("forbids new opportunity discovery", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
