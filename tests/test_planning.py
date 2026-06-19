from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.planning import activate_plan, inspect_plan_draft, run_auditor, run_plan_revision_loop, run_planner


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
CLI_ADAPTER_FIXTURE_BIN = REPO_ROOT / "tests" / "fixtures" / "cli_adapters" / "bin"


def install_cli_adapter_fixture_bin(root: Path) -> Path:
    bin_dir = root / "fixture-bin"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        target = bin_dir / name
        shutil.copy2(CLI_ADAPTER_FIXTURE_BIN / name, target)
        target.chmod(target.stat().st_mode | 0o111)
    return bin_dir


def configure_planner(
    project: Path,
    *,
    adapter: str = "noop",
    command: str = "noop",
    enabled: bool = True,
    args: list[str] | None = None,
    prompt_delivery: dict[str, object] | None = None,
) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    planner = config["runners"]["planner"]
    planner.update(
        {
            "adapter": adapter,
            "command": command,
            "enabled": enabled,
        }
    )
    if args is not None:
        planner["args"] = args
    if prompt_delivery is not None:
        planner["prompt_delivery"] = prompt_delivery
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_auditor(
    project: Path,
    *,
    adapter: str = "noop",
    command: str = "noop",
    enabled: bool = True,
    args: list[str] | None = None,
    prompt_delivery: dict[str, object] | None = None,
) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auditor = config["runners"]["auditor"]
    auditor.update(
        {
            "adapter": adapter,
            "command": command,
            "enabled": enabled,
        }
    )
    if args is not None:
        auditor["args"] = args
    if prompt_delivery is not None:
        auditor["prompt_delivery"] = prompt_delivery
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_plan_auditor(project: Path) -> None:
    config_path = project / ".loopplane" / "config" / "workflow.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["planning"]["auditor_required"] = True
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_max_planner_iterations(project: Path, max_iterations: int) -> None:
    config_path = project / ".loopplane" / "config" / "workflow.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["planning"]["max_planner_iterations"] = max_iterations
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def approval_required_plan_text(workflow_id: str) -> str:
    return f"""# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: approval fixture
- active: false

## Phase P0: Approval Fixture

- [ ] P0.T001: Run approval gated task
  - acceptance: The task is only allowed to run after explicit approval.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: high
  - validation: report_contains: APPROVAL-P0-T001-DONE
  - max_attempts: 3
  - approval: required: before_fixture_run
  - deliverables: Approval-gated fixture result.

### Phase Objective Checklist

- [ ] `P0.O1` Approval-gated evidence is reviewed before execution.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the approved task produced reviewable evidence.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 1

## Final Objective Checklist

- [ ] `FO1` Approval-gated workflow can only complete after approved evidence exists.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm the workflow did not bypass approval-gated execution.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 1
"""


