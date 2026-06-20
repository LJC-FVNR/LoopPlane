from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.plan_objectives import parse_plan_objectives
from runtime.prompt_builder import build_prompt_for_prepared_run
from runtime.read_models import rebuild_read_models
from runtime.scheduler import load_scheduler_snapshot, prepare_run, select_next_action
from runtime.self_expansion import (
    apply_expansion_proposal,
    load_expansion_status,
    reopen_expansion_failures,
    validate_expansion_proposal,
)


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_plan(project: Path, *, first_status: str = " ", second_status: str = " ") -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    (project / "PLAN.md").write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Base Work

- [{first_status}] T001: Produce base artifact
  - acceptance: Base artifact exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

- [{second_status}] T002: Dependent follow-up
  - acceptance: Follow-up artifact exists.
  - evidence: .loopplane/results/T002/
  - latest: .loopplane/results/T002/latest.json
  - depends_on: [T001]
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md
""",
        encoding="utf-8",
    )


def record_active_plan(project: Path) -> None:
    state_path = project / ".loopplane" / "runtime" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["configuration_problems"] = []
    state["active_plan_sha256"] = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_failure(project: Path, *, status: str = "exhausted") -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    write_json(
        project / ".loopplane" / "runtime" / "failure_registry.json",
        {
            "schema_version": "1.5",
            "workflow_id": workflow["workflow_id"],
            "failures": [
                {
                    "failure_id": "fail_T001",
                    "task_id": "T001",
                    "status": status,
                    "failure_class": "validation_failed",
                    "failure_signature": "missing-base-artifact",
                    "recovery_attempts": 1,
                    "max_recovery_attempts": 1,
                    "budget_remaining": False,
                }
            ],
        },
    )


def write_patch_and_proposal(
    project: Path,
    *,
    strategy: str = "reopen_failure_after_new_evidence",
    patch_phase_id: str = "P0",
    declared_operation: str | None = None,
) -> Path:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    proposal_dir = project / ".loopplane" / "runtime" / "expansion_fixture"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    patch_path = proposal_dir / "PLAN_PATCH.md"
    patch_path.write_text(
        f"""LOOPPLANE_PLAN_APPEND_BEGIN

## Phase {patch_phase_id}: Self-Expansion Evidence

- [ ] SE001: Gather independent evidence for T001 failure
  - acceptance: Independent evidence explains the missing artifact.
  - evidence: .loopplane/results/SE001/
  - latest: .loopplane/results/SE001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

LOOPPLANE_PLAN_APPEND_END
""",
        encoding="utf-8",
    )
    proposal: dict[str, object] = {
        "schema_version": "1.5",
        "proposal_id": "exp_fixture",
        "workflow_id": workflow["workflow_id"],
        "trigger": "recovery_exhausted",
        "expansion_type": "failed_recovery_decomposition",
        "resolution_strategy": strategy,
        "target_task_ids": ["T001"],
        "target_failure_ids": ["fail_T001"],
        "new_tasks": [
            {
                "task_id": "SE001",
                "title": "Gather independent evidence for T001 failure",
                "status": "[ ]",
                "dependencies": [],
                "validation": "file_exists: report.md",
            }
        ],
        "plan_patch_path": patch_path.as_posix(),
        "approval_required": False,
        "confidence": 0.8,
        "risk": "low",
        "loop_signature": "test-signature",
        "stop_condition": "SE001 reaches a terminal state and target failure is reopened once.",
    }
    if declared_operation is not None:
        proposal["plan_patch_operation"] = declared_operation
    proposal_path = proposal_dir / "expansion_proposal.json"
    write_json(proposal_path, proposal)
    return proposal_path


def write_objective_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    (project / "PLAN.md").write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Objective Base

- [x] T001: Produce objective base artifact
  - acceptance: Base artifact exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

### Phase Objective Checklist

- [ ] `PO1` Base artifact is decision-ready.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Decide whether the artifact supports the next workflow phase.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
""",
        encoding="utf-8",
    )


