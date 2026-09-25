from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ControlWorkflowBoundaryTest(unittest.TestCase):
    def test_main_entrypoint_does_not_consume_control_branch_push(self):
        text = (ROOT / ".github/workflows/market-data-collector.yml").read_text(encoding="utf-8")
        self.assertNotIn("branches: [collector-requests, maintenance-requests, discovery-requests]", text)
        self.assertIn("uses: ./.github/workflows/market-data-collector-runtime.yml", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("schedule:", text)

    def test_standard_runtime_is_reusable_and_checks_out_immutable_control_ref(self):
        text = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", text)
        self.assertIn("ref: ${{ inputs.control_ref }}", text)
        self.assertIn("ref: main", text)
        self.assertIn("control_ref must be an immutable commit SHA", text)
        self.assertNotIn("on:\n  push:", text)

    def test_store_proof_publication_is_exact_base_cas_bound(self):
        text = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        persist = text.split("- name: Persist private Store only", 1)[1]
        proof_branch = persist.split('if [[ -n "$proof_stage" ]]; then', 1)[1].split(
            "          fi\n\n          for attempt in 1 2 3; do", 1
        )[0]
        self.assertNotIn("git pull --rebase", proof_branch)
        self.assertIn("store_base_sha=\"$(git rev-parse HEAD)\"", persist)
        self.assertIn('--base "$store_base_sha"', proof_branch)
        self.assertIn('--candidate "$(git rev-parse HEAD)"', proof_branch)
        self.assertIn("--verify-existing", proof_branch)
        verify_pos = proof_branch.index("--verify-existing")
        push_pos = proof_branch.index("python ../compute/scripts/push_proof_store_commit.py")
        self.assertLess(verify_pos, push_pos)
        self.assertNotIn("git push", proof_branch)
        self.assertNotIn("--force", proof_branch)
        workflow = yaml.safe_load(text)
        steps = workflow["jobs"]["collect"]["steps"]
        names = [step["name"] for step in steps]
        self.assertLess(names.index("Persist private Store only"), names.index("Verify durable Store publication"))
        durability = steps[names.index("Verify durable Store publication")]
        self.assertEqual(durability["if"], "steps.invocation.outputs.mode != 'smoke_readonly'")
        self.assertNotIn("continue-on-error", durability)
        self.assertIn("verify_store_publication.py", durability["run"])
        self.assertIn("--required-manifest", durability["run"])

    def test_non_proof_store_publication_preserves_base_retry_semantics(self):
        text = (ROOT / ".github/workflows/market-data-collector-runtime.yml").read_text(encoding="utf-8")
        persist = text.split("- name: Persist private Store only", 1)[1]
        non_proof_branch = persist.split("          fi\n\n          for attempt in 1 2 3; do", 1)[1]
        self.assertIn("git pull --rebase origin main", non_proof_branch)
        self.assertIn("git push origin HEAD:main", non_proof_branch)
        self.assertIn("sleep $((attempt * 2))", non_proof_branch)
        self.assertNotIn("--verify-existing", non_proof_branch)
        self.assertNotIn("remote_main_sha", non_proof_branch)

    def test_discovery_runtime_is_reusable_main_logic_only(self):
        text = (ROOT / ".github/workflows/dynamic-candidate-runtime.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", text)
        self.assertIn("ref: ${{ inputs.control_ref }}", text)
        self.assertIn("ref: main", text)
        self.assertIn("discovery-requests", text)
        self.assertNotIn("on:\n  push:", text)

    def test_dispatcher_template_contains_no_business_logic(self):
        text = (ROOT / "config/control-branch-dispatcher.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [collector-requests, maintenance-requests, discovery-requests]", text)
        self.assertIn(
            "uses: yuatom/stock-market-data/.github/workflows/market-data-collector-runtime.yml@main",
            text,
        )
        self.assertIn(
            "uses: yuatom/stock-market-data/.github/workflows/dynamic-candidate-runtime.yml@main",
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
        self.assertEqual(contract["contract_version"], 15)
        self.assertTrue(contract["principles"]["mutable_control_branch_must_not_execute_branch_local_runtime_logic"])
        self.assertTrue(contract["principles"]["historical_probe_replay_must_bind_exact_probe_path_and_blob_sha"])
        workflow = contract["workflow_execution"]
        self.assertEqual(workflow["runtime_authority_ref"], "main")
        self.assertEqual(workflow["runtime_trigger"], "workflow_call")
        rules = workflow["control_branch_dispatcher_rules"]
        self.assertTrue(rules["business_logic_forbidden"])
        self.assertTrue(rules["must_call_runtime_at_main"])
        self.assertTrue(rules["must_pass_immutable_control_ref"])


if __name__ == "__main__":
    unittest.main()
