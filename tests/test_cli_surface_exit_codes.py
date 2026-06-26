from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.exit_codes import (
    EXIT_DUPLICATE_SCHEDULER,
    EXIT_FAILURE_BUDGET_EXHAUSTED,
    EXIT_FINAL_VERIFICATION_FAILED,
    EXIT_HEALTH_FAILURE,
    EXIT_INVALID_CONFIG,
    EXIT_MIGRATION_REQUIRED,
    EXIT_PLAN_MALFORMED,
    EXIT_RUNNER_UNAVAILABLE,
    EXIT_SECURITY_POLICY_VIOLATION,
    EXIT_SUCCESS,
    EXIT_VALIDATION_FAILED,
    EXIT_VERSION_CONTROL_UNAVAILABLE,
    EXIT_WAITING_APPROVAL,
    EXIT_WAITING_BACKGROUND_JOB,
)
from runtime.init_workflow import init_project
from runtime.scheduler import AtomicOwnerLock


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def run_loopplane(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def write_active_plan(
    project: Path,
    *,
    task_status: str = " ",
    approval: str = "not_required",
    validation: str = "CLI fixture validation.",
) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: CLI Exit Fixture

- [{task_status}] T001: Exercise CLI exit code
  - acceptance: The CLI exit-code fixture is handled.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: {validation}
  - max_attempts: 1
  - approval: {approval}
  - deliverables: CLI fixture output.
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class CliSurfaceHelpTest(unittest.TestCase):
    def test_mvp_command_help_surface_is_exposed(self) -> None:
        top = run_loopplane("--help")
        self.assertEqual(top.returncode, EXIT_SUCCESS, top.stderr + top.stdout)
        for token in (
            "skill",
            "configure-agent",
            "doctor-agent",
            "init",
            "write-brief",
            "plan",
            "audit-plan",
            "activate-plan",
            "start",
            "run",
            "preview",
            "tick",
            "pause",
            "resume",
            "stop",
            "attach",
            "status",
            "health",
            "logs",
            "background",
            "summarize",
            "rebuild-read-models",
            "migrate",
            "export",
            "dashboard",
            "workspace",
            "workflow",
            "template",
            "ask",
            "change-request",
            "approvals",
            "approve",
            "reject",
            "vc",
        ):
            self.assertIn(token, top.stdout)

        command_help = [
            ("skill", "doctor"),
            ("skill", "install"),
            ("skill", "update"),
            ("skill", "pack"),
            ("configure-agent",),
            ("doctor-agent",),
            ("init",),
            ("write-brief",),
            ("plan",),
            ("audit-plan",),
            ("activate-plan",),
            ("start",),
            ("run",),
            ("preview",),
            ("tick",),
            ("pause",),
            ("resume",),
            ("stop",),
            ("attach",),
            ("status",),
            ("health",),
            ("summarize",),
            ("logs",),
            ("background",),
            ("background", "start"),
            ("background", "status"),
            ("background", "complete"),
            ("background", "cancel"),
            ("rebuild-read-models",),
            ("migrate",),
            ("export",),
            ("dashboard",),
            ("workspace",),
            ("workspace", "current"),
            ("workspace", "register"),
            ("workspace", "unregister"),
            ("workspace", "scan"),
            ("workspace", "list"),
            ("workspace", "doctor"),
            ("workflow",),
            ("workflow", "list"),
            ("workflow", "current"),
            ("workflow", "show"),
            ("workflow", "switch"),
            ("workflow", "create"),
            ("workflow", "archive"),
            ("workflow", "restore"),
            ("workflow", "fork"),
            ("template",),
            ("template", "list"),
            ("template", "show"),
            ("template", "doctor"),
            ("template", "render"),
            ("template", "instance", "show"),
            ("template", "extract-preset"),
            ("ask",),
            ("change-request",),
            ("change-request", "submit"),
            ("approvals",),
            ("approve",),
            ("reject",),
            ("vc", "status"),
            ("vc", "checkpoint"),
            ("vc", "diff"),
            ("vc", "log"),
            ("vc", "rollback"),
            ("vc", "doctor"),
        ]
        for command in command_help:
            with self.subTest(command=" ".join(command)):
                result = run_loopplane(*command, "--help")
                self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
                self.assertIn("usage: loopplane", result.stdout)


class CliExitCodeContractTest(unittest.TestCase):
    def test_invalid_config_returns_2(self) -> None:
        result = run_loopplane("init")

        self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
        self.assertIn("--brief is required", result.stdout)

    def test_malformed_plan_returns_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed plan activation fixture.")
            (project / ".loopplane" / "planning" / "PLAN_DRAFT.md").write_text(
                "# Draft\n\n- [ ] T001: Missing required fields\n",
                encoding="utf-8",
            )

            result = run_loopplane("activate-plan", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_PLAN_MALFORMED, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])

    def test_validation_failure_returns_4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validation failure fixture.")
            write_active_plan(project)
            run_dir = project / ".loopplane" / "results" / "T001" / "runs" / "run_empty"
            run_dir.mkdir(parents=True)

            result = run_loopplane(
                "validate",
                "--project",
                str(project),
                "--task",
                "T001",
                "--run-dir",
                str(run_dir),
                "--json",
                "--no-write",
            )

            self.assertEqual(result.returncode, EXIT_VALIDATION_FAILED, result.stderr + result.stdout)
            self.assertIn(json.loads(result.stdout)["status"], {"fail", "blocked"})

    def test_validation_human_approval_is_auto_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Validation auto approval fixture.")
            write_active_plan(project, validation="human_approval: release manager approval required")
            run_dir = project / ".loopplane" / "results" / "T001" / "runs" / "run_human"
            run_dir.mkdir(parents=True)
            write_json(
                run_dir / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_human",
                    "task_id": "T001",
                    "primary_task_id": "T001",
                    "status": "completed",
                    "validation_claim": {"claim": "completed", "checks_claimed": [], "limitations": []},
                    "evidence_satisfies": [],
                    "commands_run": [{"cmd": "fixture", "exit_code": 0}],
                    "key_outputs": [],
                    "project_changes": [],
                    "remaining_incomplete_items": [],
                },
            )

            result = run_loopplane(
                "validate",
                "--project",
                str(project),
                "--task",
                "T001",
                "--run-dir",
                str(run_dir),
                "--json",
                "--no-write",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "pass_with_warnings")
            self.assertEqual(payload["verdict"], "accepted_with_warnings")

    def test_waiting_approval_returns_6(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Waiting approval fixture.")
            security_path = project / ".loopplane" / "config" / "security.json"
            security = json.loads(security_path.read_text(encoding="utf-8"))
            security["approval"]["enabled"] = True
            security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            write_active_plan(project, approval="required")

            result = run_loopplane("run", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_WAITING_APPROVAL, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["selected_action"]["action"], "wait_approval")

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_vc_rollback_executes_without_waiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Rollback waiting approval fixture.")
            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                configured = subprocess.run(
                    ["git", "-C", str(project), "config", key, value],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(configured.returncode, EXIT_SUCCESS, configured.stderr + configured.stdout)
            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            committed = subprocess.run(
                ["git", "-C", str(project), "add", "."],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(committed.returncode, EXIT_SUCCESS, committed.stderr + committed.stdout)
            committed = subprocess.run(
                ["git", "-C", str(project), "commit", "-m", "initial"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(committed.returncode, EXIT_SUCCESS, committed.stderr + committed.stdout)
            checkpoint = run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--json",
            )
            self.assertEqual(checkpoint.returncode, EXIT_SUCCESS, checkpoint.stderr + checkpoint.stdout)
            checkpoint_id = json.loads(checkpoint.stdout)["checkpoint"]["checkpoint_id"]

            result = run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", checkpoint_id, "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "executed")
            self.assertFalse(payload["approval_required"])

    def test_waiting_background_job_returns_7(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Waiting background fixture.")
            write_active_plan(project)
            write_json(
                project / ".loopplane" / "runtime" / "background_jobs.json",
                [{"job_id": "job1", "status": "running", "next_prompt_ready": False}],
            )

            result = run_loopplane("run", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_WAITING_BACKGROUND_JOB, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["selected_action"]["action"], "wait_background_job")

    def test_agent_runner_unavailable_returns_8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Runner unavailable fixture.")
            configured = run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                "missing-codex-command-for-loopplane",
                "--json",
            )
            self.assertEqual(configured.returncode, EXIT_SUCCESS, configured.stderr + configured.stdout)

            result = run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json")

            self.assertEqual(result.returncode, EXIT_RUNNER_UNAVAILABLE, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "waiting_config")

    def test_migration_required_returns_9(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Migration required fixture.")
            write_active_plan(project)
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["configuration_problems"] = [
                {"code": "schema_migration_required", "message": "Schema migration is required."}
            ]
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_loopplane("preview", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_MIGRATION_REQUIRED, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["next_action"], "wait_config")

    def test_security_policy_violation_returns_10(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Legacy command policy fixture.")
            write_active_plan(project)
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            runner = config["runners"]["worker"]
            runner["adapter"] = "shell"
            runner["command"] = "git"
            runner["args"] = ["tag", "v1"]
            runner["prompt_delivery"] = {"mode": "stdin"}
            runner["doctor"] = {"check_command": "git --version", "requires_auth": False}
            runner["permission_policy"]["require_approval_for_risky_commands"] = True
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_loopplane("run", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SECURITY_POLICY_VIOLATION, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["selected_action"]["action"], "run_worker")
            self.assertEqual(payload["selected_action"]["execution_result"]["adapter_exit_code"], 126)

    def test_duplicate_scheduler_returns_11(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Duplicate scheduler fixture.")
            write_active_plan(project)
            lock = AtomicOwnerLock(project / ".loopplane" / "runtime" / "lock" / "scheduler_instance_lock", "test-owner")
            with lock.acquire():
                result = run_loopplane("run", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_DUPLICATE_SCHEDULER, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["exit_code"], EXIT_DUPLICATE_SCHEDULER)

    def test_final_verification_failure_returns_12(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Final verification failure fixture.")
            write_active_plan(project)

            result = run_loopplane("final-verify", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_FINAL_VERIFICATION_FAILED, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "fail")

    def test_version_control_unavailable_returns_13(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Git unavailable fixture.")
            env = dict(os.environ)
            env["PATH"] = ""

            result = run_loopplane("vc", "doctor", "--project", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_VERSION_CONTROL_UNAVAILABLE, result.stderr + result.stdout)
            self.assertFalse(json.loads(result.stdout)["git"]["available"])

    def test_health_failure_returns_14(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health failure fixture.")
            (project / ".loopplane" / "runtime" / "failure_registry.json").write_text("{not json\n", encoding="utf-8")

            result = run_loopplane("health", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_HEALTH_FAILURE, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "unhealthy")

    def test_failure_budget_exhausted_returns_5_when_scheduler_final_verify_observes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Failure budget exhausted fixture.")
            workflow_path = project / ".loopplane" / "config" / "workflow.json"
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            workflow["self_expansion"]["enabled"] = False
            write_json(workflow_path, workflow)
            write_active_plan(project, task_status="x")
            write_json(
                project / ".loopplane" / "runtime" / "failure_registry.json",
                {
                    "schema_version": "1.5",
                    "failures": [
                        {
                            "failure_id": "fail_exhausted",
                            "task_id": "T001",
                            "status": "exhausted",
                            "recoverable": True,
                            "recovery_attempts": 1,
                            "max_recovery_attempts": 1,
                        }
                    ],
                },
            )

            result = run_loopplane("run", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_FAILURE_BUDGET_EXHAUSTED, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["selected_action"]["action"], "run_final_verification")

    def test_direct_change_request_shorthand_submits_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Direct change request fixture.")
            write_active_plan(project)

            result = run_loopplane("change-request", "Add a final release checklist.", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "pending_review")


if __name__ == "__main__":
    unittest.main()