def write_objective_patch_and_proposal(
    project: Path,
    *,
    include_links: bool = True,
    patch_phase_id: str = "P0",
    dependencies: list[str] | None = None,
) -> Path:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    proposal_dir = project / ".loopplane" / "runtime" / "objective_expansion_fixture"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    patch_path = proposal_dir / "PLAN_PATCH.md"
    dependency_ids = list(dependencies if dependencies is not None else ["T001"])
    dependency_text = ", ".join(dependency_ids)
    patch_path.write_text(
        f"""LOOPPLANE_PLAN_APPEND_BEGIN

## Phase {patch_phase_id}: Objective Follow-up

- [ ] SEO001: Add decision-readiness checks
  - acceptance: Decision-readiness checks are documented.
  - evidence: .loopplane/results/SEO001/
  - latest: .loopplane/results/SEO001/latest.json
  - depends_on: [{dependency_text}]
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

LOOPPLANE_PLAN_APPEND_END
""",
        encoding="utf-8",
    )
    new_task = {
        "task_id": "SEO001",
        "title": "Add decision-readiness checks",
        "status": "[ ]",
        "dependencies": dependency_ids,
        "validation": "file_exists: report.md",
    }
    if include_links:
        new_task["objective_links"] = ["PO1"]
    proposal_path = proposal_dir / "expansion_proposal.json"
    write_json(
        proposal_path,
        {
            "schema_version": "1.5",
            "proposal_id": "exp_objective_fixture",
            "workflow_id": workflow["workflow_id"],
            "trigger": "phase_objective_gap",
            "expansion_type": "objective_gap",
            "resolution_strategy": "append_followup_only",
            "plan_patch_operation": "insert_task_into_phase",
            "target_task_ids": [],
            "target_failure_ids": [],
            "target_objective_ids": ["PO1"],
            "target_phase_id": "P0",
            "objective_verification_report": ".loopplane/runtime/objectives/phases/P0/objective_verification.json",
            "new_tasks": [new_task],
            "plan_patch_path": patch_path.as_posix(),
            "approval_required": False,
            "confidence": 0.86,
            "risk": "low",
            "loop_signature": "objective-gap-test-signature",
            "stop_condition": "PO1 is re-verified as satisfied or explicitly unresolved.",
        },
    )
    return proposal_path


def write_workflow_objective_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    (project / "PLAN.md").write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Base Work

- [x] T001: Produce base workflow artifact
  - acceptance: Base artifact exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

## Final Objective Checklist

- [ ] `WO1` Workflow artifact is ready for final handoff.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Decide whether the workflow result is ready for handoff.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
""",
        encoding="utf-8",
    )


def write_workflow_objective_patch_and_proposal(
    project: Path,
    *,
    patch_phase_id: str = "P1",
    dependencies: list[str] | None = None,
) -> Path:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    proposal_dir = project / ".loopplane" / "runtime" / "workflow_objective_expansion_fixture"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    patch_path = proposal_dir / "PLAN_PATCH.md"
    dependency_ids = list(dependencies if dependencies is not None else ["T001"])
    dependency_text = ", ".join(dependency_ids)
    patch_path.write_text(
        f"""LOOPPLANE_PLAN_APPEND_BEGIN

## Phase {patch_phase_id}: Workflow Objective Follow-up

- [ ] WFO001: Add final handoff readiness evidence
  - acceptance: Final handoff readiness evidence is documented.
  - evidence: .loopplane/results/WFO001/
  - latest: .loopplane/results/WFO001/latest.json
  - depends_on: [{dependency_text}]
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

