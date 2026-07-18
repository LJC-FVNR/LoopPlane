from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.validation import run_validator


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def set_runner_enabled(project: Path, runner_id: str, enabled: bool) -> None:
    paths = WorkflowPaths.from_config(project, load_workflow_config(project))
    config_path = paths.config_file("agent_runners.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    runners = config.setdefault("runners", {})
    runner = runners.setdefault(runner_id, {})
    if isinstance(runner, dict):
        runner["enabled"] = enabled
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def disable_default_validator_agent(project: Path) -> None:
    set_runner_enabled(project, "validator", False)


def set_validator_agent_mode(project: Path, mode: str) -> None:
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow.setdefault("validation", {})["validator_agent_mode"] = mode
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_validator_agent_for_high_risk(project: Path) -> None:
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow.setdefault("validation", {})["validator_agent_for_high_risk"] = True
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_plan(
    project: Path,
    *,
    validation: str = "file_exists: artifacts/result.txt",
    risk: str = "low",
) -> None:
    disable_default_validator_agent(project)
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- workflow_title: Validation Fixture Workflow
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Validation Fixture

- [ ] T001: Produce result artifact
  - acceptance: Result artifact exists.
  - acceptance: Worker report records the completed command.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: {risk}
  - validation: {validation}
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def write_worker_run(
    project: Path,
    *,
    task_id: str = "T001",
    run_id: str = "run_fixture",
    worker_status: str = "completed",
    create_artifact: bool = False,
    command_exit_code: int = 0,
    commands_run: list[dict[str, object]] | None = None,
    report_text: str = "# Worker Report\n\nWorker claims completion.\n",
) -> Path:
    run_dir = project / ".loopplane" / "results" / task_id / "runs" / run_id
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    (run_dir / "raw").mkdir()
    if create_artifact:
        (run_dir / "artifacts" / "result.txt").write_text("result\n", encoding="utf-8")
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")
    (run_dir / "commands.sh").write_text("python build_result.py\n", encoding="utf-8")
    (run_dir / "logs" / "stdout.log").write_text("ok\n", encoding="utf-8")
    (run_dir / "git").mkdir()
    (run_dir / "git" / "project_diff.patch").write_text("diff --git a/source b/source\n", encoding="utf-8")
    status = {
        "schema_version": "1.5",
        "run_id": run_id,
        "task_id": task_id,
        "primary_task_id": task_id,
        "phase": "Phase P0: Validation Fixture",
        "status": worker_status,
        "next_prompt_ready": True,
        "project_changes": [],
        "commands_run": commands_run if commands_run is not None else [{"cmd": "python build_result.py", "exit_code": command_exit_code}],
        "key_outputs": [".loopplane/results/T001/runs/run_fixture/artifacts/result.txt"],
        "evidence_satisfies": [
            {
                "task_id": task_id,
                "relationship": "primary",
                "acceptance_claimed": ["Result artifact exists."],
                "evidence": [".loopplane/results/T001/runs/run_fixture/artifacts/result.txt"],
            }
        ],
        "validation_claim": {
            "claim": "completed",
            "checks_claimed": [{"name": "self_claim", "status": "pass"}],
            "limitations": [],
        },
        "summary_candidate": {"one_line": "Worker says it is complete.", "highlights": [], "warnings": [], "blockers": []},
        "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
        "repair_attempts": [],
        "known_risks": [],
        "remaining_incomplete_items": [task_id] if worker_status.startswith("blocked") else [],
    }
    (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_dir


def configure_fake_validator_agent(project: Path, *, status: str = "accepted") -> None:
    script = project / ".loopplane" / "config" / "fake_validator_agent.py"
    script.write_text(
        r'''
import json
import os
import pathlib


review_path = pathlib.Path(os.environ["LOOPPLANE_VALIDATOR_REVIEW_PATH"])
review_path.parent.mkdir(parents=True, exist_ok=True)
review_path.write_text(
    json.dumps(
        {
            "schema_version": "1.0",
            "workflow_id": os.environ.get("LOOPPLANE_WORKFLOW_ID"),
            "run_id": os.environ.get("LOOPPLANE_RUN_ID"),
            "task_id": os.environ.get("LOOPPLANE_TASK_ID"),
            "status": "__STATUS__",
            "confidence": "high",
            "rationale": "The validator agent judged the semantic evidence directly.",
            "evidence_reviewed": ["report.md"],
            "material_gaps": [],
            "recommended_action": "accept",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake validator agent wrote validator_review.json")
'''.replace("__STATUS__", status),
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["validator"].update(
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


def configure_broken_validator_agent(project: Path) -> None:
    script = project / ".loopplane" / "config" / "broken_validator_agent.py"
    script.write_text(
        "print('validator agent exited without writing validator_review.json')\n",
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["validator"].update(
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


def write_absorption_plan(project: Path, tasks: list[dict[str, object]]) -> None:
    disable_default_validator_agent(project)
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    lines = [
        "# Project Plan",
        "",
        "## Metadata",
        "",
        f"- workflow_id: {workflow['workflow_id']}",
        "- plan_version: 1",
        "- generated_from: PROJECT_BRIEF.md",
        "- active: true",
        "",
    ]
    current_phase = None
    for task in tasks:
        phase = str(task.get("phase") or "Phase P0: Absorption Fixture")
        if phase != current_phase:
            lines.extend([f"## {phase}", ""])
            current_phase = phase
        task_id = str(task["task_id"])
        status = str(task.get("status") or " ")
        title = str(task.get("title") or f"{task_id} task")
        depends_on = task.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        depends_text = "[" + ", ".join(str(item) for item in depends_on) + "]"
        risk = str(task.get("risk") or "low")
        approval = str(task.get("approval") or "not_required")
        validation = str(task.get("validation") or f"file_exists: artifacts/{absorption_artifact(task_id)}")
        lines.extend(
            [
                f"- [{status}] {task_id}: {title}",
                f"  - acceptance: {task_id} artifact exists.",
                f"  - evidence: .loopplane/results/{task_id}/",
                f"  - latest: .loopplane/results/{task_id}/latest.json",
                f"  - depends_on: {depends_text}",
                f"  - risk: {risk}",
                f"  - validation: {validation}",
                "  - max_attempts: 3",
                f"  - approval: {approval}",
                f"  - deliverables: artifacts/{absorption_artifact(task_id)}.",
                "",
            ]
        )
    (project / "PLAN.md").write_text("\n".join(lines), encoding="utf-8")


def write_absorption_worker_run(
    project: Path,
    *,
    candidate_ids: list[str],
    artifact_task_ids: set[str],
    run_id: str = "run_absorption",
) -> Path:
    run_dir = project / ".loopplane" / "results" / "T001" / "runs" / run_id
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    (run_dir / "raw").mkdir()
    for task_id in artifact_task_ids:
        (run_dir / "artifacts" / absorption_artifact(task_id)).write_text(f"{task_id}\n", encoding="utf-8")
    (run_dir / "report.md").write_text("# Absorption Worker Report\n\nEvidence recorded.\n", encoding="utf-8")
    (run_dir / "commands.sh").write_text("python absorb.py\n", encoding="utf-8")
    claims = [
        {
            "task_id": "T001",
            "relationship": "primary",
            "acceptance_claimed": ["T001 artifact exists."],
            "evidence": [f"artifacts/{absorption_artifact('T001')}"],
        }
    ]
    for candidate_id in candidate_ids:
        claims.append(
            {
                "task_id": candidate_id,
                "relationship": "candidate",
                "acceptance_claimed": [f"{candidate_id} artifact exists."],
                "evidence": [f"artifacts/{absorption_artifact(candidate_id)}"],
            }
        )
    status = {
        "schema_version": "1.5",
        "run_id": run_id,
        "task_id": "T001",
        "primary_task_id": "T001",
        "phase": "Phase P0: Absorption Fixture",
        "status": "completed",
        "next_prompt_ready": True,
        "project_changes": [],
        "commands_run": [{"cmd": "python absorb.py", "exit_code": 0}],
        "key_outputs": [f".loopplane/results/T001/runs/{run_id}/report.md"],
        "evidence_satisfies": claims,
        "validation_claim": {"claim": "completed", "checks_claimed": [{"name": "self_claim", "status": "pass"}], "limitations": []},
        "summary_candidate": {"one_line": "Absorption evidence recorded.", "highlights": [], "warnings": [], "blockers": []},
        "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
        "repair_attempts": [],
        "known_risks": [],
        "remaining_incomplete_items": [],
    }
    (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_dir


def absorption_artifact(task_id: str) -> str:
    return task_id.lower().replace(".", "_") + ".txt"


class AuthoritativeValidatorTest(unittest.TestCase):
    def test_evidence_backed_worker_output_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate passing evidence.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: completed command")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                report_text="# Worker Report\n\nThe completed command wrote the result artifact.\n",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["verdict"], "accepted")
            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], [])
            result = validation["task_results"][0]
            self.assertEqual({check["name"] for check in result["checks"]}, {"file_exists", "command_exit_code", "report_contains"})

    def test_enabled_validator_agent_can_semantically_accept_over_narrow_deterministic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Agentic validation override.")
            write_plan(project, validation="file_exists: artifacts/result.txt")
            configure_fake_validator_agent(project, status="accepted")
            run_dir = write_worker_run(
                project,
                create_artifact=False,
                report_text="# Worker Report\n\nThe result was delivered inline for review.\n",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(validation["validator"], "validator_agent")
            self.assertEqual(validation["deterministic_validation_status"], "fail")
            self.assertEqual(validation["validator_agent"]["review"]["status"], "accepted")
            self.assertTrue((run_dir / "validation.json").is_file())

    def test_default_validator_agent_skips_when_deterministic_validation_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validator fallback fixture.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            configure_broken_validator_agent(project)
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(validation["validator"], "deterministic_validation_evidence_collector")
            self.assertEqual(validation["validator_agent_policy"]["mode"], "on_deterministic_failure")
            self.assertFalse(validation["validator_agent_policy"]["run"])
            self.assertEqual(validation["validator_agent_policy"]["reason"], "deterministic_validation_passed")
            self.assertNotIn("validator_agent", validation)
            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], [])

    def test_validator_agent_always_mode_fails_closed_when_agent_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validator fallback fixture.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            set_validator_agent_mode(project, "always")
            configure_broken_validator_agent(project)
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "blocked", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(validation["deterministic_validation_status"], "pass")
            self.assertEqual(validation["validator_agent_policy"]["mode"], "always")
            self.assertTrue(validation["validator_agent_policy"]["required"])
            self.assertEqual(len(validation["validator_agent"]["attempts"]), 2)
            self.assertEqual(validation["accepted_task_ids"], [])
            self.assertEqual(validation["rejected_task_ids"], ["T001"])
            self.assertTrue(
                any("Required validator agent failed" in failure for failure in validation["failures"]),
                json.dumps(validation, indent=2, sort_keys=True),
            )

    def test_required_high_risk_validator_agent_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Required high-risk validator fixture.")
            write_plan(
                project,
                validation="file_exists: artifacts/result.txt; command_exit_code: 0",
                risk="high",
            )
            require_validator_agent_for_high_risk(project)
            configure_broken_validator_agent(project)
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "blocked", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(validation["deterministic_validation_status"], "pass")
            self.assertTrue(validation["validator_agent_policy"]["required"])
            self.assertEqual(validation["validator_agent_policy"]["reason"], "high_risk_task")
            self.assertEqual(validation["accepted_task_ids"], [])
            self.assertEqual(validation["rejected_task_ids"], ["T001"])
            self.assertTrue(
                any("Required validator agent failed" in failure for failure in validation["failures"]),
                json.dumps(validation, indent=2, sort_keys=True),
            )

    def test_file_exists_accepts_comma_separated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate multiple evidence files.")
            (project / "README.md").write_text("# Fixture\n", encoding="utf-8")
            write_plan(project, validation="file_exists: artifacts/result.txt, README.md")
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            checks = [check for check in validation["task_results"][0]["checks"] if check["name"] == "file_exists"]
            self.assertEqual(len(checks), 2)
            self.assertTrue(all(check["status"] == "pass" for check in checks))

    def test_file_exists_accepts_run_root_task_specific_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate task-specific run-root evidence.")
            write_plan(project, validation="file_exists: custom_audit.json")
            run_dir = write_worker_run(project, create_artifact=True)
            (run_dir / "custom_audit.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "file_exists")
            self.assertEqual(check["status"], "pass")

    def test_command_exit_code_named_command_ignores_unrelated_negative_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate named command only.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: python build_result.py")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python build_result.py", "exit_code": 0},
                    {"cmd": "python app.py invalid-input", "exit_code": 2},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_exit_code")
            self.assertIn("python build_result.py", command_check["message"])

    def test_command_exit_code_can_expect_nonzero_for_named_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate expected non-zero command.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: python app.py invalid-input == 2")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python build_result.py", "exit_code": 0},
                    {"cmd": "python app.py invalid-input", "exit_code": 2},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))

    def test_command_exit_code_uses_actual_exit_code_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate worker actual_exit_code field.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: python app.py invalid-input == 3")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {
                        "command": "python app.py invalid-input",
                        "expected_exit_code": 3,
                        "actual_exit_code": 3,
                    }
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))

    def test_command_exit_code_uses_loopplane_raw_exit_when_worker_exit_code_is_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate raw exit capture fallback.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: python app.py invalid-input == 3")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {
                        "command": "python app.py invalid-input",
                        "exit_code": None,
                        "stdout_path": "raw/negative.stdout",
                        "stderr_path": "raw/negative.stderr",
                    }
                ],
            )
            (run_dir / "raw" / "negative.command").write_text("python app.py invalid-input\n", encoding="utf-8")
            (run_dir / "raw" / "negative.exit").write_text("3\n", encoding="utf-8")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_exit_code")
            self.assertEqual(command_check["status"], "pass")

    def test_command_stdout_contains_uses_recorded_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate recorded stdout.")
            write_plan(
                project,
                validation='file_exists: artifacts/result.txt; command_stdout_contains: python app.py ok contains "READY:42"',
            )
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python app.py ok", "exit_code": 0, "stdout": "READY:42\n"},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            stdout_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_stdout_contains")
            self.assertEqual(stdout_check["status"], "pass")

    def test_command_stdout_contains_accepts_stdout_value_that_points_to_run_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate stdout path-like value.")
            write_plan(
                project,
                validation='file_exists: artifacts/result.txt; command_stdout_contains: python app.py ok contains "READY:42"',
            )
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python app.py ok", "exit_code": 0, "stdout": "logs/final_text.stdout"},
                ],
            )
            (run_dir / "logs" / "final_text.stdout").write_text("READY:42\n", encoding="utf-8")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            stdout_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_stdout_contains")
            self.assertEqual(stdout_check["status"], "pass")

    def test_strict_command_stdout_contains_mismatch_rejects_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Strictly validate recorded stdout.")
            write_plan(
                project,
                validation='file_exists: artifacts/result.txt; strict_command_stdout_contains: python app.py ok contains "READY:42"',
            )
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python app.py ok", "exit_code": 0, "stdout": "NOT_READY\n"},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail", json.dumps(validation, indent=2, sort_keys=True))
            stdout_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "strict_command_stdout_contains")
            self.assertEqual(stdout_check["status"], "fail")

    def test_command_matching_can_target_recorded_command_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate command id targeting.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: smoke == 0")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"command_id": "smoke", "cmd": "python app.py --smoke", "exit_code": 0},
                    {"command_id": "negative", "cmd": "python app.py --smoke-negative", "exit_code": 2},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_exit_code")
            self.assertEqual(command_check["status"], "pass")

    def test_strict_command_exit_code_reports_ambiguous_command_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate ambiguous command matching.")
            write_plan(project, validation="file_exists: artifacts/result.txt; strict_command_exit_code: python app.py == 0")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python app.py --unit", "exit_code": 0},
                    {"cmd": "python app.py --integration", "exit_code": 0},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail", json.dumps(validation, indent=2, sort_keys=True))
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "strict_command_exit_code")
            self.assertEqual(command_check["status"], "fail")
            self.assertIn("unique matching recorded command", command_check["message"])

    def test_recorded_zero_test_discovery_rejects_worker_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject zero-test discovery.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: pytest == 0")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "pytest", "exit_code": 0, "stdout": "collected 0 items\n\nno tests ran in 0.01s\n"},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail", json.dumps(validation, indent=2, sort_keys=True))
            zero_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "test_discovery_nonempty")
            self.assertEqual(zero_check["status"], "fail")

    def test_command_matching_normalizes_python_c_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate shell quoting normalization.")
            write_plan(project, validation='file_exists: artifacts/result.txt; command_exit_code: python -c "import csvguard"')
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[
                    {"cmd": "python -c import csvguard", "exit_code": 0},
                ],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_exit_code")
            self.assertEqual(command_check["status"], "pass")

    def test_command_stderr_contains_can_use_acceptance_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate acceptance result stderr.")
            write_plan(
                project,
                validation='file_exists: artifacts/result.txt; command_stderr_contains: python app.py bad contains "expected failure"',
            )
            run_dir = write_worker_run(project, create_artifact=True, commands_run=[])
            (run_dir / "acceptance_results.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "command": "python app.py bad",
                                "exit_code": 4,
                                "stderr": "expected failure\n",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            stderr_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_stderr_contains")
            self.assertEqual(stderr_check["status"], "pass")

    def test_file_exists_accepts_run_root_acceptance_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate run-root acceptance results evidence.")
            write_plan(project, validation="file_exists: artifacts/result.txt; file_exists: acceptance_results.json")
            run_dir = write_worker_run(project, create_artifact=True)
            (run_dir / "acceptance_results.json").write_text(json.dumps({"results": []}) + "\n", encoding="utf-8")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            file_checks = [check for check in validation["task_results"][0]["checks"] if check["name"] == "file_exists"]
            self.assertTrue(all(check["status"] == "pass" for check in file_checks))

    def test_command_exit_code_accepts_nonzero_expectation_tokens(self) -> None:
        for clause in (
            "command_exit_code: python app.py invalid-input returns nonzero",
            "command_exit_code: python app.py invalid-input returns non-zero",
            "command_exit_code: python app.py invalid-input != 0",
            "command_exit_code: python app.py invalid-input fails",
        ):
            with self.subTest(clause=clause):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    init_project(project, "Validate expected non-zero tokens.")
                    write_plan(project, validation=f"file_exists: artifacts/result.txt; {clause}")
                    run_dir = write_worker_run(
                        project,
                        create_artifact=True,
                        commands_run=[
                            {"cmd": "python build_result.py", "exit_code": 0},
                            {"cmd": "python app.py invalid-input", "exit_code": 2},
                        ],
                    )

                    validation = run_validator(project, task_id="T001", run_dir=run_dir)

                    self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))

    def test_command_exit_code_unparseable_expectation_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Warn on unsupported exit-code expectation.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: python app.py invalid-input returns banana")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[{"cmd": "python app.py invalid-input", "exit_code": 2}],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass_with_warnings", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(validation["verdict"], "accepted_with_warnings")
            command_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "command_exit_code")
            self.assertEqual(command_check["status"], "pass_with_warnings")
            self.assertIn("Unsupported command_exit_code expectation 'banana'", command_check["message"])
            self.assertIn("Treated as advisory", command_check["message"])

    def test_report_contains_mismatch_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Warn on report text mismatch.")
            write_plan(project, validation="file_exists: artifacts/result.txt; report_contains: exact marker")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                report_text="# Worker Report\n\nAgent completed the requested work.\n",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass_with_warnings", json.dumps(validation, indent=2, sort_keys=True))
            report_check = next(check for check in validation["task_results"][0]["checks"] if check["name"] == "report_contains")
            self.assertEqual(report_check["status"], "pass_with_warnings")
            self.assertIn("Advisory report_contains did not match", report_check["message"])

    def test_strategy_splitter_preserves_semicolon_and_colon_inside_quoted_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate quoted command clauses.")
            command = "python -c \"import sys; print('x:y')\""
            write_plan(project, validation=f"file_exists: artifacts/result.txt; command_exit_code: {command}; report_contains: Worker claims")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                commands_run=[{"cmd": command, "exit_code": 0}],
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            self.assertEqual(
                [check["name"] for check in validation["task_results"][0]["checks"]],
                ["file_exists", "command_exit_code", "report_contains"],
            )

    def test_protected_path_changes_are_rejected_even_with_passing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject protected path edits.")
            write_plan(project, validation="file_exists: artifacts/result.txt")
            run_dir = write_worker_run(project, create_artifact=True)
            (run_dir / "git" / "changed_files.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "changed_files": [
                            {"path": "src/app.py", "status": "modified"},
                            {"path": "PLAN.md", "status": "modified"},
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail")
            result = validation["task_results"][0]
            self.assertIn("protected_path_changes", {check["name"] for check in result["checks"]})
            self.assertIn("PLAN.md", "\n".join(result["failures"]))
            self.assertEqual(validation["accepted_task_ids"], [])
            self.assertEqual(validation["rejected_task_ids"], ["T001"])

    def test_worker_self_claim_without_matching_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate worker evidence.")
            write_plan(project)
            run_dir = write_worker_run(project)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail")
            self.assertEqual(validation["verdict"], "rejected")
            result = validation["task_results"][0]
            self.assertEqual(result["task_id"], "T001")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["verdict"], "rejected")
            self.assertIn("Result artifact exists.", result["acceptance_criteria_covered"])
            self.assertIn("worker self-claim references missing advisory evidence", "\n".join(result["warnings"]))
            self.assertTrue(any("artifacts/result.txt" in failure for failure in result["failures"]))
            written = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual(written["verdict"], "rejected")

    def test_blocked_worker_status_writes_blocked_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate blocked evidence.")
            write_plan(project)
            run_dir = write_worker_run(project, worker_status="blocked_external")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "blocked")
            self.assertEqual(validation["verdict"], "rejected")
            self.assertIn("blocked_external", validation["failures"][0])

    def test_human_approval_strategy_is_auto_authorized_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate human approval.")
            write_plan(project, validation="human_approval: release manager approval required")
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass_with_warnings")
            self.assertEqual(validation["verdict"], "accepted_with_warnings")
            self.assertIn("auto-authorized", validation["warnings"][0])

    def test_cli_validate_writes_authoritative_validation_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate through CLI.")
            write_plan(project, validation="file_exists: artifacts/result.txt")
            run_dir = write_worker_run(project, create_artifact=True)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "validate",
                    "--project",
                    str(project),
                    "--task",
                    "T001",
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "pass")
            self.assertTrue((run_dir / "validation.json").is_file())
            self.assertTrue((run_dir / "validator.log").is_file())

    def test_adjacent_same_phase_candidate_is_independently_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validate absorption.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T001", "T002"})
            plan_before = (project / "PLAN.md").read_text(encoding="utf-8")

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["accepted_task_ids"], ["T001", "T002"])
            self.assertEqual(validation["rejected_task_ids"], [])
            self.assertEqual(validation["multi_task_absorption"]["accepted_task_ids"], ["T002"])
            candidate = validation["task_results"][1]
            self.assertEqual(candidate["task_id"], "T002")
            self.assertEqual(candidate["relationship"], "candidate")
            self.assertEqual(candidate["status"], "pass")
            self.assertEqual({check["name"] for check in candidate["checks"]}, {"file_exists"})
            self.assertEqual((project / "PLAN.md").read_text(encoding="utf-8"), plan_before)

    def test_candidate_claim_without_independent_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject unsupported absorption.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T001"})

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], ["T002"])
            candidate = validation["task_results"][1]
            self.assertEqual(candidate["status"], "fail")
            self.assertTrue(any("artifacts/t002.txt" in failure for failure in candidate["failures"]))
            self.assertEqual(validation["multi_task_absorption"]["rejected_task_ids"], ["T002"])

    def test_non_adjacent_and_cross_phase_candidates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject out-of-scope absorption.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002"},
                    {"task_id": "T003", "depends_on": ["T001"]},
                    {"task_id": "X001", "phase": "Phase P1: Other Phase", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(
                project,
                candidate_ids=["T003", "X001"],
                artifact_task_ids={"T001", "T003", "X001"},
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], ["T003", "X001"])
            by_task = {result["task_id"]: result for result in validation["task_results"]}
            self.assertTrue(any("not adjacent" in failure for failure in by_task["T003"]["failures"]))
            self.assertTrue(any("not primary phase" in failure for failure in by_task["X001"]["failures"]))

    def test_dependency_incompatible_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject dependency-incompatible absorption.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T999"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T001", "T002"})

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            candidate = validation["task_results"][1]
            self.assertEqual(candidate["status"], "fail")
            self.assertTrue(any("T999" in failure for failure in candidate["failures"]))

    def test_blocked_skipped_high_risk_and_approval_gated_candidates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject policy-disallowed absorption.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "status": "!", "depends_on": ["T001"]},
                    {"task_id": "T003", "status": "-", "depends_on": ["T001"]},
                    {"task_id": "T004", "depends_on": ["T001"], "risk": "high", "approval": "required"},
                ],
            )
            run_dir = write_absorption_worker_run(
                project,
                candidate_ids=["T002", "T003", "T004"],
                artifact_task_ids={"T001", "T002", "T003", "T004"},
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], ["T002", "T003", "T004"])
            by_task = {result["task_id"]: result for result in validation["task_results"]}
            self.assertTrue(any("blocked" in failure for failure in by_task["T002"]["failures"]))
            self.assertTrue(any("skipped" in failure for failure in by_task["T003"]["failures"]))
            self.assertTrue(any("High-risk" in failure for failure in by_task["T004"]["failures"]))
            self.assertTrue(any("human approval" in failure for failure in by_task["T004"]["failures"]))

    def test_candidate_is_rejected_when_primary_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reject absorption when primary fails.")
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T002"})

            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail")
            self.assertEqual(validation["accepted_task_ids"], [])
            self.assertEqual(validation["rejected_task_ids"], ["T001", "T002"])
            candidate = validation["task_results"][1]
            self.assertTrue(any("Primary task was not accepted" in failure for failure in candidate["failures"]))


if __name__ == "__main__":
    unittest.main()
