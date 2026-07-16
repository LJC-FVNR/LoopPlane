from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.detached import _should_continue_after_tick, load_supervisor_status, run_supervisor
from runtime.init_workflow import init_project
from runtime.plan_objectives import objective_structure_fingerprint, parse_plan_objectives
from runtime.scheduler import SchedulerLockError
from tests.test_objective_gates import configure_fake_objective_verifier


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def run_json(*args: str, expect: int = 0) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def configure_shell_worker(project: Path, script_path: Path) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    runner = config["runners"]["worker"]
    runner["adapter"] = "shell"
    runner["command"] = sys.executable
    runner["args"] = [script_path.as_posix()]
    runner["cwd"] = "{{project_root}}"
    runner["prompt_delivery"] = {"mode": "stdin"}
    runner["timeout_seconds"] = 20
    runner["stream_logs"] = True
    runner["enabled"] = True
    runner["doctor"] = {"check_command": f"{sys.executable} --version", "requires_auth": False}
    for runner_id in ("validator", "final_reviewer", "summary"):
        if runner_id in config["runners"]:
            config["runners"][runner_id]["enabled"] = False
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    configure_fake_objective_verifier(project)


def write_active_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Detached Runtime

- [ ] T001: Run detached worker
  - acceptance: Detached worker writes result artifact.
  - acceptance: Worker report records detached completion.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Detached worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.

## Final Objective Checklist

- [ ] `FO1` Detached runtime smoke reaches completion with expected artifacts.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Confirm the detached worker completed and produced the expected artifact.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")
    write_final_objective_report(project)


def write_two_task_active_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Detached Runtime

- [ ] T001: Run first detached worker
  - acceptance: Detached worker writes result artifact.
  - acceptance: Worker report records detached completion.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Detached worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.

- [ ] T002: Run second detached worker
  - acceptance: Detached worker writes result artifact.
  - acceptance: Worker report records detached completion.
  - evidence: .loopplane/results/T002/
  - latest: .loopplane/results/T002/latest.json
  - depends_on: [T001]
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Detached worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.

## Final Objective Checklist

- [ ] `FO1` Detached runtime smoke reaches completion with expected artifacts.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm the detached workers completed and produced the expected artifacts.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")
    write_final_objective_report(project)