LOOPPLANE_PLAN_APPEND_END
""",
        encoding="utf-8",
    )
    proposal_path = proposal_dir / "expansion_proposal.json"
    write_json(
        proposal_path,
        {
            "schema_version": "1.5",
            "proposal_id": "exp_workflow_objective_fixture",
            "workflow_id": workflow["workflow_id"],
            "trigger": "final_objective_gap",
            "expansion_type": "objective_gap",
            "resolution_strategy": "append_followup_only",
            "plan_patch_operation": "insert_phase_before_final_objectives",
            "target_task_ids": [],
            "target_failure_ids": [],
            "target_objective_ids": ["WO1"],
            "new_phase_id": patch_phase_id,
            "objective_verification_report": ".loopplane/runtime/objectives/workflow/objective_verification.json",
            "new_tasks": [
                {
                    "task_id": "WFO001",
                    "title": "Add final handoff readiness evidence",
                    "status": "[ ]",
                    "dependencies": dependency_ids,
                    "validation": "file_exists: report.md",
                    "objective_links": ["WO1"],
                }
            ],
            "plan_patch_path": patch_path.as_posix(),
            "approval_required": False,
            "confidence": 0.86,
            "risk": "low",
            "loop_signature": "workflow-objective-gap-test-signature",
            "stop_condition": "WO1 is re-verified as satisfied or explicitly unresolved.",
        },
    )
    return proposal_path


class SelfExpansionTest(unittest.TestCase):
    def test_init_creates_default_policy_runner_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Self-expansion defaults.")

            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            runners = json.loads((project / ".loopplane" / "config" / "agent_runners.json").read_text(encoding="utf-8"))
            registry = json.loads((project / ".loopplane" / "runtime" / "expansion_registry.json").read_text(encoding="utf-8"))

            self.assertTrue(workflow["self_expansion"]["enabled"])
            self.assertTrue(workflow["self_expansion"]["allow_after_objective_gap"])
            self.assertTrue(workflow["self_expansion"]["auto_apply_objective_gap_low_medium_risk"])
            self.assertIn("expansion_planner", runners["runners"])
            self.assertEqual(runners["runners"]["expansion_planner"]["role"], "expansion_planner")
            self.assertIn("objective_verifier", runners["runners"])
            self.assertEqual(runners["runners"]["objective_verifier"]["role"], "objective_verifier")
            self.assertEqual(runners["runner_failover"]["expansion_planner"]["runners"][0], "expansion_planner")
            self.assertEqual(runners["runner_failover"]["objective_verifier"]["runners"][0], "objective_verifier")
            self.assertEqual(registry["proposals"], [])
            self.assertEqual(load_expansion_status(project)["status"], "enabled")

    def test_proposal_validation_rejects_append_only_for_exhausted_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject unsafe append-only expansion.")
            write_plan(project)
            record_active_plan(project)
            write_failure(project)
            proposal_path = write_patch_and_proposal(project, strategy="append_followup_only")

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertIn("append_followup_only cannot target unresolved failures.", result["errors"])

    def test_low_risk_expansion_applies_append_only_patch_and_registry_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Apply low-risk expansion.")
            write_plan(project)
            record_active_plan(project)
            write_failure(project)
            proposal_path = write_patch_and_proposal(project)

            result = apply_expansion_proposal(project, proposal_path, run_id="run_expansion_fixture", runner_id="expansion_planner")

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "applied")
            self.assertIn("- [ ] SE001: Gather independent evidence", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertEqual(result["plan_patch_operation"], "insert_task_into_phase")
            self.assertEqual(result["plan_patch_apply"]["target_phase_id"], "P0")
            registry = json.loads((project / ".loopplane" / "runtime" / "expansion_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["cycle"], 1)
            self.assertEqual(registry["proposals"][0]["failure_resolution_status"], "pending_evidence")
            self.assertEqual(registry["proposals"][0]["target_phase_id"], "P0")

    def test_target_task_recovery_expansion_must_stay_in_target_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject recovery expansion outside target phase.")
            write_plan(project)
            record_active_plan(project)
            write_failure(project)
            proposal_path = write_patch_and_proposal(
                project,
                patch_phase_id="P1",
                declared_operation="append_to_end",
            )

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("expected insert_task_into_phase, got append_to_end" in error for error in result["errors"]))
            self.assertTrue(any("must be inserted into target phase P0" in error for error in result["errors"]))

    def test_objective_gap_proposal_requires_objective_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective expansion link validation.")
            write_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_objective_patch_and_proposal(project, include_links=False)

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("must include objective_links" in error for error in result["errors"]))

    def test_objective_gap_rejects_taskless_human_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective expansion must structurally expand.")
            write_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_objective_patch_and_proposal(project)
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            proposal["resolution_strategy"] = "requires_human"
            proposal["new_tasks"] = []
            proposal_path.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("must use append_followup_only" in error for error in result["errors"]), result["errors"])
            self.assertTrue(any("must add structural follow-up tasks" in error for error in result["errors"]), result["errors"])

    def test_phase_objective_gap_followup_must_stay_in_target_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective expansion phase mismatch.")
            write_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_objective_patch_and_proposal(project, patch_phase_id="P1")

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("must be inserted into target phase P0" in error for error in result["errors"]))

    def test_phase_objective_gap_followup_must_be_immediately_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective expansion must be next task.")
            write_objective_plan(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8")
                + """
## Phase P1: Future Work

- [ ] FUTURE: Future phase task
  - acceptance: Future work exists.
  - evidence: .loopplane/results/FUTURE/
  - latest: .loopplane/results/FUTURE/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md
