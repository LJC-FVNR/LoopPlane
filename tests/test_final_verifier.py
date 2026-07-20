from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.final_verifier import final_verification_report_freshness, run_final_verifier
from runtime.init_workflow import init_project
from runtime.plan_objectives import objective_structure_fingerprint, parse_plan_objectives
from runtime.scheduler import append_event, completion_marker_status, load_scheduler_context, run_scheduler
from tests.test_validation import set_runner_enabled


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def write_final_plan(project: Path, *, task_status: str = "x", include_skip_authorization: bool = True) -> None:
    set_runner_enabled(project, "final_reviewer", False)
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    skip_authorization = "  - skip_authorization: contract:non-goals\n" if include_skip_authorization else ""
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Final Verification Fixture

- [{task_status}] T001: Build final deliverable
  - acceptance: Final deliverable exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: deterministic fixture validation
  - max_attempts: 3
  - approval: not_required
  - deliverables: final artifact

- [-] T002: Skip out-of-scope deployment
  - acceptance: Deployment is intentionally out of scope.
  - evidence: .loopplane/results/T002/
  - depends_on: []
  - risk: low
  - validation: skipped by authorization
  - max_attempts: 1
  - approval: not_required
  - deliverables: none
  - skip_reason: Public deployment is out of scope.
{skip_authorization}
## Final Objective Checklist