def write_final_objective_report(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
    objectives, _errors = parse_plan_objectives(plan_text)
    workflow_objectives = [objective for objective in objectives if objective.scope == "workflow"]
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
                "status": "satisfied",
                "verified_at": "2026-06-10T00:00:00Z",
                "plan_sha256": "sha256:" + sha256(plan_text.encode("utf-8")).hexdigest(),
                "objective_structure_fingerprint": objective_structure_fingerprint(
                    plan_text,
                    objectives=workflow_objectives,
                ),
                "objective_results": [
                    {
                        "objective_id": "FO1",
                        "status": "satisfied",
                        "verdict": "satisfied",
                        "confidence": "high",
                        "evidence_reviewed": [".loopplane/results/"],
                        "agent_rationale": "Detached runtime smoke objective is pre-satisfied by the fixture.",
                        "expandable": False,
                    }
                ],
                "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def write_slow_worker(project: Path, *, sleep_seconds: float = 1.5) -> Path:
    script = project / "worker.py"
    source = textwrap.dedent(
        """
            import json
            import os
            import time
            from pathlib import Path

            project = Path(os.environ["LOOPPLANE_PROJECT_ROOT"])
            task_id = os.environ["LOOPPLANE_TASK_ID"]
            run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
            (project / "worker_started.txt").write_text("started\\n", encoding="utf-8")
            (project / f"worker_started_{task_id}.txt").write_text("started\\n", encoding="utf-8")
            time.sleep(__SLEEP_SECONDS__)
            (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "raw").mkdir(parents=True, exist_ok=True)
            (run_dir / "artifacts" / "result.txt").write_text(f"detached result for {task_id}\\n", encoding="utf-8")
            (run_dir / "report.md").write_text("# Worker Report\\n\\nDetached worker completed.\\n", encoding="utf-8")
            (run_dir / "commands.sh").write_text("python worker.py\\n", encoding="utf-8")
            status = {
                "schema_version": "1.5",
                "run_id": os.environ["LOOPPLANE_RUN_ID"],
                "task_id": task_id,
                "primary_task_id": task_id,
                "phase": "Phase P0: Detached Runtime",
                "status": "completed",
                "next_prompt_ready": True,
                "project_changes": [],
                "commands_run": [{"cmd": "python worker.py", "exit_code": 0}],
                "key_outputs": [str(run_dir / "artifacts" / "result.txt")],
                "evidence_satisfies": [
                    {
                        "task_id": task_id,
                        "relationship": "primary",
                        "acceptance_claimed": [
                            "Detached worker writes result artifact.",
                            "Worker report records detached completion.",
                        ],
                        "evidence": [
                            str(run_dir / "artifacts" / "result.txt"),
                            str(run_dir / "report.md"),
                        ],
                    }
                ],
                "validation_claim": {
                    "claim": "completed",
                    "checks_claimed": [{"name": "detached_worker", "status": "pass"}],
                    "limitations": [],
                },
                "summary_candidate": {
                    "one_line": "Detached smoke worker completed.",
                    "highlights": ["result artifact written"],
                    "warnings": [],
                    "blockers": [],
                },
                "background": {
                    "pids": [],
                    "commands": [],
                    "logs": [],
                    "heartbeat_required": False,
                    "wake_next_agent_when": None,
                },
                "repair_attempts": [],
                "known_risks": [],
                "remaining_incomplete_items": [],
            }
            (run_dir / "agent_status.json").write_text(
                json.dumps(status, indent=2, sort_keys=True) + "\\n",
                encoding="utf-8",
            )
            print("detached worker completed")
            """
    ).lstrip()
    script.write_text(source.replace("__SLEEP_SECONDS__", repr(float(sleep_seconds))), encoding="utf-8")
    return script


def wait_until(predicate: Any, *, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def mark_detached_requested(project: Path, *, status: str = "running") -> None:
    state_path = project / ".loopplane" / "runtime" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = status
    scheduler = state.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
    scheduler["detach_requested"] = True
    scheduler["running"] = status == "running"
    state["scheduler"] = scheduler
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stale_timestamp() -> str:
    return (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_stale_supervisor_metadata(
    project: Path,
    *,
    include_pid: bool = True,
    include_command: bool = True,
    include_log_paths: bool = True,
) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    old = stale_timestamp()
    metadata: dict[str, Any] = {
        "schema_version": "1.5",
        "workflow_id": workflow["workflow_id"],
        "project_root": project.as_posix(),
        "status": "running",
        "started_at": old,
        "updated_at": old,
        "heartbeat_at": old,
        "exit_status": None,
    }
    if include_pid:
        metadata["pid"] = 999999999
    if include_command:
        metadata["command"] = [sys.executable, "-m", "runtime.detached", "supervisor"]
    if include_log_paths:
        metadata["log_paths"] = {
            "stdout": ".loopplane/runtime/supervisor/supervisor_stdout.log",
            "stderr": ".loopplane/runtime/supervisor/supervisor_stderr.log",
        }
    metadata_path = project / ".loopplane" / "runtime" / "supervisor.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class DetachedRuntimeTest(unittest.TestCase):
    def test_supervisor_retries_transient_scheduler_authority_lock_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached scheduler lock-contention retry.")
            completed_tick = {
                "ok": True,
                "status": "ok",
                "exit_code": 0,
                "selected_action": {
                    "action": "complete",
                    "reason": "Workflow is complete.",
                    "selected": {},
                },
            }
            lock_error = SchedulerLockError(
                f"lock is already held: {project}/.loopplane/runtime/lock/event_append_lock/owner.json"
            )

            with mock.patch(
                "runtime.detached.run_scheduler",
                side_effect=[lock_error, completed_tick],
            ) as run_scheduler_mock, mock.patch("runtime.detached.time.sleep"):
                exit_code = run_supervisor(project)

            self.assertEqual(exit_code, 0)
            self.assertEqual(run_scheduler_mock.call_count, 2)
            status = load_supervisor_status(project)
            self.assertEqual(status["metadata"]["last_loop_reason"], "complete")
            self.assertNotEqual(status["metadata"].get("stop_reason"), "exception:SchedulerLockError")

    def test_detached_supervisor_continues_after_recoverable_validation_follow_up_failure(self) -> None:
        selected = {"action": "run_worker"}
        follow_up = {
            "ok": False,
            "status": "reconciliation_failed",
            "validation": {"status": "fail"},
            "reconciliation": {"status": "validation_failed"},
        }

        should_continue, reason, exit_code = _should_continue_after_tick(
            {"ok": False, "exit_code": 4},
            selected,
            follow_up,
        )

        self.assertTrue(should_continue)
        self.assertEqual(reason, "recovery_pending")
        self.assertEqual(exit_code, 0)

    def test_detached_supervisor_still_exits_after_nonrecoverable_follow_up_failure(self) -> None:
        selected = {"action": "run_worker"}
        follow_up = {
            "ok": False,
            "status": "invalid_worker_result",
            "message": "Worker result did not include task_id and role_output_dir for validation.",
        }

        should_continue, reason, exit_code = _should_continue_after_tick(
            {"ok": False, "exit_code": 4},
            selected,
            follow_up,
        )

        self.assertFalse(should_continue)
        self.assertEqual(reason, "follow_up_failed")
        self.assertNotEqual(exit_code, 0)

    def test_start_detach_launches_supervisor_and_advances_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached scheduler smoke.")
            write_active_plan(project)
            worker = write_slow_worker(project)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")

            self.assertEqual(start["status"], "started")
            self.assertTrue(start["supervisor"]["pid"])
            self.assertEqual(start["supervisor"]["liveness"], "alive")
            self.assertTrue((project / ".loopplane" / "runtime" / "supervisor.json").is_file())
            self.assertFalse((project / ".loopplane" / "results" / "T001" / "latest.json").exists())
            self.assertTrue(wait_until(lambda: (project / "worker_started.txt").is_file()))

            latest = project / ".loopplane" / "results" / "T001" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest.is_file() and completion.is_file(), timeout=20.0))
            self.assertIn("- [x] T001: Run detached worker", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "completed",
                    timeout=10.0,
                )
            )

            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["supervisor"]["status"], "completed")
            self.assertIn(status["supervisor"]["liveness"], {"alive", "dead"})

            logs = run_json("logs", "--project", str(project), "--json")
            self.assertTrue(logs["supervisor"]["exists"])
            self.assertTrue(logs["supervisor_logs"]["stdout_path"])
            self.assertTrue(logs["supervisor_logs"]["stderr_path"])

    def test_detached_supervisor_ticks_through_multiple_tasks_until_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached scheduler multi-tick smoke.")
            write_two_task_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.05)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")

            self.assertEqual(start["status"], "started")
            latest_t001 = project / ".loopplane" / "results" / "T001" / "latest.json"
            latest_t002 = project / ".loopplane" / "results" / "T002" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(
                wait_until(lambda: latest_t001.is_file() and latest_t002.is_file() and completion.is_file(), timeout=20.0)
            )
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: Run first detached worker", plan_text)
            self.assertIn("- [x] T002: Run second detached worker", plan_text)
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "completed",
                    timeout=10.0,
                )
            )

            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["supervisor"]["status"], "completed")

    def test_attach_reports_active_detached_supervisor_tail_status_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached attach smoke.")
            write_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=2.0)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")
            self.assertEqual(start["status"], "started")
            self.assertTrue(wait_until(lambda: (project / "worker_started.txt").is_file(), timeout=10.0))

            attach = run_json("attach", "--project", str(project), "--lines", "20", "--json")
            self.assertTrue(attach["ok"], json.dumps(attach, indent=2, sort_keys=True))
            self.assertEqual(attach["status"], "attached")
            self.assertTrue(attach["active"])
            self.assertEqual(attach["supervisor"]["liveness"], "alive")
            self.assertTrue(attach["tail"]["supervisor_stdout_path"])
            self.assertTrue(attach["tail"]["supervisor_stderr_path"])
            self.assertTrue(attach["tail"]["events"])

            text = subprocess.run(
                [sys.executable, str(LoopPlane), "attach", "--project", str(project), "--lines", "20"],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("loopplane attach: attached", text.stdout)
            self.assertIn("supervisor_liveness: alive", text.stdout)
            self.assertIn("events:", text.stdout)

            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["supervisor"]["liveness"], "alive")
            self.assertIn(status["supervisor"]["status"], {"running", "paused", "waiting_background"})
            logs = run_json("logs", "--project", str(project), "--lines", "20", "--json")
            self.assertTrue(logs["supervisor"]["exists"])
            self.assertTrue(logs["supervisor_logs"]["stdout_path"])
            self.assertTrue(logs["supervisor_logs"]["stderr_path"])

            latest = project / ".loopplane" / "results" / "T001" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest.is_file() and completion.is_file(), timeout=20.0))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"]
                    == "completed",
                    timeout=10.0,
                )
            )

    def test_attach_reports_no_active_and_stale_supervisor_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached attach no active smoke.")

            missing = run_json("attach", "--project", str(project), "--json", expect=1)
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["status"], "no_active_supervisor")
            self.assertFalse(missing["active"])
            self.assertFalse(missing["supervisor"]["exists"])

            text = subprocess.run(
                [sys.executable, str(LoopPlane), "attach", "--project", str(project)],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(text.returncode, 1, text.stderr + text.stdout)
            self.assertIn("loopplane attach: no_active_supervisor", text.stdout)
            self.assertIn("No detached supervisor metadata exists", text.stdout)

            metadata_path = project / ".loopplane" / "runtime" / "supervisor.json"
            old = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "project_root": project.as_posix(),
                        "status": "running",
                        "pid": 999999999,
                        "started_at": old,
                        "updated_at": old,
                        "heartbeat_at": old,
                        "command": [sys.executable, "-m", "runtime.detached", "supervisor"],
                        "log_paths": {},
                        "exit_status": None,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            stale = run_json("attach", "--project", str(project), "--json", expect=1)
            self.assertFalse(stale["ok"])
            self.assertEqual(stale["status"], "stale_supervisor")
            self.assertEqual(stale["supervisor"]["status"], "stale")
            self.assertEqual(stale["supervisor"]["liveness"], "dead")
            self.assertIn("Supervisor PID is no longer alive.", stale["warnings"])

    def test_detached_pause_keeps_supervisor_alive_and_resume_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached pause and resume smoke.")
            write_two_task_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.6)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")
            self.assertEqual(start["status"], "started")
            self.assertTrue(wait_until(lambda: (project / "worker_started_T001.txt").is_file(), timeout=10.0))

            pause = run_json("pause", "--project", str(project), "--json")
            self.assertEqual(pause["request"]["type"], "pause")

            latest_t001 = project / ".loopplane" / "results" / "T001" / "latest.json"
            latest_t002 = project / ".loopplane" / "results" / "T002" / "latest.json"
            t002_started = project / "worker_started_T002.txt"

            self.assertTrue(
                wait_until(
                    lambda: latest_t001.is_file()
                    and run_json("status", "--project", str(project), "--json")["runtime_status"] == "paused",
                    timeout=15.0,
                )
            )
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "paused",
                    timeout=5.0,
                )
            )
            paused_status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(paused_status["supervisor"]["liveness"], "alive")
            self.assertFalse(t002_started.exists())
            time.sleep(1.2)
            self.assertFalse(t002_started.exists())
            self.assertFalse(latest_t002.exists())

            resume = run_json("resume", "--project", str(project), "--json")
            self.assertEqual(resume["request"]["type"], "resume")

            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest_t002.is_file() and completion.is_file(), timeout=20.0))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "completed",
                    timeout=10.0,
                )
            )
            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["supervisor"]["status"], "completed")

    def test_detached_stop_waits_for_safe_point_and_does_not_start_next_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached stop safe point smoke.")
            write_two_task_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.6)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")
            self.assertEqual(start["status"], "started")
            self.assertTrue(wait_until(lambda: (project / "worker_started_T001.txt").is_file(), timeout=10.0))

            stop = run_json("stop", "--project", str(project), "--json")
            self.assertEqual(stop["request"]["type"], "stop")

            latest_t001 = project / ".loopplane" / "results" / "T001" / "latest.json"
            latest_t002 = project / ".loopplane" / "results" / "T002" / "latest.json"
            t002_started = project / "worker_started_T002.txt"
            self.assertTrue(
                wait_until(
                    lambda: latest_t001.is_file()
                    and run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "stopped",
                    timeout=20.0,
                )
            )
            time.sleep(0.8)

            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "stopped")
            self.assertEqual(status["supervisor"]["status"], "stopped")
            self.assertNotEqual(status["supervisor"]["status"], "stale")
            self.assertEqual(status["supervisor"]["metadata"]["exit_status"], "stopped")
            self.assertEqual(status["supervisor"]["metadata"]["stop_reason"], "wait_stopped")
            self.assertEqual(status["supervisor"]["metadata"]["last_follow_up"]["status"], "reconciled")
            self.assertEqual(status["supervisor"]["metadata"]["last_follow_up"]["task_id"], "T001")
            self.assertEqual(status["pending_count"], 0)
            self.assertFalse(t002_started.exists())
            self.assertFalse(latest_t002.exists())

            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: Run first detached worker", plan_text)
            self.assertIn("- [ ] T002: Run second detached worker", plan_text)

    def test_detached_resume_after_stopped_restarts_supervisor_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached resume stopped smoke.")
            write_two_task_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.2)
            configure_shell_worker(project, worker)

            start = run_json("start", "--detach", "--project", str(project), "--json")
            self.assertEqual(start["status"], "started")
            self.assertTrue(wait_until(lambda: (project / "worker_started_T001.txt").is_file(), timeout=10.0))
            stop = run_json("stop", "--project", str(project), "--json")
            self.assertEqual(stop["request"]["type"], "stop")

            latest_t001 = project / ".loopplane" / "results" / "T001" / "latest.json"
            self.assertTrue(
                wait_until(
                    lambda: latest_t001.is_file()
                    and run_json("status", "--project", str(project), "--json")["supervisor"]["status"] == "stopped",
                    timeout=20.0,
                )
            )

            resume = run_json("resume", "--project", str(project), "--json")
            self.assertEqual(resume["request"]["type"], "resume")
            self.assertTrue(resume["detached_resume"]["attempted"])
            self.assertEqual(resume["detached_resume"]["status"], "started")
            self.assertEqual(resume["supervisor"]["liveness"], "alive")

            latest_t002 = project / ".loopplane" / "results" / "T002" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest_t002.is_file() and completion.is_file(), timeout=20.0))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"]
                    == "completed",
                    timeout=10.0,
                )
            )
            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["pending_count"], 0)
            self.assertEqual(status["supervisor"]["status"], "completed")
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: Run first detached worker", plan_text)
            self.assertIn("- [x] T002: Run second detached worker", plan_text)

    def test_detached_resume_recovers_stale_pid_supervisor_and_preserves_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached stale PID recovery smoke.")
            write_two_task_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.05)
            configure_shell_worker(project, worker)
            mark_detached_requested(project)
            write_stale_supervisor_metadata(project)

            before = run_json("status", "--project", str(project), "--json")
            self.assertEqual(before["runtime_status"], "running")
            self.assertEqual(before["supervisor"]["status"], "stale")
            self.assertEqual(before["supervisor"]["liveness"], "dead")
            self.assertIn("dead_process", before["supervisor"]["status_problems"])

            resume = run_json("resume", "--project", str(project), "--json")
            self.assertEqual(resume["request"]["type"], "resume")
            self.assertTrue(resume["detached_resume"]["attempted"])
            self.assertEqual(resume["detached_resume"]["reason"], "stale_supervisor")
            self.assertEqual(resume["supervisor"]["liveness"], "alive")

            latest_t001 = project / ".loopplane" / "results" / "T001" / "latest.json"
            latest_t002 = project / ".loopplane" / "results" / "T002" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest_t001.is_file() and latest_t002.is_file() and completion.is_file(), timeout=20.0))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"]
                    == "completed",
                    timeout=10.0,
                )
            )

            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["supervisor"]["status"], "completed")
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: Run first detached worker", plan_text)
            self.assertIn("- [x] T002: Run second detached worker", plan_text)

    def test_detached_resume_recovers_incomplete_stale_supervisor_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Detached incomplete supervisor recovery smoke.")
            write_active_plan(project)
            worker = write_slow_worker(project, sleep_seconds=0.05)
            configure_shell_worker(project, worker)
            mark_detached_requested(project)
            write_stale_supervisor_metadata(project, include_pid=False, include_command=False, include_log_paths=False)

            before = run_json("status", "--project", str(project), "--json")
            self.assertEqual(before["supervisor"]["status"], "stale")
            self.assertEqual(before["supervisor"]["liveness"], "unknown")
            self.assertIn("stale_heartbeat", before["supervisor"]["status_problems"])
            self.assertIn("incomplete_metadata", before["supervisor"]["status_problems"])
            self.assertTrue(any("incomplete" in warning for warning in before["warnings"]))

            resume = run_json("resume", "--project", str(project), "--json")
            self.assertTrue(resume["detached_resume"]["attempted"])
            self.assertEqual(resume["detached_resume"]["reason"], "stale_supervisor")
            self.assertEqual(resume["supervisor"]["liveness"], "alive")

            latest = project / ".loopplane" / "results" / "T001" / "latest.json"
            completion = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            self.assertTrue(wait_until(lambda: latest.is_file() and completion.is_file(), timeout=20.0))
            self.assertTrue(
                wait_until(
                    lambda: run_json("status", "--project", str(project), "--json")["supervisor"]["status"]
                    == "completed",
                    timeout=10.0,
                )
            )
            status = run_json("status", "--project", str(project), "--json")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertEqual(status["supervisor"]["status"], "completed")

    def test_detached_commands_surface_missing_workflow_config_as_waiting_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            status = run_json("status", "--project", str(project), "--json", expect=1)
            self.assertEqual(status["status"], "waiting_config")
            self.assertEqual(status["configuration_problems"][0]["code"], "workflow_config_unavailable")
            self.assertTrue(status["configuration_problems"][0]["recoverable"])
            self.assertTrue(status["configuration_problems"][0]["recovery_actions"])

            resume = run_json("resume", "--project", str(project), "--json", expect=1)
            self.assertEqual(resume["status"], "waiting_config")
            self.assertEqual(resume["configuration_problems"][0]["code"], "workflow_config_unavailable")

            start = run_json("start", "--detach", "--project", str(project), "--json", expect=1)
            self.assertEqual(start["status"], "waiting_config")
            self.assertEqual(start["configuration_problems"][0]["code"], "workflow_config_unavailable")

    def test_supervisor_status_classifies_stale_pid_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Stale detached scheduler metadata.")
            metadata_path = project / ".loopplane" / "runtime" / "supervisor.json"
            old = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "project_root": project.as_posix(),
                        "status": "running",
                        "pid": 999999999,
                        "started_at": old,
                        "updated_at": old,
                        "heartbeat_at": old,
                        "command": [sys.executable, "-m", "runtime.detached", "supervisor"],
                        "log_paths": {},
                        "exit_status": None,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            status = load_supervisor_status(project)

            self.assertTrue(status["exists"])
            self.assertEqual(status["status"], "stale")
            self.assertEqual(status["liveness"], "dead")
            self.assertTrue(status["heartbeat_stale"])

    def test_supervisor_status_accepts_fresh_owned_active_run_lease_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Active lease covers blocking supervisor tick.")
            runtime_dir = project / ".loopplane" / "runtime"
            metadata_path = runtime_dir / "supervisor.json"
            old = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "project_root": project.as_posix(),
                        "status": "running",
                        "pid": os.getpid(),
                        "started_at": old,
                        "updated_at": old,
                        "heartbeat_at": old,
                        "command": [sys.executable, "-m", "runtime.detached", "supervisor"],
                        "log_paths": {"stdout": "supervisor_stdout.log", "stderr": "supervisor_stderr.log"},
                        "exit_status": None,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            lease_dir = runtime_dir / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            (lease_dir / "run_live.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "run_id": "run_live",
                        "status": "running",
                        "heartbeat_at": now,
                        "adapter_pid": os.getpid(),
                        "scheduler_pid": os.getpid(),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            status = load_supervisor_status(project)

            self.assertEqual(status["status"], "running")
            self.assertEqual(status["liveness"], "alive")
            self.assertFalse(status["heartbeat_stale"])
            self.assertTrue(status["metadata_heartbeat_stale"])
            self.assertTrue(status["heartbeat_covered_by_active_run_lease"])
            self.assertEqual(status["active_run_lease_id"], "run_live")
            self.assertEqual(status["warnings"], [])


if __name__ == "__main__":
    unittest.main()