""",
                encoding="utf-8",
            )
            record_active_plan(project)
            proposal_path = write_objective_patch_and_proposal(project, dependencies=["FUTURE"])

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("not immediately executable" in error for error in result["errors"]), result["errors"])

    def test_phase_objective_gap_apply_inserts_task_inside_phase_and_selects_it_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective expansion apply.")
            write_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_objective_patch_and_proposal(project)

            result = apply_expansion_proposal(project, proposal_path, run_id="run_objective_expansion", runner_id="expansion_planner")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [~] `PO1` Base artifact is decision-ready.", plan_text)
            self.assertIn("  - followup_tasks: SEO001", plan_text)
            self.assertLess(plan_text.index("- [ ] SEO001:"), plan_text.index("### Phase Objective Checklist"))
            action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "SEO001")
            registry = json.loads((project / ".loopplane" / "runtime" / "expansion_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["proposals"][0]["objective_resolution_status"], "followup_pending")
            self.assertEqual(registry["proposals"][0]["plan_patch_operation"], "insert_task_into_phase")
            rebuild_request = json.loads(
                (project / ".loopplane" / "runtime" / "read_model_rebuild_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuild_request["latest_reason"], "self_expansion_plan_patch_applied")
            self.assertIn("self_expansion_plan_patch_applied", rebuild_request["pending_reasons"])
            self.assertEqual(rebuild_request["status"], "pending")

    def test_workflow_objective_gap_apply_inserts_new_phase_before_final_objectives_and_selects_it_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workflow objective expansion apply.")
            write_workflow_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_workflow_objective_patch_and_proposal(project)

            result = apply_expansion_proposal(project, proposal_path, run_id="run_workflow_objective_expansion", runner_id="expansion_planner")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["added_phase_ids"], ["P1"])
            self.assertEqual(result["plan_patch_operation"], "insert_phase_before_final_objectives")
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertLess(plan_text.index("## Phase P1:"), plan_text.index("## Final Objective Checklist"))
            self.assertIn("- [~] `WO1` Workflow artifact is ready for final handoff.", plan_text)
            self.assertIn("  - followup_tasks: WFO001", plan_text)
            self.assertIn("  - followup_phases: P1", plan_text)
            objectives, objective_errors = parse_plan_objectives(plan_text)
            self.assertEqual(objective_errors, [])
            self.assertFalse([objective for objective in objectives if objective.scope == "phase" and objective.phase_id == "P1"])
            action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "WFO001")
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            plan_index = json.loads((project / ".loopplane" / "read_models" / "plan_index.json").read_text(encoding="utf-8"))
            expanded_phase = next(phase for phase in plan_index["phases"] if phase["phase_id"] == "P1")
            self.assertTrue(expanded_phase["expanded"])
            self.assertEqual(expanded_phase["expansion"]["proposal_id"], "exp_workflow_objective_fixture")
            expanded_task = expanded_phase["tasks"][0]
            self.assertEqual(expanded_task["task_id"], "WFO001")
            self.assertTrue(expanded_task["expanded"])
            self.assertEqual(expanded_task["expansion"]["target_objective_ids"], ["WO1"])
            rebuild_request = json.loads(
                (project / ".loopplane" / "runtime" / "read_model_rebuild_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuild_request["latest_reason"], "self_expansion_plan_patch_applied")
            self.assertIn("self_expansion_plan_patch_applied", rebuild_request["pending_reasons"])

    def test_workflow_objective_gap_rejects_existing_phase_task_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workflow objective expansion must add phase.")
            write_workflow_objective_plan(project)
            record_active_plan(project)
            proposal_path = write_workflow_objective_patch_and_proposal(project, patch_phase_id="P0")

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("must create a new phase" in error for error in result["errors"]))

    def test_workflow_objective_gap_new_phase_first_task_must_be_immediately_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workflow objective expansion must be next phase.")
            write_workflow_objective_plan(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace(
                    "## Final Objective Checklist",
                    """- [ ] T002: Unfinished pre-final task
  - acceptance: Pre-final work exists.
  - evidence: .loopplane/results/T002/
  - latest: .loopplane/results/T002/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md

