from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.human_summaries import ensure_human_summaries
from runtime.objective_verification import apply_objective_verification_report
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.planning import acknowledge_active_plan_change
from runtime.prompt_builder import build_prompt_for_prepared_run
from runtime.scheduler import load_scheduler_snapshot, prepare_run, run_scheduler, select_next_action
from runtime.self_expansion import expansion_candidate, load_expansion_status


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def workflow_id(project: Path) -> str:
    return json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))["workflow_id"]


def record_active_plan(project: Path) -> None:
    state_path = project / ".loopplane" / "runtime" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["configuration_problems"] = []
    state["active_plan_sha256"] = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_fake_objective_verifier(project: Path) -> None:
    paths = WorkflowPaths.from_config(project, load_workflow_config(project))
    script = paths.config_file("fake_objective_verifier.py")
    script.write_text(
        r'''
import hashlib
import json
import os
import pathlib


project = pathlib.Path(os.environ["LOOPPLANE_PROJECT_ROOT"])
role_output_dir = pathlib.Path(os.environ["LOOPPLANE_ROLE_OUTPUT_DIR"])
selection = json.loads((role_output_dir / "objective_selection.json").read_text(encoding="utf-8"))
report_path = pathlib.Path(selection["objective_verification_report"])
if not report_path.is_absolute():
    report_path = project / report_path
plan_path_value = os.environ.get("LOOPPLANE_PLAN_FILE")
plan_path = pathlib.Path(plan_path_value) if plan_path_value else project / "PLAN.md"
if not plan_path.is_absolute():
    plan_path = project / plan_path
plan_text = plan_path.read_text(encoding="utf-8")
plan_sha = "sha256:" + hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
objective_results = []
for objective_id in selection.get("target_objective_ids", []):
    objective_results.append(
        {
            "objective_id": objective_id,
            "status": "satisfied",
            "verdict": "satisfied",
            "confidence": "high",
            "evidence_reviewed": [".loopplane/results"],
            "agent_rationale": "Fake verifier accepted the objective for scheduler-loop coverage.",
            "expandable": False,
        }
    )
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(
    json.dumps(
        {
            "schema_version": "1.5",
            "workflow_id": selection["workflow_id"],
            "scope": selection["scope"],
            "phase_id": selection.get("phase_id"),
            "phase_title": selection.get("phase_title"),
            "status": "satisfied",
            "verified_at": "2026-01-01T00:00:00Z",
            "plan_sha256": plan_sha,
            "objective_results": objective_results,
            "summary": {
                "total": len(objective_results),
                "passed": len(objective_results),
                "unmet": 0,
                "blocked": 0,
                "waived": 0,
            },
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
(role_output_dir / "agent_status.json").write_text(
    json.dumps(
        {
            "schema_version": "1.5",
            "run_id": selection["run_id"],
            "role": "objective_verifier",
            "status": "completed",
            "objective_verification_report": str(report_path),
            "summary_candidate": {"one_line": "Fake objective verifier completed."},
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake objective verifier wrote report")
''',
        encoding="utf-8",
    )
    config_path = paths.config_file("agent_runners.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["objective_verifier"].update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "enabled": True,
        }
    )
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_runner_enabled(project: Path, runner_id: str, enabled: bool) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"].setdefault(runner_id, {})["enabled"] = enabled
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_objective_plan(project: Path, *, first_status: str = "x", second_status: str = " ", max_expansions: int = 2) -> None:
    set_runner_enabled(project, "final_reviewer", False)
    wid = workflow_id(project)
    blocked_fields = (
        "  - blocked_reason: Fixture blocked terminal state.\n"
        "  - unblock_condition: Objective gate can evaluate after blocked terminal state.\n"
        "  - detected_at: 2026-01-01T00:00:00Z\n"
        if first_status == "!"
        else ""
    )
    (project / "PLAN.md").write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {wid}
- workflow_title: Objective Gate Fixture
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Produce Baseline

- [{first_status}] T001: Produce baseline table
  - acceptance: Baseline table exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: not_required
  - deliverables: report.md
{blocked_fields}

### Phase Objective Checklist

- [ ] `PO1` Baseline table is sufficient for downstream analysis.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Judge whether the table is complete enough to support the next phase.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: {max_expansions}

## Phase P1: Use Baseline