- [ ] `FO1` Final deliverable is acceptable for handoff.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Decide whether the final deliverable can be handed off.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def write_completed_task_evidence(project: Path, *, validation_status: str = "pass", warnings: list[str] | None = None) -> None:
    run_dir = project / ".loopplane" / "results" / "T001" / "runs" / "run_T001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("final deliverable evidence\n", encoding="utf-8")
    validation = {
        "schema_version": "1.5",
        "run_id": "run_T001",
        "primary_task_id": "T001",
        "status": validation_status,
        "verdict": "accepted",
        "accepted_task_ids": ["T001"],
        "rejected_task_ids": [],
        "task_results": [{"task_id": "T001", "status": validation_status}],
        "failures": [],
        "warnings": list(warnings or []),
    }
    (run_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest = {
        "schema_version": "1.5",
        "task_id": "T001",
        "latest_run_id": "run_T001",
        "latest_run_dir": ".loopplane/results/T001/runs/run_T001",
        "validation_path": ".loopplane/results/T001/runs/run_T001/validation.json",
        "validation_status": validation_status,
        "updated_at": "2026-06-10T00:00:00Z",
        "updated_by": "test",
    }
    (project / ".loopplane" / "results" / "T001" / "latest.json").write_text(
        json.dumps(latest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_final_objective_report(project, status="satisfied")


def append_final_objective(project: Path) -> None:
    plan_path = project / "PLAN.md"
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8")
        + """

## Final Objective Checklist

- [ ] `FO1` Final deliverable is acceptable for handoff.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Decide whether the final deliverable can be handed off.
  - verifier: objective_verifier
  - unmet_action: self_expand
""",
        encoding="utf-8",
    )


def write_final_objective_report(project: Path, *, status: str = "waived", policy_reason: str | None = None) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
    plan_sha = "sha256:" + sha256(plan_text.encode("utf-8")).hexdigest()
    objectives, _errors = parse_plan_objectives(plan_text)
    selected_objectives = [objective for objective in objectives if objective.scope == "workflow"]
    result_status = "satisfied" if status == "satisfied" else "waived"
    verdict = "satisfied" if result_status == "satisfied" else "waived_by_policy"
    result: dict[str, object] = {
        "objective_id": "FO1",
        "status": result_status,
        "verdict": verdict,
        "confidence": "high",
        "evidence_reviewed": [".loopplane/results/T001/latest.json"],
        "agent_rationale": "Satisfied for test." if result_status == "satisfied" else "Waived for test.",
        "expandable": False,
    }
    if policy_reason is not None:
        result["policy_reason"] = policy_reason
    report_path = project / ".loopplane" / "runtime" / "objectives" / "final_objective_verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "workflow_id": workflow["workflow_id"],
                "scope": "workflow",
                "phase_id": None,
                "phase_title": None,
                "status": result_status,
                "verified_at": "2026-06-10T00:00:00Z",
                "plan_sha256": plan_sha,
                "objective_structure_fingerprint": objective_structure_fingerprint(
                    plan_text,
                    objectives=selected_objectives,
                ),
                "objective_results": [result],
                "summary": {
                    "total": 1,
                    "passed": 1 if result_status == "satisfied" else 0,
                    "unmet": 0,
                    "blocked": 0,
                    "waived": 1 if result_status == "waived" else 0,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def configure_fake_final_reviewer(project: Path, *, status: str = "accepted") -> None:
    script = project / ".loopplane" / "config" / "fake_final_reviewer.py"
    script.write_text(
        r'''
import json
import os
import pathlib


review_path = pathlib.Path(os.environ["LOOPPLANE_FINAL_REVIEWER_REPORT_PATH"])
missing = [
    name
    for name in (
        "LOOPPLANE_PROJECT_ROOT",
        "LOOPPLANE_ROLE_OUTPUT_DIR",
        "LOOPPLANE_RUNTIME_DIR",
        "LOOPPLANE_PLAN_FILE",
        "LOOPPLANE_WORKFLOW_ID",
        "LOOPPLANE_RUN_ID",
    )
    if not os.environ.get(name)
]
if missing:
    raise SystemExit("missing final reviewer env: " + ", ".join(missing))
review_path.parent.mkdir(parents=True, exist_ok=True)
review_path.write_text(
    json.dumps(
        {
            "schema_version": "1.0",
            "workflow_id": os.environ.get("LOOPPLANE_WORKFLOW_ID"),
            "run_id": os.environ.get("LOOPPLANE_RUN_ID"),
            "status": "__STATUS__",
            "confidence": "high",
            "rationale": "The final reviewer agent found a semantic handoff gap.",
            "findings": ["handoff synthesis is missing"],
            "evidence_reviewed": ["PLAN.md", ".loopplane/runtime/evidence_manifest.json"],
            "residual_risks": ["project owner cannot accept completion yet"],
            "recommended_action": "self_expand",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake final reviewer wrote final_reviewer_report.json")
'''.replace("__STATUS__", status),
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["final_reviewer"].update(
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


def configure_usage_limited_final_reviewer(project: Path) -> None:
    script = project / ".loopplane" / "config" / "usage_limited_final_reviewer.py"
    script.write_text(
        "import sys\n"
        "print(\"ERROR: You've hit your usage limit. Try again at Jul 25th, 2026 3:25 AM.\", file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["final_reviewer"].update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "adapter_options": {"runner_availability": {"builtin_classifiers": True}},
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


def jsonl_line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


class FinalVerifierTest(unittest.TestCase):
    def test_report_records_deterministic_plan_and_authoritative_evidence_input_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final verification input fingerprint fixture.")
            write_final_plan(project)
            configure_fake_final_reviewer(project, status="rejected")
            write_completed_task_evidence(project)

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail")
            report = json.loads(
                (project / ".loopplane" / "runtime" / "final_verification_report.json").read_text(encoding="utf-8")
            )
            self.assertRegex(report["input_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            components = report["input_fingerprint_components"]
            self.assertEqual(
                components["active_plan_sha256"],
                "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest(),
            )
            self.assertRegex(components["task_validation_evidence_sha256"], r"^sha256:[0-9a-f]{64}$")
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(final_verification_report_freshness(paths, report)["fresh"])

            evidence_report = project / ".loopplane" / "results" / "T001" / "runs" / "run_T001" / "report.md"
            evidence_report.write_text("final deliverable evidence changed\n", encoding="utf-8")
            freshness = final_verification_report_freshness(paths, report)
            self.assertFalse(freshness["fresh"])
            self.assertIn("task_validation_evidence_sha256_mismatch", freshness["stale_reasons"])

    def test_missing_objective_checklist_blocks_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Missing objective final verification fixture.")
            write_final_plan(project)
            plan_path = project / "PLAN.md"
            plan_text = plan_path.read_text(encoding="utf-8").split("## Final Objective Checklist", 1)[0]
            plan_path.write_text(plan_text, encoding="utf-8")
            write_completed_task_evidence(project)

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail")
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            blocker = next(item for item in result["blockers"] if item["check"] == "objectives_closed_by_agentic_verification")
            self.assertEqual(blocker["details"]["total"], 0)

    def test_complete_fixture_writes_report_manifest_and_fresh_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Complete final verification fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["scheduler"]["running"] = True
            state["scheduler"]["active_run_id"] = "run_stale"
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "pass")
            self.assertTrue((project / ".loopplane" / "runtime" / "evidence_manifest.json").is_file())
            self.assertTrue((project / ".loopplane" / "runtime" / "final_verification_report.json").is_file())
            self.assertTrue(result["projection_sync"]["ok"])
            self.assertTrue(
                any(check["check"] == "evidence_manifest_schema_valid" and check["status"] == "pass" for check in result["checks"])
            )
            self.assertTrue(
                any(check["check"] == "active_workflow_projections_current" and check["status"] == "pass" for check in result["checks"])
            )
            manifest = json.loads((project / ".loopplane" / "runtime" / "evidence_manifest.json").read_text(encoding="utf-8"))
            self.assertIsInstance(manifest["tasks"], dict)
            self.assertEqual(manifest["tasks"]["T001"]["task_id"], "T001")
            marker_path = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(marker_path.is_file())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "completed")
            for field in (
                "plan_sha256",
                "event_log_head",
                "evidence_manifest_sha256",
                "final_verification_report_sha256",
                "final_git_checkpoint_id",
                "state_fingerprint",
            ):
                self.assertIn(field, marker)
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(completion_marker_status(paths)["fresh"])
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(final_state["scheduler"]["running"])
            self.assertIsNone(final_state["scheduler"]["active_run_id"])

    def test_enabled_final_reviewer_agent_blocks_completion_marker_on_semantic_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Semantic final review fixture.")
            write_final_plan(project)
            configure_fake_final_reviewer(project, status="rejected")
            write_completed_task_evidence(project)

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail", json.dumps(result, indent=2, sort_keys=True))
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            self.assertEqual(result["final_reviewer"]["review"]["status"], "rejected")
            blocker = next(item for item in result["blockers"] if item["check"] == "semantic_final_review")
            self.assertEqual(blocker["details"]["recommended_action"], "self_expand")
            self.assertTrue(blocker["expandable"])
            self.assertEqual(blocker["kind"], "semantic_final_review")
            self.assertEqual(blocker["suggested_expansion_type"], "final_verifier_retry")

    def test_final_reviewer_usage_limit_creates_runner_hold_and_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Usage-limited final reviewer fixture.")
            write_final_plan(project)
            configure_usage_limited_final_reviewer(project)
            write_completed_task_evidence(project)

            first = run_scheduler(project, max_ticks=1)

            self.assertEqual(first["selected_action"]["action"], "run_final_verification")
            execution = first["selected_action"]["execution_result"]
            self.assertEqual(execution["status"], "waiting_runner_availability")
            self.assertEqual(execution["runner_availability"]["reason_class"], "usage_limit_exhausted")
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            paths = load_scheduler_context(project)["context"].paths
            runner_health = json.loads((paths.runtime_dir / "runner_health.json").read_text(encoding="utf-8"))
            hold = runner_health["runners"]["final_reviewer"]["availability_hold"]
            self.assertEqual(hold["status"], "active")
            self.assertEqual(hold["reason_class"], "usage_limit_exhausted")
            self.assertGreater(hold["retry_after_seconds"], 5 * 24 * 60 * 60)

            second = run_scheduler(project, max_ticks=1)

            self.assertEqual(second["selected_action"]["action"], "wait_runner_availability")
            self.assertEqual(second["selected_action"]["selected"]["roles"], ["final_reviewer"])

    def test_enabled_final_reviewer_agent_can_accept_over_narrow_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final reviewer false-negative fallback fixture.")
            write_final_plan(project)
            configure_fake_final_reviewer(project, status="accepted")
            write_completed_task_evidence(project, validation_status="fail")

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "pass", json.dumps(result, indent=2, sort_keys=True))
            self.assertTrue((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            latest_check = next(
                check
                for check in result["checks"]
                if check["check"] == "all_completed_tasks_have_latest_and_passing_validation"
            )
            self.assertEqual(latest_check["status"], "pass")
            self.assertIn("overridden_by_final_reviewer", latest_check["details"])
            self.assertTrue(
                any("deterministic final verifier blocker" in warning for warning in result["warnings"]),
                json.dumps(result["warnings"], indent=2),
            )

    def test_final_reviewer_acceptance_does_not_override_missing_skip_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final reviewer hard-gate fixture.")
            write_final_plan(project, include_skip_authorization=False)
            configure_fake_final_reviewer(project, status="accepted")
            write_completed_task_evidence(project)

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail", json.dumps(result, indent=2, sort_keys=True))
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            self.assertEqual(result["final_reviewer"]["status"], "skipped")
            self.assertEqual(result["final_reviewer"]["reason"], "deterministic_hard_blockers")
            blocker = next(item for item in result["blockers"] if item["check"] == "skipped_tasks_authorized")
            self.assertEqual(blocker["kind"], "non_expandable")

    def test_objective_report_stays_fresh_when_only_objective_checkbox_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Objective structure freshness fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace("- [ ] `FO1`", "- [x] `FO1`", 1),
                encoding="utf-8",
            )

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "pass", json.dumps(result, indent=2, sort_keys=True))

    def test_waived_objective_requires_policy_reason_and_exposes_target_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Waived objective policy reason fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            write_final_objective_report(project)

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail")
            blocker = next(item for item in result["blockers"] if item["check"] == "objectives_closed_by_agentic_verification")
            self.assertEqual(blocker["kind"], "objective_gap")
            self.assertEqual(blocker["target_objective_ids"], ["FO1"])
            self.assertTrue(
                any("missing an explicit policy reason" in error for error in blocker["details"]["errors"]),
                json.dumps(blocker, indent=2, sort_keys=True),
            )

    def test_fresh_completion_marker_makes_final_verifier_read_only_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Idempotent final verification fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            first = run_final_verifier(project, owner="test")
            self.assertEqual(first["status"], "pass")
            marker_path = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            checkpoints_path = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
            marker_before = marker_path.read_text(encoding="utf-8")
            checkpoint_count_before = jsonl_line_count(checkpoints_path)

            second = run_final_verifier(project, owner="test")

            self.assertEqual(second["status"], "pass")
            self.assertTrue(second["write_requested"])
            self.assertFalse(second["write_performed"])
            self.assertTrue(any("Existing completion marker is fresh" in warning for warning in second["warnings"]))
            self.assertEqual(marker_path.read_text(encoding="utf-8"), marker_before)
            self.assertEqual(jsonl_line_count(checkpoints_path), checkpoint_count_before)

            forced = run_final_verifier(project, owner="test", force=True)

            self.assertEqual(forced["status"], "pass")
            self.assertTrue(forced["write_requested"])
            self.assertTrue(forced["write_performed"])
            self.assertGreater(jsonl_line_count(checkpoints_path), checkpoint_count_before)

    def test_pass_with_warnings_validations_are_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final verification warnings fixture.")
            write_final_plan(project)
            write_completed_task_evidence(
                project,
                validation_status="pass_with_warnings",
                warnings=["command_stdout_contains accepted with advisory mismatch"],
            )

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "pass")
            self.assertTrue(any("T001: validation passed with warnings" in warning for warning in result["warnings"]))
            latest_check = next(
                check for check in result["checks"] if check["check"] == "all_completed_tasks_have_latest_and_passing_validation"
            )
            self.assertEqual(latest_check["status"], "pass")
            self.assertTrue(latest_check["details"]["warnings"])
            manifest = json.loads((project / ".loopplane" / "runtime" / "evidence_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["tasks"]["T001"]["validation_warnings"],
                ["command_stdout_contains accepted with advisory mismatch"],
            )

    def test_completion_marker_invalidates_when_fingerprint_components_change(self) -> None:
        def mutate_plan(project: Path) -> None:
            plan_path = project / "PLAN.md"
            plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n<!-- changed after completion -->\n", encoding="utf-8")

        def mutate_evidence_manifest(project: Path) -> None:
            manifest_path = project / ".loopplane" / "runtime" / "evidence_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["changed_after_completion"] = True
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_final_report(project: Path) -> None:
            report_path = project / ".loopplane" / "runtime" / "final_verification_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["changed_after_completion"] = True
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        def mutate_git_checkpoint(project: Path) -> None:
            checkpoints_path = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
            with checkpoints_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"checkpoint_id": "gitcp_changed_after_completion", "status": "created"}, sort_keys=True) + "\n")

        cases = [
            ("plan_sha256_mismatch", mutate_plan),
            ("evidence_manifest_sha256_mismatch", mutate_evidence_manifest),
            ("final_verification_report_sha256_mismatch", mutate_final_report),
            ("final_git_checkpoint_id_mismatch", mutate_git_checkpoint),
        ]
        for expected_reason, mutate in cases:
            with self.subTest(expected_reason=expected_reason):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    init_project(project, f"Freshness invalidation fixture {expected_reason}.")
                    write_final_plan(project)
                    write_completed_task_evidence(project)
                    result = run_final_verifier(project, owner="test")
                    self.assertEqual(result["status"], "pass")
                    paths = load_scheduler_context(project)["context"].paths
                    self.assertTrue(completion_marker_status(paths)["fresh"])

                    mutate(project)

                    marker_status = completion_marker_status(paths)
                    self.assertFalse(marker_status["fresh"], json.dumps(marker_status, indent=2, sort_keys=True))
                    self.assertIn(expected_reason, marker_status["stale_reasons"])

    def test_completion_marker_remains_fresh_after_post_completion_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Post-completion audit event fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            result = run_final_verifier(project, owner="test")
            self.assertEqual(result["status"], "pass")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(completion_marker_status(paths)["fresh"])

            append_event(paths, workflow_id=workflow["workflow_id"], event_type="summary_finished", data={"source": "test"})

            marker_status = completion_marker_status(paths)
            self.assertTrue(marker_status["fresh"], json.dumps(marker_status, indent=2, sort_keys=True))
            self.assertEqual(marker_status["stale_reasons"], [])
            self.assertIn("event_log_head_mismatch", marker_status["audit_drift_reasons"])
            self.assertIn("state_fingerprint_mismatch", marker_status["audit_drift_reasons"])

    def test_incomplete_fixture_fails_without_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Incomplete final verification fixture.")
            write_final_plan(project, task_status=" ")

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "fail")
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())
            self.assertTrue(any(blocker["check"] == "no_active_pending_partial_or_blocked_tasks" for blocker in result["blockers"]))
            self.assertEqual(result["read_model_rebuild"], {})
            self.assertTrue(
                any(check["check"] == "read_models_rebuild_deferred" and check["status"] == "pass" for check in result["checks"])
            )
            self.assertFalse(any(check["check"] == "read_models_fresh_or_rebuildable" for check in result["checks"]))
            report = json.loads((project / ".loopplane" / "runtime" / "final_verification_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")

    def test_stale_marker_fixture_archives_marker_and_writes_fresh_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Stale marker final verification fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            stale_marker = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            stale_marker.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_stale",
                        "status": "completed",
                        "plan_sha256": "sha256:not-current",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            expected_digest = sha256(stale_marker.read_bytes()).hexdigest()

            result = run_final_verifier(project, owner="test")

            self.assertEqual(result["status"], "pass")
            archived = sorted((project / ".loopplane" / "runtime" / "stale_completion_markers").glob("*.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].name, f"plan_loop_complete.stale.sha256-{expected_digest}.json")
            self.assertEqual(json.loads(archived[0].read_text(encoding="utf-8"))["workflow_id"], "wf_stale")
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(completion_marker_status(paths)["fresh"])

    def test_scheduler_tick_runs_final_verifier_without_staling_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Scheduler final verifier fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace("- [ ] `FO1`", "- [x] `FO1`", 1),
                encoding="utf-8",
            )

            result = run_scheduler(project, max_ticks=10)

            self.assertEqual(result["selected_action"]["action"], "run_final_verification")
            self.assertEqual(result["ticks_run"], 1)
            self.assertEqual(len(result["action_history"]), 1)
            self.assertEqual(result["action_history"][0]["action"], "run_final_verification")
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(completion_marker_status(paths)["fresh"])

            completion_result = run_scheduler(project, max_ticks=1)

            self.assertEqual(completion_result["selected_action"]["action"], "complete")
            self.assertTrue(completion_marker_status(paths)["fresh"])

    def test_scheduler_can_continue_from_final_verification_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Scheduler final completion fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace("- [ ] `FO1`", "- [x] `FO1`", 1),
                encoding="utf-8",
            )

            result = run_scheduler(project, max_ticks=10, continue_after_final_verification=True)

            self.assertEqual(result["selected_action"]["action"], "complete")
            self.assertEqual(result["ticks_run"], 2)
            self.assertEqual(
                [entry["action"] for entry in result["action_history"]],
                ["run_final_verification", "complete"],
            )
            paths = load_scheduler_context(project)["context"].paths
            self.assertTrue(completion_marker_status(paths)["fresh"])

    def test_cli_until_complete_runs_completion_after_final_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "CLI until-complete final fixture.")
            write_final_plan(project)
            write_completed_task_evidence(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace("- [ ] `FO1`", "- [x] `FO1`", 1),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "run",
                    "--project",
                    str(project),
                    "--until-complete",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["selected_action"]["action"], "complete")
            self.assertEqual(
                [entry["action"] for entry in payload["action_history"]],
                ["run_final_verification", "complete"],
            )

    def test_cli_final_verify_returns_nonzero_for_incomplete_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "CLI incomplete final verification fixture.")
            write_final_plan(project, task_status=" ")

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "final-verify", "--project", str(project), "--json"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "fail")


if __name__ == "__main__":
    unittest.main()
