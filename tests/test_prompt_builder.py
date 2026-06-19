from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import _workflow_config, init_project
from runtime.prompt_builder import build_prompt_for_prepared_run
from runtime.scheduler import PreparedRun, prepare_run


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompt_builder"
WORKFLOW_ID = "wf_prompt_fixture"
CUSTOM_PATHS = {
    "brief_file": "docs/BRIEF.md",
    "plan_file": "plans/ACTIVE_PLAN.md",
    "shared_context_file": "workflow/context/SHARED.md",
    "results_dir": "artifacts/loopplane/results",
    "runtime_dir": "state/loopplane/runtime",
    "read_models_dir": "state/loopplane/read_models",
    "requests_dir": "inbox/loopplane/requests",
    "planning_dir": "plans/loopplane/planning",
    "version_control_config_file": "configuration/loopplane/version_control.json",
}


def create_prompt_project(root: Path) -> Path:
    project = root / "project"
    workflow_file = project / ".loopplane" / "config" / "workflow.json"
    workflow_file.parent.mkdir(parents=True)
    workflow = _workflow_config(WORKFLOW_ID, "2026-06-10T00:00:00Z", CUSTOM_PATHS)
    workflow_file.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    init_project(project, "Prompt builder fixture project.")
    (project / CUSTOM_PATHS["plan_file"]).write_text(active_plan(), encoding="utf-8")
    failure_registry = {
        "schema_version": "1.5",
        "workflow_id": WORKFLOW_ID,
        "failures": [
            {
                "failure_id": "fail_fixture",
                "task_id": "T1",
                "status": "unrecovered",
                "failure_class": "worker_failed",
                "failure_signature": "golden-mismatch",
                "first_seen_at": "2026-06-10T00:00:00Z",
                "last_seen_at": "2026-06-10T00:00:00Z",
                "run_id": "run_failed",
                "attempts": 1,
                "recovery_attempts": 0,
                "max_recovery_attempts": 3,
                "budget_remaining": True,
                "summary": "Earlier prompt omitted configured paths.",
            }
        ]
    }
    (project / CUSTOM_PATHS["runtime_dir"] / "failure_registry.json").write_text(
        json.dumps(failure_registry, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return project


def active_plan() -> str:
    return """# Project Plan

## Metadata

- workflow_id: wf_prompt_fixture
- plan_version: 1
- generated_from: docs/BRIEF.md
- active: true

## Configured Paths

- brief_file: docs/BRIEF.md
- plan_file: plans/ACTIVE_PLAN.md
- shared_context_file: workflow/context/SHARED.md
- results_dir: artifacts/loopplane/results
- runtime_dir: state/loopplane/runtime
- read_models_dir: state/loopplane/read_models
- requests_dir: inbox/loopplane/requests
- planning_dir: plans/loopplane/planning
- version_control_config_file: configuration/loopplane/version_control.json

## Phase P0: Prompt Fixture

- [ ] T1: Render target prompt
  - acceptance: Prompt includes target task block and configured paths.
  - evidence: artifacts/loopplane/results/T1/
  - latest: artifacts/loopplane/results/T1/latest.json
  - depends_on: []
  - risk: low
  - validation: Prompt golden test.
  - max_attempts: 3
  - approval: not_required
  - deliverables: Rendered prompt fixture.

- [ ] T2: Later task
  - acceptance: This block must not appear in the T1 prompt.
  - evidence: artifacts/loopplane/results/T2/
  - latest: artifacts/loopplane/results/T2/latest.json
  - depends_on: [T1]
  - risk: low
  - validation: Later validation.
  - max_attempts: 3
  - approval: not_required
  - deliverables: Later output.
"""


def fixed_prepared_run(project: Path, *, role: str) -> PreparedRun:
    runtime_dir = project / CUSTOM_PATHS["runtime_dir"]
    results_dir = project / CUSTOM_PATHS["results_dir"]
    scheduler_run_dir = runtime_dir / "runs" / "run_fixture"
    role_output_dir = results_dir / "T1" / "runs" / "run_fixture"
    return PreparedRun(
        workflow_id=WORKFLOW_ID,
        run_id="run_fixture",
        node_id=f"node_{role}_T1_run_fixture",
        role=role,
        runner_id="worker",
        task_id="T1",
        scheduler_run_dir=scheduler_run_dir,
        role_output_dir=role_output_dir,
        task_evidence_run_dir=role_output_dir,
        prompt_path=scheduler_run_dir / "prompt.md",
        stdout_path=scheduler_run_dir / "stdout.log",
        stderr_path=scheduler_run_dir / "stderr.log",
        final_output_path=scheduler_run_dir / "final.md",
        adapter_result_path=scheduler_run_dir / "adapter_result.json",
        active_run_lease_path=runtime_dir / "active_run_leases" / "run_fixture.json",
        prepared_at="2026-06-10T00:00:00Z",
        scheduler_owner="test-scheduler",
    )


class PromptBuilderTest(unittest.TestCase):
    def test_worker_prompt_matches_golden_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = create_prompt_project(Path(tmp))

            built = build_prompt_for_prepared_run(project, fixed_prepared_run(project, role="worker"))

            old_golden = (FIXTURES / "worker_prompt.golden.md").read_text(encoding="utf-8")
            self.assertLess(len(built.content), len(old_golden))
            self.assertEqual(built.prompt_path.read_text(encoding="utf-8"), built.content)
            self.assertIn("## Target Task Block", built.content)
            self.assertIn("- [ ] T1: Render target prompt", built.content)
            self.assertIn("Configured workflow paths, hashes, run paths", built.content)
            self.assertNotIn("## Configured Workflow Paths", built.content)
            metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_authority"], "derived_prompt_not_source_of_truth")
            self.assertEqual(metadata["configured_paths"]["plan_file"], CUSTOM_PATHS["plan_file"])
            self.assertEqual(metadata["previous_failure_ids"], ["fail_fixture"])
            self.assertEqual(
                metadata["context_manifest_path"],
                "state/loopplane/runtime/runs/run_fixture/prompt_context_manifest.json",
            )
            manifest = json.loads((built.metadata_path.parent / "prompt_context_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_authority"], "context_manifest_not_source_of_truth")
            self.assertEqual(manifest["task_id"], "T1")
            self.assertIn("- [ ] T1: Render target prompt", manifest["target_task_block"])
            self.assertEqual(manifest["previous_failure_ids"], ["fail_fixture"])
            self.assertEqual(manifest["configured_paths"]["plan_file"], CUSTOM_PATHS["plan_file"])
            self.assertEqual(manifest["run_paths"]["prompt_path"], "state/loopplane/runtime/runs/run_fixture/prompt.md")
            self.assertIn("canonical_statuses", manifest["output_contract"])

    def test_recovery_prompt_matches_golden_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = create_prompt_project(Path(tmp))

            built = build_prompt_for_prepared_run(
                project,
                fixed_prepared_run(project, role="recovery_worker"),
                failure_id="fail_fixture",
            )

            old_golden = (FIXTURES / "recovery_prompt.golden.md").read_text(encoding="utf-8")
            self.assertLess(len(built.content), len(old_golden))
            self.assertEqual(built.prompt_path.read_text(encoding="utf-8"), built.content)
            self.assertIn("## Failure Summary", built.content)
            self.assertIn("failure_id: fail_fixture", built.content)
            self.assertNotIn("## Configured Workflow Paths", built.content)
            metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["selected_failure_id"], "fail_fixture")
            self.assertEqual(metadata["run_paths"]["prompt_path"], "state/loopplane/runtime/runs/run_fixture/prompt.md")
            self.assertTrue((built.metadata_path.parent / "prompt_context_manifest.json").is_file())

    def test_prompt_builder_writes_prompt_after_prepare_run_allocates_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = create_prompt_project(Path(tmp))
            run = prepare_run(
                project,
                role="worker",
                task_id="T1",
                runner_id="worker",
                scheduler_owner="test-scheduler",
            )

            built = build_prompt_for_prepared_run(project, run)
            text = run.prompt_path.read_text(encoding="utf-8")

            self.assertEqual(built.prompt_path, run.prompt_path)
            self.assertEqual(text, built.content)
            self.assertIn("## Target Task Block", text)
            self.assertIn("- [ ] T1: Render target prompt", text)
            self.assertNotIn("- [ ] T2: Later task", text)
            self.assertIn("- `workflow/context/SHARED.md`", text)
            self.assertIn("prompt_context_manifest.json", text)
            self.assertNotIn("- plan_file: plans/ACTIVE_PLAN.md", text)
            self.assertNotIn(f"- prompt path: state/loopplane/runtime/runs/{run.run_id}/prompt.md", text)
            self.assertNotIn("- active run lease path: state/loopplane/runtime/active_run_leases/", text)
            metadata = json.loads((run.scheduler_run_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["prompt_path"], f"state/loopplane/runtime/runs/{run.run_id}/prompt.md")
            self.assertIn("Earlier prompt omitted configured paths.", text)
            self.assertIn("## Untrusted Input Rule", text)
            manifest = json.loads((run.scheduler_run_dir / "prompt_context_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["configured_paths"]["plan_file"], "plans/ACTIVE_PLAN.md")
            self.assertIn("agent_status_required_fields", manifest["output_contract"])


if __name__ == "__main__":
    unittest.main()