- [{second_status}] T002: Use baseline table
  - acceptance: Downstream result exists.
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
    record_active_plan(project)


class ObjectiveGateTest(unittest.TestCase):
    def test_phase_objective_gate_blocks_later_worker_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective gate fixture")
            write_objective_plan(project)

            snapshot = load_scheduler_snapshot(project)
            selected = select_next_action(snapshot)

            self.assertEqual(selected["action"], "run_phase_objective_verifier")
            self.assertEqual(selected["selected"]["target_objective_ids"], ["PO1"])

    def test_phase_objective_gate_does_not_open_when_phase_tasks_cannot_be_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective gate missing phase match fixture")
            write_objective_plan(project)

            snapshot = load_scheduler_snapshot(project)
            for task in snapshot["tasks"]:
                task["phase"] = None
            selected = select_next_action(snapshot)

            self.assertNotEqual(selected["action"], "run_phase_objective_verifier")

    def test_objective_verifier_prompt_references_evidence_context_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective verifier prompt fixture")
            write_objective_plan(project)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            run = prepare_run(project, role="objective_verifier", runner_id="objective_verifier", scheduler_owner="test")
            report_path = paths.runtime_dir / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                Path(run.role_output_dir) / "objective_selection.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": workflow_id(project),
                    "run_id": run.run_id,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "target_objective_ids": ["PO1"],
                    "objective_verification_report": report_path.relative_to(project).as_posix(),
                },
            )

            built = build_prompt_for_prepared_run(project, run)

            self.assertIn("objective_context_manifest.json", built.content)
            self.assertIn("objective_facts.json", built.content)
            self.assertNotIn('"artifact_inventory":', built.content)
            facts = json.loads((Path(run.role_output_dir) / "objective_facts.json").read_text(encoding="utf-8"))
            self.assertIn("artifact_inventory", facts)
            manifest = json.loads((Path(run.role_output_dir) / "objective_context_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["references"]["objective_facts"]["path"], ".loopplane/runtime/objectives/" + run.run_id + "/objective_facts.json")

    def test_objective_apply_authorizes_plan_marker_update_and_keeps_report_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective apply freshness fixture")
            write_objective_plan(project)
            wid = workflow_id(project)
            initial_plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            report_path = project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "satisfied",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": initial_plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "satisfied",
                            "verdict": "satisfied",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "Baseline table satisfies the phase objective.",
                            "expandable": False,
                        }
                    ],
                    "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
                },
            )

            result = apply_objective_verification_report(project, report_path)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("- [x] `PO1` Baseline table is sufficient", (project / "PLAN.md").read_text(encoding="utf-8"))
            current_plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_plan_sha256"], current_plan_sha)
            self.assertNotEqual(current_plan_sha, initial_plan_sha)
            applied_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(applied_report["accepted_plan_sha256"], current_plan_sha)
            self.assertIn("objective_structure_fingerprint", applied_report)
            rebuild_request = json.loads(
                (project / ".loopplane" / "runtime" / "read_model_rebuild_request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuild_request["reason"], "objective_verification_applied")
            self.assertEqual(rebuild_request["status"], "pending")
            gate = load_scheduler_snapshot(project)["objective_reports"]["by_key"]["phase:P0"]
            self.assertTrue(gate["fresh"], json.dumps(gate, indent=2, sort_keys=True))
            self.assertEqual(gate["status"], "closed")

    def test_objective_apply_refreshes_phase_human_summary_after_gate_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective summary refresh fixture")
            write_objective_plan(project)
            wid = workflow_id(project)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            initial_summary = ensure_human_summaries(project)
            self.assertTrue(initial_summary["ok"], json.dumps(initial_summary, indent=2, sort_keys=True))
            phase_summary_path = paths.results_dir / "phases" / "P0" / "human_summary.json"
            before_summary = json.loads(phase_summary_path.read_text(encoding="utf-8"))
            before_source = before_summary["source_hashes"]["summary_source"]
            self.assertEqual(before_summary["tables"]["phase_objective_closure"][0]["Closed"], "no")

            initial_plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            report_path = project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "satisfied",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": initial_plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "satisfied",
                            "verdict": "satisfied",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "Baseline table satisfies the phase objective.",
                            "expandable": False,
                        }
                    ],
                    "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
                },
            )

            result = apply_objective_verification_report(project, report_path)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["human_summaries"]["status"], "summaries_updated")
            after_summary = json.loads(phase_summary_path.read_text(encoding="utf-8"))
            self.assertNotEqual(after_summary["source_hashes"]["summary_source"], before_source)
            self.assertEqual(after_summary["tables"]["phase_objective_closure"][0]["Closed"], "yes")
            self.assertNotIn("stale", after_summary.get("status", ""))

    def test_self_expansion_status_derives_objective_resolution_from_current_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective expansion status fixture")
            write_objective_plan(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace(
                    "- [ ] `PO1` Baseline table is sufficient",
                    "- [x] `PO1` Baseline table is sufficient",
                ),
                encoding="utf-8",
            )
            wid = workflow_id(project)
            write_json(
                project / ".loopplane" / "runtime" / "expansion_registry.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "cycle": 1,
                    "events": [],
                    "proposals": [
                        {
                            "proposal_id": "exp_objective_gap",
                            "status": "applied",
                            "expansion_type": "objective_gap",
                            "target_objective_ids": ["PO1"],
                            "objective_resolution_status": "followup_pending",
                        }
                    ],
                },
            )

            status = load_expansion_status(project)

            self.assertEqual(status["latest_proposal"]["objective_resolution_status"], "resolved")

    def test_acknowledge_plan_releases_waiting_config_after_manual_plan_problem_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Acknowledge plan waiting_config fixture")
            write_objective_plan(project)
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "waiting_config"
            state["configuration_problems"] = [
                {
                    "code": "manual_plan_change_detected",
                    "message": "PLAN.md changed outside an authorized update.",
                }
            ]
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = acknowledge_active_plan_change(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "active")
            self.assertEqual(state["configuration_problems"], [])

    def test_scheduler_continues_after_successful_objective_verifier_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective verifier continuation fixture")
            configure_fake_objective_verifier(project)
            write_objective_plan(project, second_status="x")

            result = run_scheduler(project, max_ticks=2)

            self.assertEqual(result["ticks_run"], 2, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["action_history"][0]["action"], "run_phase_objective_verifier")
            self.assertNotEqual(result["stopped_reason"], "selected_action_terminal")

    def test_fresh_closed_objective_report_skips_redundant_objective_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective verifier cache fixture")
            write_objective_plan(project, second_status=" ")
            wid = workflow_id(project)
            plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            report_path = project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "satisfied",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "satisfied",
                            "verdict": "satisfied",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "Baseline table satisfies the phase objective.",
                            "expandable": False,
                        }
                    ],
                    "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
                },
            )

            snapshot = load_scheduler_snapshot(project)
            selected = select_next_action(snapshot)

            self.assertEqual(snapshot["objective_reports"]["by_key"]["phase:P0"]["status"], "closed")
            self.assertEqual(selected["action"], "run_worker")
            self.assertEqual(selected["selected"]["task_id"], "T002")

    def test_objective_gap_report_becomes_self_expansion_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Objective gap fixture")
            write_objective_plan(project)
            wid = workflow_id(project)
            plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            report_path = project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "unmet",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "unmet",
                            "verdict": "unmet_expandable",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "Baseline table lacks downstream-ready checks.",
                            "expandable": True,
                        }
                    ],
                    "summary": {"total": 1, "passed": 0, "unmet": 1, "blocked": 0, "waived": 0},
                },
            )

            snapshot = load_scheduler_snapshot(project)
            candidate = expansion_candidate(snapshot, mode="objective_gap")

            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(candidate["trigger"], "phase_objective_gap")
            self.assertEqual(candidate["expansion_type"], "objective_gap")
            self.assertEqual(candidate["target_objective_ids"], ["PO1"])

    def test_unmet_repeated_uses_remaining_objective_expansion_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Repeated objective gap with remaining budget fixture")
            write_objective_plan(project, max_expansions=50)
            wid = workflow_id(project)
            plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            report_path = project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "unmet",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "unmet",
                            "verdict": "unmet_repeated",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "The latest follow-up remains insufficient.",
                            "gap_summary": "A materially different expansion is still possible.",
                            "unmet_action": "escalate_unresolved",
                            "expandable": False,
                        }
                    ],
                    "summary": {"total": 1, "passed": 0, "unmet": 1, "blocked": 0, "waived": 0},
                },
            )
            write_json(
                project / ".loopplane" / "runtime" / "expansion_registry.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "cycle": 5,
                    "events": [],
                    "proposals": [
                        {
                            "proposal_id": f"exp_prior_{index}",
                            "workflow_id": wid,
                            "status": "applied",
                            "created_at": "2026-01-01T00:00:00Z",
                            "expansion_type": "objective_gap",
                            "resolution_strategy": "append_followup_only",
                            "loop_signature": f"sig-{index}",
                            "target_objective_ids": ["PO1"],
                        }
                        for index in range(5)
                    ],
                },
            )

            apply_result = apply_objective_verification_report(project, report_path)
            self.assertTrue(apply_result["ok"], json.dumps(apply_result, indent=2, sort_keys=True))
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [ ] `PO1` Baseline table is sufficient", plan_text)
            self.assertNotIn("- [!] `PO1`", plan_text)

            snapshot = load_scheduler_snapshot(project)
            gate = snapshot["objective_reports"]["by_key"]["phase:P0"]
            self.assertEqual(gate["status"], "needs_expansion")
            self.assertEqual(gate["expandable_objective_ids"], ["PO1"])
            self.assertEqual(gate["objective_expansion_counts"]["PO1"], 5)
            self.assertEqual(gate["objective_expansion_limits"]["PO1"], 50)
            candidate = expansion_candidate(snapshot, mode="objective_gap")
            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(candidate["target_objective_ids"], ["PO1"])
            selected = select_next_action(snapshot)
            self.assertEqual(selected["action"], "run_expansion_planner")

    def test_blocked_task_is_terminal_for_phase_objective_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Blocked terminal objective gate fixture")
            write_objective_plan(project, first_status="!")

            snapshot = load_scheduler_snapshot(project)
            selected = select_next_action(snapshot)

            self.assertEqual(selected["action"], "run_phase_objective_verifier")
            self.assertEqual(selected["selected"]["target_objective_ids"], ["PO1"])

    def test_repeated_objective_gap_enters_objective_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_project(project, "Repeated objective gap fixture")
            write_objective_plan(project, max_expansions=1)
            wid = workflow_id(project)
            plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            write_json(
                project / ".loopplane" / "runtime" / "objectives" / "phases" / "P0" / "objective_verification.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "scope": "phase",
                    "phase_id": "P0",
                    "phase_title": "Phase P0: Produce Baseline",
                    "status": "unmet",
                    "verified_at": "2026-01-01T00:00:00Z",
                    "plan_sha256": plan_sha,
                    "objective_results": [
                        {
                            "objective_id": "PO1",
                            "status": "unmet",
                            "verdict": "unmet_expandable",
                            "confidence": "high",
                            "evidence_reviewed": [".loopplane/results/T001/latest.json"],
                            "agent_rationale": "The same objective gap remains after expansion.",
                            "expandable": True,
                        }
                    ],
                    "summary": {"total": 1, "passed": 0, "unmet": 1, "blocked": 0, "waived": 0},
                },
            )
            write_json(
                project / ".loopplane" / "runtime" / "expansion_registry.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": wid,
                    "cycle": 1,
                    "events": [],
                    "proposals": [
                        {
                            "proposal_id": "exp_prior",
                            "workflow_id": wid,
                            "status": "applied",
                            "created_at": "2026-01-01T00:00:00Z",
                            "expansion_type": "objective_gap",
                            "resolution_strategy": "append_followup_only",
                            "loop_signature": "sig",
                            "target_objective_ids": ["PO1"],
                        }
                    ],
                },
            )

            selected = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(selected["action"], "requires_attention")
            self.assertEqual(selected["selected"]["type"], "objective_unresolved")
            self.assertEqual(selected["selected"]["target_objective_ids"], ["PO1"])

            run_scheduler(project, max_ticks=1)
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "objective_unresolved")
            self.assertIn("- [!] `PO1` Baseline table is sufficient", (project / "PLAN.md").read_text(encoding="utf-8"))
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            current = [record for record in registry["workflows"] if record["workflow_id"] == wid][0]
            self.assertEqual(current["status"], "objective_unresolved")


if __name__ == "__main__":
    unittest.main()