class PlannerRuntimeTest(unittest.TestCase):
    def test_noop_planner_generates_structural_plan_draft_and_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Build a tiny CLI that greets the user.")
            configure_planner(project)

            result = run_planner(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            plan_path = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            readiness_path = project / ".loopplane" / "planning" / "plan_readiness_report.json"
            run_dir = Path(result["run_dir"])
            self.assertTrue(plan_path.is_file())
            self.assertTrue(readiness_path.is_file())
            self.assertTrue((run_dir / "PLAN_DRAFT.md").is_file())
            self.assertTrue((run_dir / "plan_readiness_report.json").is_file())
            self.assertTrue((run_dir / "node_summary.json").is_file())
            self.assertTrue((run_dir / "adapter_result.json").is_file())
            self.assertTrue((project / ".loopplane" / "planning" / "planning_events.jsonl").is_file())
            summary = json.loads((run_dir / "node_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["node_type"], "planner")
            self.assertEqual(summary["role"], "planner")

            prompt = (run_dir / "planner_prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("Build a tiny CLI that greets the user.", prompt)
            self.assertNotIn("## Shared Context Content", prompt)
            self.assertIn("context_manifest_path: .loopplane/planning/runs/" + result["run_id"] + "/planner_context_manifest.json", prompt)
            self.assertIn("plan_draft_path: .loopplane/planning/PLAN_DRAFT.md", prompt)
            self.assertIn("task_granularity: coarse_by_default", prompt)
            self.assertIn("max_initial_tasks: 8", prompt)
            self.assertIn("batch_low_risk_tasks: true", prompt)
            self.assertIn("## Task Granularity Defaults", prompt)
            context_manifest = json.loads((run_dir / "planner_context_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("Build a tiny CLI that greets the user.", context_manifest["references"]["project_brief"]["excerpt"])
            self.assertEqual(context_manifest["references"]["shared_context"]["path"], ".loopplane/SHARED_CONTEXT.md")
            self.assertTrue((run_dir / "workspace_tree.txt").is_file())
            self.assertTrue((run_dir / "workflow_paths.txt").is_file())
            adapter_input = json.loads((run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_input["role"], "planner")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_ROLE"], "planner")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_PLAN_DRAFT_PATH_REL"], ".loopplane/planning/PLAN_DRAFT.md")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_PLANNING_RUN_DIR_REL"], ".loopplane/planning/runs/" + result["run_id"])

            plan_text = plan_path.read_text(encoding="utf-8")
            self.assertIn("- [ ] P0.T001: Review brief and workspace constraints", plan_text)
            self.assertIn("- evidence: .loopplane/results/P0.T001/", plan_text)
            structural = inspect_plan_draft(plan_path, workflow_id=result["workflow_id"])
            self.assertTrue(structural["valid"], structural)
            self.assertEqual(structural["task_ids"], ["P0.T001", "P1.T001", "P2.T001"])

            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
            self.assertEqual(readiness["status"], "ready_for_activation")
            self.assertEqual(readiness["draft_source"], "noop_fixture")
            self.assertTrue(readiness["structural_checks"]["valid"])
            self.assertTrue(readiness["ready_for_audit"])
            self.assertTrue(readiness["ready_for_activation"])
            self.assertEqual(readiness["activation_blocked_by"], [])
            self.assertEqual(readiness["summary"]["tasks"], 3)
            self.assertEqual(readiness["summary"]["high_risk_tasks"], 0)
            self.assertFalse(readiness["summary"]["requires_human_approval"])
            readiness_warnings = "\n".join(str(warning) for warning in readiness.get("warnings", []))
            self.assertNotIn("looks prose-like", readiness_warnings)
            self.assertIn("report_contains: LoopPlane-P1-T001-DONE", plan_text)

    def test_noop_planner_blocks_activation_when_auditor_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Build a tiny CLI that greets the user.")
            configure_planner(project)
            require_plan_auditor(project)

            result = run_planner(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "ready_for_audit")
            readiness = json.loads(
                (project / ".loopplane" / "planning" / "plan_readiness_report.json").read_text(encoding="utf-8")
            )
            self.assertTrue(readiness["ready_for_audit"])
            self.assertFalse(readiness["ready_for_activation"])
            self.assertEqual(readiness["activation_blocked_by"], ["audit_required"])

    def test_disabled_planner_returns_waiting_config_without_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Draft a workflow.")
            configure_planner(project, enabled=False)

            result = run_planner(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "waiting_config")
            self.assertIn("disabled", result["message"])
            self.assertFalse((project / ".loopplane" / "planning" / "PLAN_DRAFT.md").exists())
            self.assertTrue((Path(result["run_dir"]) / "node_summary.json").is_file())

    def test_shell_planner_can_write_plan_draft_from_prompt_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Generate a one task adapter-authored plan.")
            script = """
import json
import os
import pathlib
import sys

prompt = sys.stdin.read()
project = pathlib.Path(os.environ["LOOPPLANE_PROJECT_ROOT"])
run_dir = pathlib.Path(os.environ["LOOPPLANE_PLANNING_RUN_DIR"])
manifest = json.loads((run_dir / "planner_context_manifest.json").read_text(encoding="utf-8"))
brief = (project / manifest["references"]["project_brief"]["path"]).read_text(encoding="utf-8")
if "Generate a one task adapter-authored plan." not in brief:
    raise SystemExit(2)
draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Adapter Authored

- [ ] P0.T001: Adapter generated task
  - acceptance: The planner adapter writes this draft from the prompt context.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Structural readiness checks pass.
  - max_attempts: 3
  - approval: not_required
  - deliverables: Adapter-authored plan draft

### Phase Objective Checklist

- [ ] `P0.O1` Adapter-authored planning evidence is ready for activation.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the adapter-authored plan is suitable for durable execution.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` The adapter-authored workflow has objective-verifiable handoff gates.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm final workflow readiness can be judged through objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_planner(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            plan = (project / ".loopplane" / "planning" / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            self.assertIn("Adapter generated task", plan)
            readiness = json.loads(
                (project / ".loopplane" / "planning" / "plan_readiness_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(readiness["draft_source"], "adapter")

    def test_shell_planner_malformed_draft_fails_readiness_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Generate a malformed adapter-authored plan.")
            script = """
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Adapter Authored

- [ ] P0.T001: Adapter generated task
  - acceptance: The planner adapter writes this draft from the prompt context.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Structural readiness checks pass.
  - max_attempts: 3

### Phase Objective Checklist

- [ ] `P0.O1` Adapter-authored planning evidence is ready for activation.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the adapter-authored plan is suitable for durable execution.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` The adapter-authored workflow has objective-verifiable handoff gates.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm final workflow readiness can be judged through objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_planner(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "needs_revision")
            readiness = json.loads(
                (project / ".loopplane" / "planning" / "plan_readiness_report.json").read_text(encoding="utf-8")
            )
            self.assertFalse(readiness["ready_for_audit"])
            self.assertFalse(readiness["ready_for_activation"])
            self.assertEqual(readiness["activation_blocked_by"], ["readiness_errors"])
            self.assertIn("P0.T001: missing required field 'approval'", readiness["errors"])
            self.assertIn("P0.T001: missing required field 'deliverables'", readiness["errors"])

    def test_planner_blocks_activation_readiness_when_approval_required_but_policy_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Generate an approval-gated plan while approvals are disabled.")
            script = """
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(__PLAN__.replace("__WORKFLOW_ID__", workflow_id), encoding="utf-8")
""".replace("__PLAN__", repr(approval_required_plan_text("__WORKFLOW_ID__")))
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_planner(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "ready_for_audit")
            readiness = json.loads(
                (project / ".loopplane" / "planning" / "plan_readiness_report.json").read_text(encoding="utf-8")
            )
            self.assertTrue(readiness["ready_for_audit"])
            self.assertFalse(readiness["ready_for_activation"])
            self.assertIn("approval_policy_disabled", readiness["activation_blocked_by"])
            self.assertTrue(readiness["summary"]["requires_human_approval"])
            self.assertIn("interactive approvals are disabled", "\n".join(readiness["warnings"]))


class AuditorRuntimeTest(unittest.TestCase):
    def test_noop_auditor_writes_report_and_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Audit a generated plan draft.")
            configure_planner(project)
            configure_auditor(project)
            require_plan_auditor(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))

            result = run_auditor(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "passed")
            audit_report_path = project / ".loopplane" / "planning" / "audit_report.json"
            run_dir = Path(result["run_dir"])
            self.assertTrue(audit_report_path.is_file())
            self.assertTrue((run_dir / "audit_report.json").is_file())
            self.assertTrue((run_dir / "node_summary.json").is_file())
            self.assertTrue((run_dir / "adapter_result.json").is_file())
            self.assertTrue((run_dir / "auditor_prompt.md").is_file())
            self.assertTrue((run_dir / "audit_events.jsonl").is_file())
            self.assertTrue((project / ".loopplane" / "planning" / "audit_events.jsonl").is_file())
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001").exists())

            report = json.loads(audit_report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["passed"])
            self.assertTrue(report["auditor_required"])
            self.assertFalse(report["implementation_tasks_executed"])
            self.assertEqual(report["blocking_findings"], [])

            summary = json.loads((run_dir / "node_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["node_type"], "auditor")
            self.assertEqual(summary["role"], "auditor")
            self.assertEqual(summary["audit_report_path"], audit_report_path.as_posix())
            adapter_input = json.loads((run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_input["role"], "auditor")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_ROLE"], "auditor")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_PLAN_DRAFT_PATH_REL"], ".loopplane/planning/PLAN_DRAFT.md")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_AUDIT_RUN_DIR_REL"], ".loopplane/planning/runs/" + result["run_id"])

            events = (project / ".loopplane" / "planning" / "audit_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("auditor_run_started", events)
            self.assertIn("audit_report_written", events)
            self.assertIn("auditor_run_finished", events)

    def test_auditor_fixture_catches_missing_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Audit a malformed plan draft.")
            configure_auditor(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            draft = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            draft.write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Missing Acceptance

- [ ] P0.T001: Missing acceptance criteria
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: A deterministic smoke check passes.
  - max_attempts: 3
  - approval: not_required
  - deliverables: Smoke result
""",
                encoding="utf-8",
            )

            result = run_auditor(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            report = json.loads(
                (project / ".loopplane" / "planning" / "audit_report.json").read_text(encoding="utf-8")
            )
            messages = [finding["message"] for finding in report["blocking_findings"]]
            self.assertIn("P0.T001: missing required field 'acceptance'", messages)
            self.assertIn("missing_acceptance", {finding["code"] for finding in report["blocking_findings"]})
            self.assertIn("Add `acceptance` metadata to task P0.T001.", report["recommended_revisions"])


class PlanRevisionLoopTest(unittest.TestCase):
    def test_readiness_failure_returns_to_planning_and_records_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Revise a plan after readiness fails.")
            set_max_planner_iterations(project, 3)
            script = r"""
import json
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
state_path = pathlib.Path(os.environ["LOOPPLANE_PLANNING_STATE_PATH"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
has_readiness_reason = any(reason.get("source") == "readiness" for reason in state.get("revision_reasons", []))
extra_fields = ""
if has_readiness_reason:
    extra_fields = "  - approval: not_required\n  - deliverables: Revised readiness-checked plan\n"
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Revision

- [ ] P0.T001: Readiness revision task
  - acceptance: The revised plan records all required readiness fields.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Plan readiness checks pass.
  - max_attempts: 3
{extra_fields}
### Phase Objective Checklist

- [ ] `P0.O1` Readiness revision objective is reviewable.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the revised plan includes enough evidence for objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` Revised plan is ready for objective-verifiable activation.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm the workflow can be checked by final objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_plan_revision_loop(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "plan_ready")
            self.assertEqual(result["planner_iterations"], 2)
            self.assertEqual(result["audit_iterations"], 0)
            self.assertEqual(result["revision_reasons"][0]["source"], "readiness")
            self.assertIn("missing required field 'approval'", result["revision_reasons"][0]["messages"][0])

            state = json.loads((project / ".loopplane" / "planning" / "planning_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "plan_ready")
            self.assertTrue(state["ready_for_activation"])
            transitions = [(item["from"], item["to"]) for item in state["transitions"]]
            self.assertIn(("planning", "plan_revision_needed"), transitions)
            self.assertIn(("plan_revision_needed", "planning"), transitions)

            runtime_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["status"], "plan_ready")
            self.assertEqual(runtime_state["planner_iterations"], 2)
            self.assertEqual(runtime_state["planning"]["revision_reasons"][0]["source"], "readiness")

            events = (project / ".loopplane" / "planning" / "planning_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("plan_revision_reason_recorded", events)
            self.assertIn("plan_revision_loop_finished", events)

    def test_audit_fail_then_revised_plan_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Revise a plan after audit fails.")
            set_max_planner_iterations(project, 3)
            require_plan_auditor(project)
            planner_script = r"""
import json
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
state_path = pathlib.Path(os.environ["LOOPPLANE_PLANNING_STATE_PATH"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
has_audit_reason = any(reason.get("source") == "audit" for reason in state.get("revision_reasons", []))
marker = "Auditor revision marker" if has_audit_reason else "Initial audit candidate"
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Audit Revision

- [ ] P0.T001: Audit-sensitive task
  - acceptance: The plan satisfies auditor feedback before activation.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Auditor accepts the revised plan.
  - max_attempts: 3
  - approval: not_required
  - deliverables: {marker}

### Phase Objective Checklist

- [ ] `P0.O1` Audit revision evidence is reviewable.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the audited plan evidence can support objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` Audited plan is ready for objective-verifiable activation.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm final workflow readiness can be judged by objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
''', encoding="utf-8")
"""
            auditor_script = r"""
import os
import pathlib
import sys

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"]).read_text(encoding="utf-8")
if "Auditor revision marker" not in draft:
    print("Audit requires an explicit revision marker.", file=sys.stderr)
    raise SystemExit(3)
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", planner_script],
                prompt_delivery={"mode": "stdin"},
            )
            configure_auditor(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", auditor_script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_plan_revision_loop(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "plan_ready")
            self.assertEqual(result["planner_iterations"], 2)
            self.assertEqual(result["audit_iterations"], 2)
            self.assertEqual(len(result["revision_reasons"]), 1)
            reason = result["revision_reasons"][0]
            self.assertEqual(reason["source"], "audit")
            self.assertIn("auditor_adapter_failed", reason["codes"])
            self.assertIn("Auditor adapter exited with code 3.", reason["messages"])

            plan_text = (project / ".loopplane" / "planning" / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            self.assertIn("Auditor revision marker", plan_text)
            audit_report = json.loads((project / ".loopplane" / "planning" / "audit_report.json").read_text(encoding="utf-8"))
            self.assertTrue(audit_report["passed"])

            state = json.loads((project / ".loopplane" / "planning" / "planning_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "plan_ready")
            transitions = [(item["from"], item["to"]) for item in state["transitions"]]
            self.assertIn(("auditing", "plan_revision_needed"), transitions)
            self.assertIn(("plan_revision_needed", "planning"), transitions)

            events = (project / ".loopplane" / "planning" / "planning_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("plan_revision_reason_recorded", events)
            self.assertIn("planning_state_transition", events)

    def test_revision_loop_stops_at_planner_iteration_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Keep producing a malformed plan.")
            set_max_planner_iterations(project, 2)
            script = r"""
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Still Malformed

- [ ] P0.T001: Missing required fields forever
  - acceptance: This draft remains malformed.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Readiness must fail.
  - max_attempts: 3
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_plan_revision_loop(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "plan_revision_needed")
            self.assertEqual(result["planner_iterations"], 2)
            self.assertEqual(len(result["revision_reasons"]), 2)
            state = json.loads((project / ".loopplane" / "planning" / "planning_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "plan_revision_needed")
            self.assertEqual(state["max_planner_iterations"], 2)
            self.assertFalse(state["ready_for_activation"])

    def test_revision_loop_does_not_mark_plan_ready_when_approval_policy_blocks_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Keep producing an approval-gated plan while approvals are disabled.")
            set_max_planner_iterations(project, 1)
            script = """
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(__PLAN__.replace("__WORKFLOW_ID__", workflow_id), encoding="utf-8")
""".replace("__PLAN__", repr(approval_required_plan_text("__WORKFLOW_ID__")))
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            result = run_plan_revision_loop(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "plan_revision_needed")
            self.assertEqual(result["planner_iterations"], 1)
            self.assertIn("approval_policy_disabled", result["revision_reasons"][0]["codes"])
            state = json.loads((project / ".loopplane" / "planning" / "planning_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "plan_revision_needed")
            self.assertFalse(state["ready_for_activation"])


class PlanActivationTest(unittest.TestCase):
    def test_loopplane_activate_plan_cli_promotes_draft_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Activate a generated plan.")
            configure_planner(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "activate-plan", "--project", str(project), "--json"],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"], json.dumps(data, indent=2, sort_keys=True))
            self.assertEqual(data["status"], "activated")
            active_plan = project / "PLAN.md"
            draft = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            self.assertIn("- active: true", active_plan.read_text(encoding="utf-8"))
            self.assertIn("- active: false", draft.read_text(encoding="utf-8"))
            self.assertTrue((project / ".loopplane" / "planning" / "activation_events.jsonl").is_file())
            self.assertTrue(Path(data["summary_path"]).is_file())
            self.assertTrue(Path(data["node_summary_path"]).is_file())

            runtime_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["status"], "active")
            self.assertEqual(runtime_state["active_plan"], "PLAN.md")
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            workflow_record = next(record for record in registry["workflows"] if record["workflow_id"] == data["workflow_id"])
            self.assertEqual(workflow_record["status"], "active")
            self.assertEqual(workflow_record["name"], "Baseline Requested Deliverables")
            self.assertEqual(workflow_record["workflow_title"], "Baseline Requested Deliverables")
            self.assertEqual(data["workflow_registry_update"]["status"], "workflow_active")

            self.assertEqual(data["before_checkpoint"]["checkpoint"]["reason"], "before_plan_activation")
            self.assertEqual(data["after_checkpoint"]["checkpoint"]["reason"], "after_plan_activation")
            for key in ("before_checkpoint", "after_checkpoint"):
                ref = data[key]["checkpoint"]["ref"]
                show_ref = subprocess.run(
                    ["git", "-C", str(project), "show-ref", "--verify", "--quiet", ref],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(show_ref.returncode, 0, ref)

            checkpoint_log = (project / ".loopplane" / "runtime" / "git_checkpoints.jsonl").read_text(encoding="utf-8")
            self.assertIn("before_plan_activation", checkpoint_log)
            self.assertIn("after_plan_activation", checkpoint_log)
            events = (project / ".loopplane" / "planning" / "activation_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("plan_activation_started", events)
            self.assertIn("plan_activated", events)
            self.assertIn("plan_activation_finished", events)

    def test_loopplane_activate_plan_cli_imports_plan_file_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Activate an imported deterministic plan.")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            imported_plan = Path(tmp) / "deterministic_PLAN.md"
            imported_plan.write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: deterministic fixture
- active: false

## Phase P0: Imported Plan

- [ ] P0.T001: Run deterministic fixture task
  - acceptance: Imported activation preserves this task.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: report_contains: IMPORTED-P0-T001-DONE
  - max_attempts: 3
  - approval: not_required
  - deliverables: Imported fixture report.

### Phase Objective Checklist

- [ ] `P0.O1` Imported deterministic plan evidence is reviewable.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the imported plan can support objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` Imported deterministic workflow has objective-verifiable handoff gates.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm imported workflow readiness can be judged by objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
""",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "activate-plan", "--project", str(project), "--file", str(imported_plan), "--json"],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"], json.dumps(data, indent=2, sort_keys=True))
            self.assertEqual(data["status"], "activated")
            self.assertEqual(data["readiness_report"]["draft_source"], "imported_file")
            self.assertEqual(Path(data["readiness_report"]["source_plan_file"]), imported_plan.resolve())
            active_text = (project / "PLAN.md").read_text(encoding="utf-8")
            draft_text = (project / ".loopplane" / "planning" / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            self.assertIn("Run deterministic fixture task", active_text)
            self.assertIn("- active: true", active_text)
            self.assertIn("Run deterministic fixture task", draft_text)
            self.assertIn("plan_draft_imported", (project / ".loopplane" / "planning" / "activation_events.jsonl").read_text(encoding="utf-8"))

    def test_activation_rejects_malformed_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject malformed draft.")
            draft = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            draft.write_text("not a parseable project plan\n", encoding="utf-8")

            result = activate_plan(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "blocked")
            self.assertIn("readiness_errors", result["blocker_codes"])
            self.assertIn("plan draft must include '# Project Plan'", result["errors"])
            self.assertIn("- plan_version: 0", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_activation_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject a draft missing required fields.")
            script = """
import os
import pathlib

draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Missing Fields

- [ ] P0.T001: Missing activation fields
  - acceptance: This task lacks required activation fields.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: Activation must reject the draft.
  - max_attempts: 3
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )
            plan_result = run_planner(project)
            self.assertFalse(plan_result["ok"])

            result = activate_plan(project)

            self.assertFalse(result["ok"])
            self.assertIn("missing_required_fields", result["blocker_codes"])
            self.assertIn("P0.T001: missing required field 'approval'", "\n".join(result["errors"]))
            self.assertIn("P0.T001: missing required field 'deliverables'", "\n".join(result["errors"]))
            self.assertIn("- plan_version: 0", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_activation_rejects_approval_required_plan_when_approval_policy_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Reject approval-gated plan without approval policy.")
            imported_plan = Path(tmp) / "approval_required_PLAN.md"
            imported_plan.write_text(approval_required_plan_text(initialized.workflow_id), encoding="utf-8")

            result = activate_plan(project, source_plan_file=imported_plan)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "blocked")
            self.assertIn("approval_policy_disabled", result["blocker_codes"])
            self.assertIn("interactive approvals are disabled", "\n".join(result["errors"]))
            self.assertIn("- plan_version: 0", (project / "PLAN.md").read_text(encoding="utf-8"))
            readiness = result["readiness_report"]
            self.assertFalse(readiness["ready_for_activation"])
            self.assertIn("approval_policy_disabled", readiness["activation_blocked_by"])

    def test_activation_rejects_blocking_readiness_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject unresolved readiness questions.")
            configure_planner(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))
            readiness_path = project / ".loopplane" / "planning" / "plan_readiness_report.json"
            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
            readiness["blocking_questions"] = ["Which deployment target should the worker use?"]
            readiness_path.write_text(json.dumps(readiness, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = activate_plan(project)

            self.assertFalse(result["ok"])
            self.assertIn("blocking_readiness_questions", result["blocker_codes"])
            self.assertIn("blocking readiness question remains", "\n".join(result["errors"]))
            self.assertIn("- plan_version: 0", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_activation_rejects_failed_required_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject a failed required audit.")
            configure_planner(project)
            require_plan_auditor(project)
            configure_auditor(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", "import sys; print('audit failed', file=sys.stderr); raise SystemExit(4)"],
                prompt_delivery={"mode": "stdin"},
            )
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))
            audit_result = run_auditor(project)
            self.assertFalse(audit_result["ok"])

            result = activate_plan(project)

            self.assertFalse(result["ok"])
            self.assertIn("audit_failed", result["blocker_codes"])
            self.assertIn("Required plan audit did not pass.", result["errors"])
            self.assertIn("- plan_version: 0", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_activation_rejects_protected_plan_overwrite_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject protected active plan overwrite.")
            configure_planner(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))
            active_plan = project / "PLAN.md"
            active_plan.write_text(
                """# Project Plan

## Metadata

- workflow_id: user_existing
- plan_version: 1
- active: true

## Phase P0: Existing

- [ ] P0.T001: Existing protected task
  - acceptance: Existing active plan must not be overwritten.
""",
                encoding="utf-8",
            )

            result = activate_plan(project)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "protected_overwrite_risk")
            self.assertIn("protected_plan_overwrite_risk", result["blocker_codes"])
            self.assertIn("Existing protected task", active_plan.read_text(encoding="utf-8"))

    def test_activation_import_failure_does_not_replace_canonical_planning_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Protect imported plan staging.")
            active_plan = project / "PLAN.md"
            active_plan.write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {initialized.workflow_id}
- plan_version: 1
- active: true

## Phase P0: Existing

- [ ] P0.T001: Existing protected task
  - acceptance: Existing active plan must not be overwritten.
""",
                encoding="utf-8",
            )
            planning_dir = project / ".loopplane" / "planning"
            existing_draft = planning_dir / "PLAN_DRAFT.md"
            existing_readiness = planning_dir / "plan_readiness_report.json"
            existing_draft.write_text("sentinel draft must remain\n", encoding="utf-8")
            existing_readiness.write_text('{"sentinel": true}\n', encoding="utf-8")
            imported_plan = Path(tmp) / "external_PLAN_DRAFT.md"
            imported_plan.write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {initialized.workflow_id}
- workflow_title: Imported staging fixture
- plan_version: 2
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Imported

- [ ] P0.T001: Imported task
  - acceptance: Imported acceptance is reviewable.
  - validation: file_exists: artifacts/imported.txt
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - max_attempts: 1
  - approval: not_required
  - deliverables: artifacts/imported.txt

### Phase Objective Checklist

- [ ] `P0.O1` Imported task satisfies the phase objective.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Judge imported task evidence.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 1

## Final Objective Checklist

- [ ] `FO1` Imported workflow is ready for handoff.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Judge final imported workflow readiness.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 1
""",
                encoding="utf-8",
            )

            result = activate_plan(project, source_plan_file=imported_plan)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "protected_overwrite_risk")
            self.assertIn("protected_plan_overwrite_risk", result["blocker_codes"])
            self.assertEqual(existing_draft.read_text(encoding="utf-8"), "sentinel draft must remain\n")
            self.assertEqual(existing_readiness.read_text(encoding="utf-8"), '{"sentinel": true}\n')
            self.assertIn("Existing protected task", active_plan.read_text(encoding="utf-8"))


class PlanReadinessInspectionTest(unittest.TestCase):
    def test_validation_strategy_lint_reports_command_and_report_marker_risks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "PLAN_DRAFT.md"
            draft.write_text(
                """# Project Plan

## Metadata

- workflow_id: wf_test
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Validation Lint

- [ ] P0.T001: Lint validation strategy
  - acceptance: Validation lint is reported.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: command_exit_code: python -c "import sys; print('x:y')"; report_contains: implementation completed successfully
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.

- [ ] P0.T002: Broken command separator
  - acceptance: Broken command separators are reported.
  - evidence: .loopplane/results/P0.T002/
  - latest: .loopplane/results/P0.T002/latest.json
  - depends_on: []
  - risk: low
  - validation: command_exit_code: python -c import sys; print('split')
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.

- [ ] P0.T003: Non-zero exit lint
  - acceptance: Non-zero exit expectations are supported.
  - evidence: .loopplane/results/P0.T003/
  - latest: .loopplane/results/P0.T003/latest.json
  - depends_on: []
  - risk: low
  - validation: command_exit_code: python app.py invalid returns nonzero
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.

- [ ] P0.T004: Invalid exit lint
  - acceptance: Invalid exit expectations are warned.
  - evidence: .loopplane/results/P0.T004/
  - latest: .loopplane/results/P0.T004/latest.json
  - depends_on: []
  - risk: low
  - validation: command_exit_code: python app.py invalid returns banana
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.

- [ ] P0.T005: Multiple file exists lint
  - acceptance: Comma separated file_exists paths are supported.
  - evidence: .loopplane/results/P0.T005/
  - latest: .loopplane/results/P0.T005/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md, README.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.

- [ ] P0.T006: Command output lint
  - acceptance: Structured stdout expectations are supported.
  - evidence: .loopplane/results/P0.T006/
  - latest: .loopplane/results/P0.T006/latest.json
  - depends_on: []
  - risk: low
  - validation: command_stdout_contains: python app.py ok contains READY; command_stderr_contains: python app.py bad contains expected failure
  - max_attempts: 3
  - approval: not_required
  - deliverables: Lint report.
""",
                encoding="utf-8",
            )

            report = inspect_plan_draft(draft, workflow_id="wf_test")

            self.assertTrue(report["valid"], report["errors"])
            warnings = "\n".join(report["warnings"])
            self.assertIn("report_contains target 'implementation completed successfully' looks prose-like", warnings)
            self.assertIn("unsupported validation clause \"print('split')\"", warnings)
            self.assertIn("P0.T004: command_exit_code expectation 'banana' is not supported", warnings)
            self.assertNotIn("P0.T003: command_exit_code", warnings)
            self.assertNotIn("P0.T005", warnings)
            self.assertNotIn("P0.T006", warnings)
            self.assertNotIn("print('x:y')", warnings)

    def test_missing_approval_and_deliverables_fail_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "PLAN_DRAFT.md"
            draft.write_text(
                """# Project Plan

## Metadata

- workflow_id: wf_test
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Missing Metadata

- [ ] P0.T001: Old structural task
  - acceptance: The task has the old required fields.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: A deterministic smoke check passes.
  - max_attempts: 3
""",
                encoding="utf-8",
            )

            report = inspect_plan_draft(draft, workflow_id="wf_test")

            self.assertFalse(report["valid"])
            self.assertIn("P0.T001: missing required field 'approval'", report["errors"])
            self.assertIn("P0.T001: missing required field 'deliverables'", report["errors"])

    def test_multiline_task_field_values_satisfy_required_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "PLAN_DRAFT.md"
            draft.write_text(
                """# Project Plan

## Metadata

- workflow_id: wf_test
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

### Phase 1: Acceptance Smoke Recording

- [ ] T001: Record Codex CLI acceptance smoke
  - acceptance_criteria:
    - A smoke record artifact is written.
    - Deterministic validation confirms the record.
  - evidence_root: .loopplane/results/wf_test/T001
  - latest_pointer_path: .loopplane/results/wf_test/T001/latest.json
  - dependencies: []
  - risk_level: low
  - validation_strategy: Inspect the task evidence run directory.
  - max_attempts: 2
  - approval: not_required
  - deliverables: Smoke record artifact.
""",
                encoding="utf-8",
            )

            report = inspect_plan_draft(draft, workflow_id="wf_test")

            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["task_ids"], ["T001"])

    def test_invalid_dependency_risk_and_retry_values_fail_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "PLAN_DRAFT.md"
            draft.write_text(
                """# Project Plan

## Metadata

- workflow_id: wf_test
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Invalid Fields

- [ ] P0.T001: Invalid references
  - acceptance: The task is malformed.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: [P9.T999]
  - risk: severe
  - validation: A deterministic smoke check passes.
  - max_attempts: 0
  - approval: not_required
  - deliverables: Smoke result
""",
                encoding="utf-8",
            )

            report = inspect_plan_draft(draft, workflow_id="wf_test")

            self.assertFalse(report["valid"])
            self.assertIn("P0.T001: unknown dependency 'P9.T999'", report["errors"])
            self.assertIn("P0.T001: invalid risk 'severe'", report["errors"])
            self.assertIn("P0.T001: max_attempts must be a positive integer", report["errors"])

    def test_blocked_and_skipped_metadata_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "PLAN_DRAFT.md"
            draft.write_text(
                """# Project Plan

## Metadata

- workflow_id: wf_test
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Blocked And Skipped

- [!] P0.T001: Missing blocked timestamp
  - acceptance: The task is blocked until user input exists.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: medium
  - validation: User-provided data is verified.
  - max_attempts: 2
  - approval: required
  - deliverables: Verified input manifest
  - blocked_reason: dataset unavailable
  - unblock_condition: user provides dataset path

- [-] P0.T002: Missing skip authorization
  - acceptance: The task is out of scope.
  - evidence: .loopplane/results/P0.T002/
  - latest: .loopplane/results/P0.T002/latest.json
  - depends_on: []
  - risk: low
  - validation: Skipped task has authorization.
  - max_attempts: 1
  - approval: not_required
  - deliverables: None; task skipped by contract
  - skip_reason: out of scope
""",
                encoding="utf-8",
            )

            report = inspect_plan_draft(draft, workflow_id="wf_test")

            self.assertFalse(report["valid"])
            self.assertIn("P0.T001: blocked task missing 'blocked_since' or 'detected_at'", report["errors"])
            self.assertIn("P0.T002: skipped task missing 'skip_authorization' or 'approval_id'", report["errors"])


class PlannerCliTest(unittest.TestCase):
    def test_loopplane_plan_cli_runs_noop_planner_and_emits_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create a plan through the CLI.")
            configure_planner(project)

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "plan", "--project", str(project), "--json"],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "ready_for_activation")
            self.assertTrue((project / ".loopplane" / "planning" / "PLAN_DRAFT.md").is_file())
            self.assertTrue(Path(data["adapter_result_path"]).is_file())

    def test_loopplane_plan_cli_emits_active_progress_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create a plan with visible progress.")
            script = """
import os
import pathlib
import sys
import time

sys.stdin.read()
time.sleep(0.4)
draft = pathlib.Path(os.environ["LOOPPLANE_PLAN_DRAFT_PATH"])
workflow_id = os.environ["LOOPPLANE_WORKFLOW_ID"]
draft.write_text(f'''# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: false

## Phase P0: Progress

- [ ] P0.T001: Progress task
  - acceptance: Planner progress is visible.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 3
  - approval: not_required
  - deliverables: Progress plan.

### Phase Objective Checklist

- [ ] `P0.O1` Progress planner evidence is reviewable.
  - evidence_scope: .loopplane/results/P0.T001/
  - judgment_guidance: Confirm the progress fixture plan supports objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2

## Final Objective Checklist

- [ ] `FO1` Progress fixture workflow has objective-verifiable handoff gates.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm final workflow readiness can be judged by objective verification.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
''', encoding="utf-8")
"""
            configure_planner(
                project,
                adapter="shell",
                command=sys.executable,
                args=["-c", script],
                prompt_delivery={"mode": "stdin"},
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "plan",
                    "--project",
                    str(project),
                    "--progress-interval",
                    "0.1",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"], json.dumps(data, indent=2, sort_keys=True))
            self.assertIn("[loopplane plan] active", completed.stderr)
            self.assertIn("run_id=plan_", completed.stderr)
            self.assertIn("run_dir=", completed.stderr)
            self.assertIn("new_files=", completed.stderr)

    def test_loopplane_plan_cli_runs_codex_fixture_planner_without_noop_or_waiting_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            fixture_bin = install_cli_adapter_fixture_bin(root)
            init_project(project, "Create a plan through a concrete Codex CLI adapter.")
            env = dict(os.environ)
            env["PATH"] = fixture_bin.as_posix() + os.pathsep + env.get("PATH", "")

            configured = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "configure-agent",
                    "--project",
                    str(project),
                    "--role",
                    "planner",
                    "--adapter",
                    "codex_cli",
                    "--command",
                    "codex",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr + configured.stdout)
            configured_data = json.loads(configured.stdout)
            self.assertEqual(configured_data["selected_runner_ids"], ["planner"])
            self.assertEqual(configured_data["runners"]["planner"]["adapter"], "codex_cli")
            self.assertEqual(configured_data["runners"]["planner"]["command"], "codex")

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "plan", "--project", str(project), "--json"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"], json.dumps(data, indent=2, sort_keys=True))
            self.assertEqual(data["status"], "ready_for_activation")
            self.assertEqual(data["adapter"], "codex_cli")
            self.assertEqual(data["runner_id"], "planner")
            self.assertNotEqual(data["status"], "waiting_config")

            run_dir = Path(data["run_dir"])
            plan_path = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            self.assertTrue(plan_path.is_file())
            self.assertIn("Codex fixture planner task", plan_path.read_text(encoding="utf-8"))
            self.assertTrue((run_dir / "PLAN_DRAFT.md").is_file())
            self.assertTrue((run_dir / "codex_fixture_record.json").is_file())

            readiness = json.loads((project / ".loopplane" / "planning" / "plan_readiness_report.json").read_text(encoding="utf-8"))
            self.assertEqual(readiness["adapter"], "codex_cli")
            self.assertEqual(readiness["draft_source"], "adapter")
            self.assertEqual(readiness["adapter_exit_code"], 0)
            self.assertTrue(readiness["ready_for_activation"])

            adapter_input = json.loads((run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_input["role"], "planner")
            self.assertEqual(adapter_input["adapter"], "codex_cli")
            self.assertIsNone(adapter_input["task_id"])
            self.assertIsNone(adapter_input["task_evidence_run_dir"])
            self.assertEqual(adapter_input["env"]["LOOPPLANE_ROLE"], "planner")

            adapter_result = json.loads(Path(data["adapter_result_path"]).read_text(encoding="utf-8"))
            self.assertEqual(adapter_result["adapter"], "codex_cli")
            self.assertEqual(adapter_result["role"], "planner")
            self.assertEqual(adapter_result["exit_code"], 0)
            self.assertTrue(adapter_result["adapter_metadata"]["external_execution"])
            self.assertEqual(adapter_result["adapter_metadata"]["delivery_mode"], "file_argument")
            self.assertEqual(
                adapter_result["adapter_metadata"]["argv"],
                [
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )
            self.assertEqual(adapter_result["adapter_metadata"]["policy_decision"]["allowed"], True)

            record = json.loads((run_dir / "codex_fixture_record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["fixture"], "codex")
            self.assertEqual(record["prompt_source"], "stdin")
            self.assertEqual(record["env"]["LOOPPLANE_ROLE"], "planner")
            self.assertEqual(record["env"]["LOOPPLANE_PLANNING_RUN_DIR"], run_dir.as_posix())
            self.assertEqual(record["env"]["LOOPPLANE_PLAN_DRAFT_PATH"], plan_path.as_posix())
            self.assertNotIn("Create a plan through a concrete Codex CLI adapter.", record["prompt"])
            context_manifest = json.loads((run_dir / "planner_context_manifest.json").read_text(encoding="utf-8"))
            self.assertIn(
                "Create a plan through a concrete Codex CLI adapter.",
                context_manifest["references"]["project_brief"]["excerpt"],
            )

    def test_loopplane_audit_plan_cli_runs_noop_auditor_and_emits_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create and audit a plan through the CLI.")
            configure_planner(project)
            configure_auditor(project)
            require_plan_auditor(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "audit-plan", "--project", str(project), "--json"],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "passed")
            self.assertTrue((project / ".loopplane" / "planning" / "audit_report.json").is_file())
            self.assertTrue(Path(data["adapter_result_path"]).is_file())

    def test_loopplane_audit_plan_cli_emits_active_progress_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Audit a plan with visible progress.")
            configure_planner(project)
            plan_result = run_planner(project)
            self.assertTrue(plan_result["ok"], json.dumps(plan_result, indent=2, sort_keys=True))
            configure_auditor(
                project,
                adapter="shell",
                command=sys.executable,
                args=[
                    "-c",
                    (
                        "import os, pathlib, sys, time; "
                        "sys.stdin.read(); "
                        "pathlib.Path(os.environ['LOOPPLANE_STDOUT_LOG']).write_text('auditor progress marker\\n', encoding='utf-8'); "
                        "time.sleep(0.4)"
                    ),
                ],
                prompt_delivery={"mode": "stdin"},
            )
            require_plan_auditor(project)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "audit-plan",
                    "--project",
                    str(project),
                    "--progress-interval",
                    "0.1",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            data = json.loads(completed.stdout)
            self.assertTrue(data["ok"], json.dumps(data, indent=2, sort_keys=True))
            self.assertIn("[loopplane audit-plan] active", completed.stderr)
            self.assertIn("run_id=audit_", completed.stderr)
            self.assertIn("run_dir=", completed.stderr)
            self.assertIn("new_files=", completed.stderr)
            self.assertIn("stdout_tail=auditor progress marker", completed.stderr)

    def test_repeated_loopplane_plan_preserves_run_specific_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create an idempotent planner history.")
            configure_planner(project)

            results = []
            for _index in range(2):
                completed = subprocess.run(
                    [sys.executable, str(LoopPlane), "plan", "--project", str(project), "--json"],
                    cwd=REPO_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                results.append(json.loads(completed.stdout))

            first, second = results
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertNotEqual(first["run_dir"], second["run_dir"])

            required_files = (
                "planner_prompt.md",
                "adapter_input.json",
                "stdout.log",
                "stderr.log",
                "final.md",
                "adapter_result.json",
                "PLAN_DRAFT.md",
                "plan_readiness_report.json",
                "planning_events.jsonl",
                "node_summary.json",
            )
            for result in results:
                run_dir = Path(result["run_dir"])
                self.assertTrue(run_dir.is_dir())
                for filename in required_files:
                    self.assertTrue((run_dir / filename).is_file(), f"{run_dir / filename} missing")

                draft = (run_dir / "PLAN_DRAFT.md").read_text(encoding="utf-8")
                self.assertIn(f"- planner_run_id: {result['run_id']}", draft)

                readiness = json.loads((run_dir / "plan_readiness_report.json").read_text(encoding="utf-8"))
                self.assertEqual(readiness["run_id"], result["run_id"])

                adapter_result = json.loads((run_dir / "adapter_result.json").read_text(encoding="utf-8"))
                self.assertEqual(adapter_result["run_id"], result["run_id"])
                self.assertEqual(adapter_result["stdout_path"], (run_dir / "stdout.log").as_posix())
                self.assertEqual(adapter_result["stderr_path"], (run_dir / "stderr.log").as_posix())
                self.assertEqual(adapter_result["final_output_path"], (run_dir / "final.md").as_posix())

                stdout = (run_dir / "stdout.log").read_text(encoding="utf-8")
                final = (run_dir / "final.md").read_text(encoding="utf-8")
                self.assertIn(result["run_id"], stdout)
                self.assertIn(result["run_id"], final)

                events = (run_dir / "planning_events.jsonl").read_text(encoding="utf-8")
                self.assertIn(result["run_id"], events)
                self.assertIn("planner_run_started", events)
                self.assertIn("planner_run_finished", events)

            first_run_draft = (Path(first["run_dir"]) / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            second_run_draft = (Path(second["run_dir"]) / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            latest_draft = (project / ".loopplane" / "planning" / "PLAN_DRAFT.md").read_text(encoding="utf-8")
            self.assertIn(f"- planner_run_id: {first['run_id']}", first_run_draft)
            self.assertNotIn(f"- planner_run_id: {second['run_id']}", first_run_draft)
            self.assertIn(f"- planner_run_id: {second['run_id']}", second_run_draft)
            self.assertIn(f"- planner_run_id: {second['run_id']}", latest_draft)


if __name__ == "__main__":
    unittest.main()
