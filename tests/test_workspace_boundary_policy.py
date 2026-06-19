from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.reconciliation import run_reconciler
from runtime.scheduler import run_scheduler
from runtime.validation import run_validator
from tests.test_human_summaries import configure_fake_summary_agent
from tests.test_validation import disable_default_validator_agent


def init_monorepo_workspace(tmp: str) -> tuple[Path, Path, Path, WorkflowPaths, dict[str, object]]:
    repo = Path(tmp) / "monorepo"
    service_a = repo / "services" / "service-a"
    service_b = repo / "services" / "service-b"
    service_a.mkdir(parents=True)
    service_b.mkdir(parents=True)
    completed = subprocess.run(["git", "init", "-q", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr + completed.stdout)
    init_project(service_a, "Worker write boundary.")
    workflow = load_workflow_config(service_a)
    paths = WorkflowPaths.from_config(service_a, workflow)
    return repo, service_a, service_b, paths, workflow


def write_plan(paths: WorkflowPaths, workflow: dict[str, object], *, allow_path: str | None = None) -> None:
    disable_default_validator_agent(paths.project_root)
    allow_lines = ""
    if allow_path is not None:
        allow_lines = f"  - allow_out_of_boundary_writes: true\n  - out_of_boundary_write_paths: {allow_path}\n"
    paths.plan_file.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Boundary Policy

- [ ] T001: Produce result artifact
  - acceptance: Result artifact exists.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 1
  - approval: not_required
{allow_lines}  - deliverables: artifacts/result.txt.
""",
        encoding="utf-8",
    )


def accept_plan_hash(paths: WorkflowPaths) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_plan_sha256"] = "sha256:" + sha256(paths.plan_file.read_bytes()).hexdigest()
    state.pop("manual_plan_change", None)
    state["configuration_problems"] = [
        problem
        for problem in state.get("configuration_problems", [])
        if isinstance(problem, dict) and problem.get("code") != "manual_plan_change_detected"
    ]
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def allow_out_of_boundary_path(project: Path, paths: WorkflowPaths, relative_path: str) -> None:
    workspace_path = project / ".loopplane" / "workspace.json"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    workspace["allow_out_of_boundary_writes"] = True
    workspace_path.write_text(json.dumps(workspace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    security_path = paths.config_file("security.json")
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["file_access"]["allow_out_of_boundary_writes"] = True
    security["file_access"]["out_of_boundary_write_allowlist"] = [relative_path]
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_worker_run(paths: WorkflowPaths, project: Path, sibling_file: Path, *, run_id: str = "run_oob") -> Path:
    run_dir = paths.results_dir / "T001" / "runs" / run_id
    for child in ("artifacts", "logs", "raw", "git"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts" / "result.txt").write_text("ok\n", encoding="utf-8")
    sibling_file.write_text("worker wrote outside boundary\n", encoding="utf-8")
    relative_sibling = os.path.relpath(sibling_file, start=project).replace(os.sep, "/")
    (run_dir / "report.md").write_text("# Worker Report\n\nCompleted.\n", encoding="utf-8")
    (run_dir / "commands.sh").write_text("python worker.py\n", encoding="utf-8")
    (run_dir / "git" / "changed_files.json").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "task_id": "T001",
                "run_id": run_id,
                "changed_files": [{"path": relative_sibling, "change_type": "added"}],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    status = {
        "schema_version": "1.5",
        "run_id": run_id,
        "task_id": "T001",
        "primary_task_id": "T001",
        "phase": "Phase P0: Boundary Policy",
        "status": "completed",
        "next_prompt_ready": True,
        "project_changes": [{"path": relative_sibling, "change_type": "added"}],
        "commands_run": [{"cmd": "python worker.py", "exit_code": 0}],
        "key_outputs": ["artifacts/result.txt"],
        "evidence_satisfies": [
            {
                "task_id": "T001",
                "relationship": "primary",
                "acceptance_claimed": ["Result artifact exists."],
                "evidence": ["artifacts/result.txt"],
            }
        ],
        "validation_claim": {"claim": "completed", "checks_claimed": [{"name": "self_claim", "status": "pass"}], "limitations": []},
        "summary_candidate": {"one_line": "Worker completed.", "highlights": [], "warnings": [], "blockers": []},
        "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
        "repair_attempts": [],
        "known_risks": [],
        "remaining_incomplete_items": [],
    }
    (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_dir


def write_forged_passing_validation(run_dir: Path) -> None:
    validation = {
        "schema_version": "1.5",
        "run_id": run_dir.name,
        "primary_task_id": "T001",
        "status": "pass",
        "verdict": "accepted",
        "validated_at": "2026-06-12T00:00:00Z",
        "validator": "deterministic_validation_evidence_collector",
        "validation_mode": "deterministic_evidence_with_optional_agent_review",
        "accepted_task_ids": ["T001"],
        "rejected_task_ids": [],
        "task_results": [{"task_id": "T001", "relationship": "primary", "status": "pass", "failures": [], "warnings": []}],
        "multi_task_absorption": {"accepted_task_ids": [], "candidate_task_ids": [], "policy": "controlled_multi_task_absorption"},
        "failures": [],
        "warnings": [],
        "summary": "Forged passing validation.",
    }
    (run_dir / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class WorkerWriteBoundaryPolicyTest(unittest.TestCase):
    def test_validator_and_reconciler_deny_out_of_boundary_worker_write_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, service_b, paths, workflow = init_monorepo_workspace(tmp)
            write_plan(paths, workflow)
            run_dir = write_worker_run(paths, service_a, service_b / "worker_wrote_sibling.txt")

            validation = run_validator(service_a, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "fail")
            self.assertFalse(validation["worker_write_boundary"]["ok"])
            self.assertEqual(validation["accepted_task_ids"], [])
            checks = validation["task_results"][0]["checks"]
            self.assertIn("workspace_boundary_writes", {check["name"] for check in checks})
            self.assertIn("../service-b/worker_wrote_sibling.txt", json.dumps(validation["worker_write_boundary"], sort_keys=True))

            write_forged_passing_validation(run_dir)
            reconciled = run_reconciler(service_a, task_id="T001", run_dir=run_dir)

            self.assertFalse(reconciled["ok"])
            self.assertEqual(reconciled["status"], "workspace_boundary_violation")
            self.assertIn("- [ ] T001: Produce result artifact", paths.plan_file.read_text(encoding="utf-8"))
            self.assertFalse((paths.results_dir / "T001" / "latest.json").exists())

    def test_explicit_plan_and_security_allow_out_of_boundary_worker_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, service_b, paths, workflow = init_monorepo_workspace(tmp)
            configure_fake_summary_agent(service_a)
            sibling = service_b / "allowed_sibling.txt"
            relative_sibling = os.path.relpath(sibling, start=service_a).replace(os.sep, "/")
            allow_out_of_boundary_path(service_a, paths, relative_sibling)
            write_plan(paths, workflow, allow_path=relative_sibling)
            run_dir = write_worker_run(paths, service_a, sibling, run_id="run_allowed_oob")

            validation = run_validator(service_a, task_id="T001", run_dir=run_dir)
            reconciled = run_reconciler(service_a, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            self.assertTrue(validation["worker_write_boundary"]["ok"])
            self.assertEqual(validation["worker_write_boundary"]["violations"], [])
            self.assertIn(relative_sibling, json.dumps(validation["worker_write_boundary"], sort_keys=True))
            self.assertTrue(reconciled["ok"], json.dumps(reconciled, indent=2, sort_keys=True))
            self.assertEqual(reconciled["status"], "reconciled")
            self.assertIn("- [x] T001: Produce result artifact", paths.plan_file.read_text(encoding="utf-8"))

    def test_scheduler_classifies_out_of_boundary_worker_write_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, _service_b, paths, workflow = init_monorepo_workspace(tmp)
            write_plan(paths, workflow)
            accept_plan_hash(paths)
            script = service_a / "worker_oob.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    project = Path(os.environ["LOOPPLANE_PROJECT_ROOT"])
                    run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                    sibling = project.parent / "service-b" / "scheduler_sibling.txt"
                    sibling.parent.mkdir(parents=True, exist_ok=True)
                    sibling.write_text("scheduler worker wrote outside boundary\\n", encoding="utf-8")
                    relative_sibling = os.path.relpath(sibling, start=project).replace(os.sep, "/")
                    for child in ("artifacts", "logs", "raw", "git"):
                        (run_dir / child).mkdir(parents=True, exist_ok=True)
                    (run_dir / "artifacts" / "result.txt").write_text("ok\\n", encoding="utf-8")
                    (run_dir / "report.md").write_text("# Worker Report\\n\\nCompleted.\\n", encoding="utf-8")
                    (run_dir / "commands.sh").write_text("python worker_oob.py\\n", encoding="utf-8")
                    (run_dir / "git" / "changed_files.json").write_text(json.dumps({
                        "schema_version": "1.5",
                        "task_id": "T001",
                        "changed_files": [{"path": relative_sibling, "change_type": "added"}],
                    }, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                    status = {
                        "schema_version": "1.5",
                        "run_id": run_dir.name,
                        "task_id": "T001",
                        "primary_task_id": "T001",
                        "phase": "Phase P0: Boundary Policy",
                        "status": "completed",
                        "next_prompt_ready": True,
                        "project_changes": [{"path": relative_sibling, "change_type": "added"}],
                        "commands_run": [{"cmd": "python worker_oob.py", "exit_code": 0}],
                        "key_outputs": ["artifacts/result.txt"],
                        "evidence_satisfies": [{"task_id": "T001", "relationship": "primary", "evidence": ["artifacts/result.txt"]}],
                        "validation_claim": {"claim": "completed", "checks_claimed": [{"name": "self_claim", "status": "pass"}], "limitations": []},
                        "summary_candidate": {"one_line": "Worker completed.", "highlights": [], "warnings": [], "blockers": []},
                        "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                        "repair_attempts": [],
                        "known_risks": [],
                        "remaining_incomplete_items": [],
                    }
                    (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config_path = paths.config_file("agent_runners.json")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            runner = config["runners"]["worker"]
            runner["adapter"] = "shell"
            runner["command"] = sys.executable
            runner["args"] = [script.as_posix()]
            runner["cwd"] = "{{project_root}}"
            runner["prompt_delivery"] = {"mode": "stdin"}
            runner["timeout_seconds"] = 10
            runner["doctor"] = {"check_command": f"{sys.executable} --version", "requires_auth": False}
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_scheduler(service_a, max_ticks=1)

            execution = result["selected_action"]["execution_result"]
            self.assertFalse(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["classification"], "worker_boundary_violation")
            self.assertEqual(execution["status"], "failed_agent")
            self.assertFalse(execution["worker_write_boundary"]["ok"])
            self.assertIn("../service-b/scheduler_sibling.txt", json.dumps(execution["worker_write_boundary"], sort_keys=True))
            self.assertIn("- [ ] T001: Produce result artifact", paths.plan_file.read_text(encoding="utf-8"))

    def test_scheduler_fails_unreported_adapter_out_of_boundary_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, _service_b, paths, workflow = init_monorepo_workspace(tmp)
            write_plan(paths, workflow)
            accept_plan_hash(paths)
            script = service_a / "adapter_unreported_oob.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    project = Path(os.environ["LOOPPLANE_PROJECT_ROOT"])
                    run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                    sibling = project.parent / "service-b" / "adapter_unreported_sibling.txt"
                    sibling.write_text("adapter wrote outside boundary without reporting it\\n", encoding="utf-8")
                    for child in ("artifacts", "logs", "raw", "git"):
                        (run_dir / child).mkdir(parents=True, exist_ok=True)
                    (run_dir / "artifacts" / "result.txt").write_text("ok\\n", encoding="utf-8")
                    (run_dir / "report.md").write_text("# Worker Report\\n\\nCompleted.\\n", encoding="utf-8")
                    (run_dir / "commands.sh").write_text("python adapter_unreported_oob.py\\n", encoding="utf-8")
                    status = {
                        "schema_version": "1.5",
                        "run_id": run_dir.name,
                        "task_id": "T001",
                        "primary_task_id": "T001",
                        "phase": "Phase P0: Boundary Policy",
                        "status": "completed",
                        "next_prompt_ready": True,
                        "project_changes": [],
                        "commands_run": [{"cmd": "python adapter_unreported_oob.py", "exit_code": 0}],
                        "key_outputs": ["artifacts/result.txt"],
                        "evidence_satisfies": [{"task_id": "T001", "relationship": "primary", "evidence": ["artifacts/result.txt"]}],
                        "validation_claim": {"claim": "completed", "checks_claimed": [{"name": "self_claim", "status": "pass"}], "limitations": []},
                        "summary_candidate": {"one_line": "Worker completed.", "highlights": [], "warnings": [], "blockers": []},
                        "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                        "repair_attempts": [],
                        "known_risks": [],
                        "remaining_incomplete_items": [],
                    }
                    (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                    Path(os.environ["LOOPPLANE_FINAL_OUTPUT"]).write_text("worker claimed completion\\n", encoding="utf-8")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config_path = paths.config_file("agent_runners.json")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            runner = config["runners"]["worker"]
            runner["adapter"] = "shell"
            runner["command"] = sys.executable
            runner["args"] = [script.as_posix()]
            runner["cwd"] = "{{project_root}}"
            runner["prompt_delivery"] = {"mode": "stdin"}
            runner["timeout_seconds"] = 10
            runner["doctor"] = {"check_command": f"{sys.executable} --version", "requires_auth": False}
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_scheduler(service_a, max_ticks=1)

            execution = result["selected_action"]["execution_result"]
            adapter_result = json.loads((service_a / execution["adapter_result_path"]).read_text(encoding="utf-8"))
            self.assertFalse(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["classification"], "worker_boundary_violation")
            self.assertEqual(execution["status"], "failed_agent")
            self.assertFalse(execution["worker_write_boundary"]["ok"])
            self.assertEqual(adapter_result["exit_code"], 126)
            self.assertFalse(adapter_result["adapter_metadata"]["workspace_boundary_policy"]["ok"])
            self.assertIn(
                "../service-b/adapter_unreported_sibling.txt",
                json.dumps(adapter_result["adapter_metadata"]["workspace_boundary_policy"], sort_keys=True),
            )
            self.assertIn("- [ ] T001: Produce result artifact", paths.plan_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