## Final Objective Checklist""",
                ),
                encoding="utf-8",
            )
            record_active_plan(project)
            proposal_path = write_workflow_objective_patch_and_proposal(project, dependencies=["T002"])

            result = validate_expansion_proposal(project, proposal_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("not immediately executable" in error for error in result["errors"]), result["errors"])

    def test_scheduler_selects_expansion_for_expandable_final_verifier_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final verifier expansion.")
            write_plan(project, first_status="x", second_status="x")
            record_active_plan(project)
            write_json(
                project / ".loopplane" / "runtime" / "final_verification_report.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))["workflow_id"],
                    "status": "fail",
                    "blockers": [
                        {
                            "check": "required_final_deliverables_exist",
                            "message": "Required final deliverables are missing.",
                            "expandable": True,
                            "suggested_expansion_type": "missing_deliverable",
                            "target_task_ids": ["T001"],
                        }
                    ],
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_expansion_planner")
            self.assertEqual(action["selected"]["role"], "expansion_planner")

    def test_scheduler_selects_expansion_for_semantic_final_review_self_expand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Semantic final review expansion.")
            write_plan(project, first_status="x", second_status="x")
            record_active_plan(project)
            write_json(
                project / ".loopplane" / "runtime" / "final_verification_report.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))["workflow_id"],
                    "status": "fail",
                    "pass": False,
                    "ok": False,
                    "blockers": [
                        {
                            "check": "semantic_final_review",
                            "message": "Final reviewer rejected completion semantics.",
                            "kind": "non_expandable",
                            "expandable": False,
                            "details": {
                                "status": "rejected",
                                "recommended_action": "self_expand",
                                "rationale": "Research bar is unmet and expansion budget remains.",
                            },
                        }
                    ],
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_expansion_planner")
            self.assertEqual(action["selected"]["role"], "expansion_planner")
            self.assertEqual(action["selected"]["candidate"]["trigger"], "final_verification_failed")
            self.assertEqual(action["selected"]["candidate"]["blockers"][0]["check"], "semantic_final_review")

    def test_reopen_expansion_failure_after_added_evidence_task_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reopen failure after expansion evidence.")
            write_plan(project, first_status=" ", second_status="x")
            record_active_plan(project)
            write_failure(project)
            write_json(
                project / ".loopplane" / "runtime" / "expansion_registry.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))["workflow_id"],
                    "cycle": 1,
                    "events": [],
                    "proposals": [
                        {
                            "schema_version": "1.5",
                            "proposal_id": "exp_fixture",
                            "workflow_id": "ignored",
                            "status": "applied",
                            "created_at": "2026-06-16T00:00:00Z",
                            "expansion_type": "failed_recovery_decomposition",
                            "resolution_strategy": "reopen_failure_after_new_evidence",
                            "loop_signature": "test-signature",
                            "target_task_ids": ["T001"],
                            "target_failure_ids": ["fail_T001"],
                            "added_task_ids": ["T002"],
                            "failure_resolution_status": "pending_evidence",
                        }
                    ],
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "resolve_expansion_failure")
            result = reopen_expansion_failures(
                project,
                proposal_id=action["selected"]["proposal_id"],
                target_failure_ids=action["selected"]["target_failure_ids"],
                added_task_ids=action["selected"]["added_task_ids"],
                owner="test",
            )
            self.assertTrue(result["ok"], result)
            failure = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))["failures"][0]
            self.assertEqual(failure["status"], "unrecovered")
            self.assertTrue(failure["budget_remaining"])

    def test_expansion_prompt_is_taskless_and_points_to_expected_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Expansion prompt.")
            write_plan(project)
            record_active_plan(project)

            run = prepare_run(project, role="expansion_planner", runner_id="expansion_planner", scheduler_owner="test")
            built = build_prompt_for_prepared_run(project, run)

            self.assertEqual(built.task_id, "")
            self.assertIn("LoopPlane Self-Expansion Planner", built.content)
            self.assertIn("expansion_proposal.json", built.content)
            self.assertIn("PLAN_PATCH.md", built.content)
            self.assertIn("LOOPPLANE_PLAN_APPEND_BEGIN", built.content)
            self.assertIn("LOOPPLANE_PLAN_APPEND_END", built.content)
            self.assertIn("## Phase <phase-id>: <phase title>", built.content)
            self.assertIn("phase_objective_gap", built.content)
            self.assertNotIn("Produce base artifact", built.content)
            manifest = json.loads((Path(run.role_output_dir) / "expansion_context_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["references"]["active_plan"]["path"], "PLAN.md")
            self.assertIn("Produce base artifact", manifest["references"]["active_plan"]["excerpt"])


if __name__ == "__main__":
    unittest.main()
