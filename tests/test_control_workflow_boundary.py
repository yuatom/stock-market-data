from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ControlWorkflowBoundaryTest(unittest.TestCase):
    def test_main_entrypoint_does_not_consume_control_branch_push(self):
        text = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        self.assertNotIn("branches: [collector-requests, maintenance-requests]", text)
        self.assertIn("uses: ./.github/workflows/market-data-collector-runtime.yml", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("schedule:", text)

    def test_runtime_is_reusable_and_checks_out_immutable_control_ref(self):
        text = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", text)
        self.assertIn("ref: ${{ inputs.control_ref }}", text)
        self.assertIn("ref: main", text)
        self.assertIn("control_ref must be an immutable commit SHA", text)
        self.assertNotIn("on:\n  push:", text)

    def test_dispatcher_template_contains_no_business_logic(self):
        text = (ROOT / "config/control-branch-dispatcher.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [collector-requests, maintenance-requests]", text)
        self.assertIn(
            "uses: yuatom/stock-market-data/.github/workflows/market-data-collector-runtime.yml@main",
            text,
        )
        self.assertIn("control_ref: ${{ github.sha }}", text)
        self.assertIn("control_branch: ${{ github.ref_name }}", text)
        forbidden = [
            "TWELVE_DATA_API_KEY",
            "MARKET_DATA_STORE_TOKEN",
            "python scripts/",
            "git push",
            "market-data-store",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)

    def test_contract_forbids_control_branch_runtime_drift(self):
        contract = yaml.safe_load((ROOT / "config/data-plane.yaml").read_text(encoding="utf-8"))
        self.assertEqual(contract["contract_version"], 10)
        self.assertTrue(contract["principles"]["mutable_control_branch_must_not_execute_branch_local_runtime_logic"])
        workflow = contract["workflow_execution"]
        self.assertEqual(workflow["runtime_authority_ref"], "main")
        self.assertEqual(workflow["runtime_trigger"], "workflow_call")
        rules = workflow["control_branch_dispatcher_rules"]
        self.assertTrue(rules["business_logic_forbidden"])
        self.assertTrue(rules["must_call_runtime_at_main"])
        self.assertTrue(rules["must_pass_immutable_control_ref"])


if __name__ == "__main__":
    unittest.main()
