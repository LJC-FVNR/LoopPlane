from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import runtime.background_jobs as background_jobs_runtime
from runtime.adapters.base import utc_timestamp
from runtime.background_jobs import (
    cancel_background_job,
    complete_background_job,
    list_background_jobs,
    start_background_job,
    _run_supervisor,
    _run_watchdog_check,
    _start_supervisor_record_update,
    _watchdog_allowed_paths,
)
from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.scheduler import SchedulerLockError, load_scheduler_snapshot, select_next_action


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def configure_watchdog_inspector(project: Path) -> None:
    script = project / ".loopplane_agents" / "watchdog_inspector.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
if "slow needs recovery fixture" in prompt:
    time.sleep(1.5)
    watchdog = {
        "job_id": "from_prompt",
        "healthy_progress": False,
        "recommended_status": "needs_recovery",
        "issue_summary": "late stalled fixture detected",
        "repair_actions_taken": [],
        "follow_up_needed": "recover the background job",
        "confidence": "high",
    }
    answer = "Watchdog slowly found a stalled fixture."
elif "needs recovery fixture" in prompt:
    watchdog = {
        "job_id": "from_prompt",
        "healthy_progress": False,
        "recommended_status": "needs_recovery",
        "issue_summary": "stalled fixture detected",
        "repair_actions_taken": [],
        "follow_up_needed": "recover the background job",
        "confidence": "high",
    }
    answer = "Watchdog found a stalled fixture."
else:
    watchdog = {
        "job_id": "from_prompt",
        "healthy_progress": True,
        "recommended_status": "running",
        "issue_summary": "",
        "repair_actions_taken": [],
        "follow_up_needed": "",
        "confidence": "high",
    }
    answer = "Watchdog confirms healthy progress."
response_path = Path(os.environ["LOOPPLANE_INSPECTION_RESPONSE_PATH"])
response_path.parent.mkdir(parents=True, exist_ok=True)
response_path.write_text(json.dumps({"answer": answer, "summary": answer, "background_watchdog": watchdog}) + "\\n", encoding="utf-8")
print(answer)
""",
        encoding="utf-8",
    )
    workflow_config = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow_config)
    runners_path = paths.config_file("agent_runners.json")
    runners = json.loads(runners_path.read_text(encoding="utf-8"))
    inspector = runners["runners"]["inspector"]
    inspector.update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "timeout_seconds": 10,
            "enabled": True,
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "doctor": {"check_command": f"{sys.executable} --version", "check_kind": "doctor_check", "requires_auth": False},
        }
    )
    runners_path.write_text(json.dumps(runners, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class BackgroundJobRuntimeTest(unittest.TestCase):
    def test_starting_handoff_remains_active_during_supervisor_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Accept the supervisor startup transition.")
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)
            (paths.runtime_dir / "background_jobs.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": workflow_config["workflow_id"],
                        "jobs": [
                            {
                                "job_id": "starting_fixture",
                                "status": "starting",
                                "next_prompt_ready": False,
                                "heartbeat_at": utc_timestamp(),
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = list_background_jobs(project, job_id="starting_fixture")

            self.assertEqual(result["status"], "waiting_background_job")
            self.assertEqual(result["jobs"][0]["status"], "starting")
            self.assertFalse(result["jobs"][0]["next_prompt_ready"])

    def test_loopplane_supervised_background_job_blocks_scheduler_until_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Track a long-running command under LoopPlane supervision.")

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                task_id="P0.T001",
                run_id="run_background_fixture",
                wake_next_agent_when="Continue after the supervised fixture exits.",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            job_id = result["job_id"]
            self.assertEqual(result["agent_status_fragment"]["status"], "running_background")
            self.assertFalse(result["agent_status_fragment"]["next_prompt_ready"])
            try:
                status = list_background_jobs(project, job_id=job_id)
                self.assertEqual(status["jobs"][0]["status"], "running")
                self.assertFalse(status["jobs"][0]["next_prompt_ready"])

                action = select_next_action(load_scheduler_snapshot(project))

                self.assertEqual(action["action"], "wait_background_job")
                self.assertEqual(action["selected"]["job_id"], job_id)
            finally:
                cancel_background_job(project, job_id, reason="test cleanup")

    def test_external_supervisor_signal_does_not_wake_next_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Externally interrupted background jobs should not auto-wake.")

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                task_id="P0.T001",
                run_id="run_background_signal",
                wake_next_agent_when="Continue only after the long command is explicitly resolved.",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            job_id = result["job_id"]
            supervisor_pid = int(result["supervisor_pid"])
            try:
                deadline = time.monotonic() + 5.0
                latest: dict[str, object] | None = None
                while time.monotonic() < deadline:
                    latest = list_background_jobs(project, job_id=job_id)
                    job = latest["jobs"][0]
                    if job.get("status") == "running" and job.get("child_pid"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"background job {job_id} did not start child process; latest={latest}")

                os.kill(supervisor_pid, signal.SIGTERM)

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    latest = list_background_jobs(project, job_id=job_id)
                    job = latest["jobs"][0]
                    if job.get("status") == "cancelled" and str(job.get("status_problem") or "").startswith("supervisor_signal:"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"background job {job_id} did not record supervisor signal; latest={latest}")

                self.assertFalse(job["next_prompt_ready"])
                action = select_next_action(load_scheduler_snapshot(project))
                self.assertEqual(action["action"], "wait_background_job")
                self.assertEqual(action["selected"]["job_id"], job_id)
            finally:
                cancel_background_job(project, job_id, reason="test cleanup")

    def test_supervisor_marks_successful_background_job_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Complete a supervised background command.")
            marker = project / "background_done.txt"

            result = start_background_job(
                project,
                command=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('done', encoding='utf-8')",
                ],
                task_id="P0.T001",
                run_id="run_background_complete",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            job_id = result["job_id"]
            status = self._wait_for_job_status(project, job_id, "completed")

            self.assertEqual(status["jobs"][0]["status"], "completed")
            self.assertTrue(status["jobs"][0]["next_prompt_ready"])
            self.assertEqual(status["jobs"][0]["exit_code"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "done")

    def test_supervisor_defers_contended_heartbeat_without_stopping_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Transient registry contention must not stop healthy work.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            job_id = "heartbeat_lock_contention_fixture"
            job_dir = paths.runtime_dir / "background_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            marker = project / "heartbeat_lock_contention_done.txt"
            launch_path = job_dir / "launch.json"
            launch_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "project_root": project.as_posix(),
                        "workflow_id": workflow_id,
                        "job_id": job_id,
                        "command": [
                            sys.executable,
                            "-c",
                            (
                                "import time; from pathlib import Path; time.sleep(0.8); "
                                f"Path({str(marker)!r}).write_text('done', encoding='utf-8')"
                            ),
                        ],
                        "cwd": project.as_posix(),
                        "stdout_path": (job_dir / "stdout.log").as_posix(),
                        "stderr_path": (job_dir / "stderr.log").as_posix(),
                        "supervisor_log_path": (job_dir / "supervisor.log").as_posix(),
                        "exit_code_file": (job_dir / "exit_code.txt").as_posix(),
                        "heartbeat_seconds": 0.5,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": job_id,
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "launch_path": launch_path.relative_to(project).as_posix(),
                    }
                ],
            )
            real_update_job = background_jobs_runtime._update_job
            update_calls = 0

            def contend_once(*args: object, **kwargs: object) -> object:
                nonlocal update_calls
                update_calls += 1
                if update_calls == 3:
                    raise SchedulerLockError("transient heartbeat lock contention")
                return real_update_job(*args, **kwargs)

            with mock.patch.object(background_jobs_runtime, "_update_job", side_effect=contend_once):
                exit_code = _run_supervisor(project, workflow_id=workflow_id, job_id=job_id, launch_path=launch_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "done")
            status = list_background_jobs(project, job_id=job_id)
            self.assertEqual(status["jobs"][0]["status"], "completed")
            supervisor_log = (job_dir / "supervisor.log").read_text(encoding="utf-8")
            self.assertIn("heartbeat_registry_lock_contended update_deferred", supervisor_log)

    def test_supervisor_terminal_lock_contention_keeps_exit_code_evidence_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Terminal registry contention must not erase command completion.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            job_id = "terminal_lock_contention_fixture"
            job_dir = paths.runtime_dir / "background_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            exit_code_file = job_dir / "exit_code.txt"
            launch_path = job_dir / "launch.json"
            launch_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "project_root": project.as_posix(),
                        "workflow_id": workflow_id,
                        "job_id": job_id,
                        "command": [sys.executable, "-c", "print('completed')"],
                        "cwd": project.as_posix(),
                        "stdout_path": (job_dir / "stdout.log").as_posix(),
                        "stderr_path": (job_dir / "stderr.log").as_posix(),
                        "supervisor_log_path": (job_dir / "supervisor.log").as_posix(),
                        "exit_code_file": exit_code_file.as_posix(),
                        "heartbeat_seconds": 0.1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": job_id,
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "exit_code_file": exit_code_file.relative_to(project).as_posix(),
                        "launch_path": launch_path.relative_to(project).as_posix(),
                    }
                ],
            )
            real_update_job = background_jobs_runtime._update_job

            def contend_on_completed_update(*args: object, **kwargs: object) -> object:
                update = kwargs["update"]
                candidate = update({"job_id": job_id, "status": "running", "next_prompt_ready": False})
                if candidate.get("status") == "completed":
                    raise SchedulerLockError("terminal lock contention")
                return real_update_job(*args, **kwargs)

            with mock.patch.object(background_jobs_runtime, "_update_job", side_effect=contend_on_completed_update):
                supervisor_exit_code = _run_supervisor(
                    project,
                    workflow_id=workflow_id,
                    job_id=job_id,
                    launch_path=launch_path,
                )

            self.assertEqual(supervisor_exit_code, 0)
            self.assertEqual(exit_code_file.read_text(encoding="utf-8").strip(), "0")
            supervisor_log = (job_dir / "supervisor.log").read_text(encoding="utf-8")
            self.assertIn("command_terminal_registry_lock_contended retry_exhausted", supervisor_log)
            status = list_background_jobs(project, job_id=job_id)
            self.assertEqual(status["jobs"][0]["status"], "completed")
            self.assertTrue(status["jobs"][0]["next_prompt_ready"])

    def test_continue_on_fail_releases_failed_background_job_without_hiding_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Continue after a failed supervised background command.")
            workflow_config = load_workflow_config(project)
            workflow_config["execution"]["continue_on_fail"] = True
            config_path = WorkflowPaths.from_config(project, workflow_config).config_file("workflow.json")
            config_path.write_text(json.dumps(workflow_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "raise SystemExit(3)"],
                task_id="P0.T001",
                run_id="run_background_continue_on_fail",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "cancelled")
            job = status["jobs"][0]
            self.assertTrue(job["next_prompt_ready"])
            self.assertEqual(job["exit_code"], 3)
            self.assertEqual(job["original_terminal_status"], "failed")
            self.assertTrue(job["auto_resolved_for_continue_on_fail"])
            self.assertIn("not acceptance", job["auto_resolution_reason"])

    def test_cli_background_start_returns_agent_status_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Expose supervised background jobs through the CLI.")

            started = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "background",
                    "start",
                    "--project",
                    str(project),
                    "--json",
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.1)",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
            payload = json.loads(started.stdout)
            self.assertEqual(payload["status"], "started")
            self.assertEqual(payload["agent_status_fragment"]["status"], "running_background")
            self.assertFalse(payload["agent_status_fragment"]["next_prompt_ready"])
            job_id = payload["job_id"]
            status = self._wait_for_cli_job_status(project, job_id, "completed")
            self.assertEqual(status["jobs"][0]["exit_code"], 0)

    def test_duplicate_job_id_does_not_overwrite_existing_launch_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Duplicate background jobs should not corrupt launch metadata.")

            first = start_background_job(
                project,
                job_id="stable_job",
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
            )
            self.assertTrue(first["ok"], json.dumps(first, indent=2, sort_keys=True))
            launch_path = project / first["launch_path"]
            before = json.loads(launch_path.read_text(encoding="utf-8"))
            try:
                duplicate = start_background_job(
                    project,
                    job_id="stable_job",
                    command=[sys.executable, "-c", "print('replacement command must not land')"],
                )

                self.assertFalse(duplicate["ok"], json.dumps(duplicate, indent=2, sort_keys=True))
                self.assertEqual(duplicate["status"], "duplicate_job_id")
                after = json.loads(launch_path.read_text(encoding="utf-8"))
                self.assertEqual(after["command"], before["command"])
            finally:
                cancel_background_job(project, "stable_job", reason="test cleanup")

    def test_shell_background_command_preserves_quoted_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Shell background command quoting.")
            marker = project / "shell marker.txt"
            script = f"from pathlib import Path; Path({str(marker)!r}).write_text('done', encoding='utf-8')"

            result = start_background_job(
                project,
                command=[sys.executable, "-c", script],
                shell=True,
                task_id="P0.T001",
                run_id="run_background_shell",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "completed")
            self.assertEqual(status["jobs"][0]["exit_code"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "done")

    def test_immediate_cancel_does_not_leave_child_process_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Immediate background cancel should stop the child process.")
            marker = project / "cancel_race_marker.txt"

            result = start_background_job(
                project,
                command=[
                    sys.executable,
                    "-c",
                    f"import time; time.sleep(1); open({str(marker)!r}, 'w', encoding='utf-8').write('late write')",
                ],
                task_id="P0.T001",
                run_id="run_background_cancel_race",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            cancelled = cancel_background_job(project, result["job_id"], reason="cancel race fixture")
            self.assertTrue(cancelled["ok"], json.dumps(cancelled, indent=2, sort_keys=True))
            time.sleep(1.5)
            status = list_background_jobs(project, job_id=result["job_id"])
            self.assertEqual(status["jobs"][0]["status"], "cancelled")
            self.assertFalse(marker.exists())

    def test_supervisor_does_not_start_precancelled_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Pre-cancelled background jobs must not be resurrected.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            job_id = "pre_cancelled_fixture"
            job_dir = paths.runtime_dir / "background_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            marker = project / "pre_cancelled_marker.txt"
            launch_path = job_dir / "launch.json"
            launch_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "project_root": project.as_posix(),
                        "workflow_id": workflow_id,
                        "job_id": job_id,
                        "command": [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).write_text('bad', encoding='utf-8')",
                        ],
                        "cwd": project.as_posix(),
                        "stdout_path": (job_dir / "stdout.log").as_posix(),
                        "stderr_path": (job_dir / "stderr.log").as_posix(),
                        "supervisor_log_path": (job_dir / "supervisor.log").as_posix(),
                        "exit_code_file": (job_dir / "exit_code.txt").as_posix(),
                        "heartbeat_seconds": 0.5,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (paths.runtime_dir / "background_jobs.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "workflow_id": workflow_id,
                        "jobs": [
                            {
                                "job_id": job_id,
                                "workflow_id": workflow_id,
                                "status": "cancelled",
                                "next_prompt_ready": True,
                                "started_at": "2026-06-19T00:00:00Z",
                                "heartbeat_at": "2026-06-19T00:00:00Z",
                                "launch_path": launch_path.relative_to(project).as_posix(),
                                "logs": [
                                    (job_dir / "stdout.log").relative_to(project).as_posix(),
                                    (job_dir / "stderr.log").relative_to(project).as_posix(),
                                    (job_dir / "supervisor.log").relative_to(project).as_posix(),
                                ],
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            exit_code = _run_supervisor(project, workflow_id=workflow_id, job_id=job_id, launch_path=launch_path)

            self.assertEqual(exit_code, 0)
            self.assertFalse(marker.exists())
            status = list_background_jobs(project, job_id=job_id)
            self.assertEqual(status["jobs"][0]["status"], "cancelled")
            self.assertTrue(status["jobs"][0]["next_prompt_ready"])

    def test_supervisor_marks_missing_launch_command_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed background launch records should become recoverable state.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            job_id = "missing_command_fixture"
            job_dir = paths.runtime_dir / "background_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            launch_path = job_dir / "launch.json"
            launch_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "project_root": project.as_posix(),
                        "workflow_id": workflow_id,
                        "job_id": job_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (paths.runtime_dir / "background_jobs.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "workflow_id": workflow_id,
                        "jobs": [
                            {
                                "job_id": job_id,
                                "workflow_id": workflow_id,
                                "status": "running",
                                "next_prompt_ready": False,
                                "started_at": "2026-06-19T00:00:00Z",
                                "heartbeat_at": "2026-06-19T00:00:00Z",
                                "launch_path": launch_path.relative_to(project).as_posix(),
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            exit_code = _run_supervisor(project, workflow_id=workflow_id, job_id=job_id, launch_path=launch_path)

            self.assertEqual(exit_code, 2)
            status = list_background_jobs(project, job_id=job_id)
            self.assertEqual(status["jobs"][0]["status"], "needs_recovery")
            self.assertEqual(status["jobs"][0]["status_problem"], "launch_command_missing")

    def test_empty_exit_code_file_does_not_force_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Empty exit-code files can appear during atomicity races.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            job_id = "empty_exit_code_fixture"
            job_dir = paths.runtime_dir / "background_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            exit_code_file = job_dir / "exit_code.txt"
            exit_code_file.write_text("", encoding="utf-8")
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": job_id,
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "exit_code_file": exit_code_file.relative_to(project).as_posix(),
                    }
                ],
            )

            runtime_status = list_background_jobs(project, job_id=job_id)
            scheduler_snapshot = load_scheduler_snapshot(project)

            self.assertEqual(runtime_status["jobs"][0]["status"], "running")
            self.assertEqual(scheduler_snapshot["background_jobs"][0]["status"], "running")

    def test_missing_supervisor_pid_has_startup_grace_before_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Supervisor pid handoff should not stale immediately.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            pid = self._unused_pid()
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "startup_grace_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "pid": pid,
                        "supervisor_pid": pid,
                    }
                ],
            )

            runtime_status = list_background_jobs(project, job_id="startup_grace_fixture")
            scheduler_snapshot = load_scheduler_snapshot(project)

            self.assertEqual(runtime_status["jobs"][0]["status"], "running")
            self.assertEqual(scheduler_snapshot["background_jobs"][0]["status"], "running")

    def test_watchdog_allowed_paths_exclude_background_registry_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Watchdog should not receive registry authority as a writable target.")
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)

            allowed = _watchdog_allowed_paths(project, paths, job_id="watchdog_fixture")

            self.assertIn(".loopplane/runtime/background_jobs/watchdog_fixture/", allowed)
            self.assertNotIn(".loopplane/runtime/background_jobs.json", allowed)

    def test_start_return_update_preserves_child_pid_written_by_supervisor(self) -> None:
        current = {
            "job_id": "pid_handoff_fixture",
            "status": "running",
            "pid": 456,
            "child_pid": 456,
            "supervisor_pid": 123,
            "heartbeat_at": "2026-06-19T00:00:01Z",
        }
        parent_update = {
            "job_id": "pid_handoff_fixture",
            "status": "running",
            "pid": 123,
            "supervisor_pid": 123,
            "heartbeat_at": "2026-06-19T00:00:00Z",
        }

        updated = _start_supervisor_record_update(current, parent_update)

        self.assertEqual(updated["pid"], 456)
        self.assertEqual(updated["child_pid"], 456)
        self.assertEqual(updated["supervisor_pid"], 123)

    def test_start_return_update_does_not_resurrect_terminal_job(self) -> None:
        current = {
            "job_id": "terminal_start_race_fixture",
            "status": "completed",
            "next_prompt_ready": True,
            "exit_code": 0,
        }
        parent_update = {
            "job_id": "terminal_start_race_fixture",
            "status": "running",
            "next_prompt_ready": False,
            "pid": 123,
            "supervisor_pid": 123,
        }

        updated = _start_supervisor_record_update(current, parent_update)

        self.assertEqual(updated["status"], "completed")
        self.assertTrue(updated["next_prompt_ready"])
        self.assertNotIn("pid", updated)

    def test_cancel_force_kills_recorded_process_that_ignores_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Cancelling a background job should not leave SIGTERM-resistant children.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            marker = project / "sigterm_ignored_started.txt"
            script = (
                "import signal, time; "
                "from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(marker)!r}).write_text('started', encoding='utf-8'); "
                "time.sleep(30)"
            )
            process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
            try:
                deadline = time.monotonic() + 5.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists())
                now = utc_timestamp()
                self._write_background_registry(
                    paths,
                    workflow_id,
                    [
                        {
                            "job_id": "sigterm_ignored_fixture",
                            "workflow_id": workflow_id,
                            "status": "running",
                            "next_prompt_ready": False,
                            "started_at": now,
                            "heartbeat_at": now,
                            "pid": process.pid,
                            "child_pid": process.pid,
                            "supervisor_host": socket.gethostname(),
                        }
                    ],
                )

                cancelled = cancel_background_job(project, "sigterm_ignored_fixture", reason="force kill fixture")

                self.assertTrue(cancelled["ok"], json.dumps(cancelled, indent=2, sort_keys=True))
                process.wait(timeout=3.0)
                self.assertIsNotNone(process.returncode)
                status = list_background_jobs(project, job_id="sigterm_ignored_fixture")
                self.assertEqual(status["jobs"][0]["status"], "cancelled")
                self.assertTrue(status["jobs"][0]["next_prompt_ready"])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3.0)

    def test_running_job_without_parseable_heartbeat_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed running background records should not stay silently running.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "missing_heartbeat_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                    }
                ],
            )

            status = list_background_jobs(project, job_id="missing_heartbeat_fixture")

            self.assertEqual(status["jobs"][0]["status"], "needs_recovery")
            self.assertEqual(status["jobs"][0]["status_problem"], "missing_parseable_heartbeat")

    def test_fresh_remote_heartbeat_does_not_use_local_pid_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Remote background supervisors use heartbeat evidence.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "remote_pid_namespace_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "pid": 999_999_999,
                        "supervisor_pid": 999_999_998,
                        "supervisor_host": f"remote-{socket.gethostname()}",
                    }
                ],
            )

            status = list_background_jobs(project, job_id="remote_pid_namespace_fixture")

            job = status["jobs"][0]
            self.assertEqual(job["status"], "running")
            self.assertFalse(job["next_prompt_ready"])
            self.assertNotIn("status_problem", job)

    def test_stale_remote_heartbeat_is_not_preserved_by_colliding_local_pids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Remote PID collisions must not prove background liveness.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "remote_stale_pid_collision_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "pid": os.getpid(),
                        "child_pid": os.getpid(),
                        "supervisor_pid": os.getpid(),
                        "supervisor_host": f"remote-{socket.gethostname()}",
                    }
                ],
            )

            status = list_background_jobs(project, job_id="remote_stale_pid_collision_fixture")

            job = status["jobs"][0]
            self.assertEqual(job["status"], "stale")
            self.assertEqual(job["status_problem"], "stale_heartbeat")
            self.assertFalse(job["next_prompt_ready"])

    def test_legacy_stale_record_cannot_use_unscoped_pid_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Legacy PID records lack a trustworthy host namespace.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "legacy_stale_pid_collision_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "pid": os.getpid(),
                        "child_pid": os.getpid(),
                        "supervisor_pid": os.getpid(),
                    }
                ],
            )

            status = list_background_jobs(project, job_id="legacy_stale_pid_collision_fixture")

            job = status["jobs"][0]
            self.assertEqual(job["status"], "stale")
            self.assertEqual(job["status_problem"], "stale_heartbeat")

    def test_manual_remote_resolution_never_signals_node_local_pids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Remote background resolution must respect PID namespaces.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "remote_resolution_fixture",
                        "workflow_id": workflow_id,
                        "status": "stale",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "pid": os.getpid(),
                        "child_pid": os.getpid(),
                        "supervisor_pid": os.getpid(),
                        "supervisor_host": f"remote-{socket.gethostname()}",
                    }
                ],
            )

            with mock.patch.object(background_jobs_runtime, "_terminate_recorded_pid") as terminate:
                result = complete_background_job(
                    project,
                    "remote_resolution_fixture",
                    status="needs_recovery",
                    reason="controller host exited",
                )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            terminate.assert_not_called()
            status = list_background_jobs(project, job_id="remote_resolution_fixture")
            self.assertEqual(status["jobs"][0]["status"], "needs_recovery")
            self.assertEqual(status["jobs"][0]["manual_reason"], "controller host exited")

    def test_fresh_local_heartbeat_still_detects_missing_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Local background supervisors retain PID liveness checks.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            now = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "local_missing_pid_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": now,
                        "heartbeat_at": now,
                        "pid": 999_999_999,
                        "supervisor_pid": 999_999_998,
                        "supervisor_host": socket.gethostname(),
                    }
                ],
            )

            status = list_background_jobs(project, job_id="local_missing_pid_fixture")

            job = status["jobs"][0]
            self.assertEqual(job["status"], "stale")
            self.assertEqual(job["status_problem"], "process_not_live")
            self.assertFalse(job["next_prompt_ready"])

    def test_live_synchronous_watchdog_covers_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "A live synchronous watchdog covers its heartbeat gap.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "live_watchdog_fixture",
                        "workflow_id": workflow_id,
                        "status": "running",
                        "next_prompt_ready": False,
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "pid": os.getpid(),
                        "supervisor_pid": os.getpid(),
                        "supervisor_host": socket.gethostname(),
                        "status_problem": "stale_heartbeat",
                        "watchdog": {
                            "enabled": True,
                            "current_check_id": "watchdog_live_fixture",
                            "current_check_started_at": "2000-01-01T00:00:00Z",
                        },
                    }
                ],
            )

            status = list_background_jobs(project, job_id="live_watchdog_fixture")

            job = status["jobs"][0]
            self.assertEqual(job["status"], "running")
            self.assertFalse(job["next_prompt_ready"])
            self.assertNotIn("status_problem", job)

    def test_status_refresh_reconciles_completed_source_agent_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Background status should honor completed source agent status.")
            workflow_config = load_workflow_config(project)
            workflow_id = str(workflow_config["workflow_id"])
            paths = WorkflowPaths.from_config(project, workflow_config)
            run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_bg_reconciled"
            run_dir.mkdir(parents=True, exist_ok=True)
            status_path = run_dir / "agent_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "run_id": "run_bg_reconciled",
                        "task_id": "P0.T001",
                        "status": "completed_with_warnings",
                        "background_state": {
                            "started_background_work": False,
                            "next_prompt_ready": True,
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": "bg_P0_T001_run_bg_reconciled",
                        "workflow_id": workflow_id,
                        "task_id": "P0.T001",
                        "run_id": "run_bg_reconciled",
                        "status": "stale",
                        "next_prompt_ready": False,
                        "source_agent_status_path": status_path.relative_to(project).as_posix(),
                    }
                ],
            )

            status = list_background_jobs(project, job_id="bg_P0_T001_run_bg_reconciled")

            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["jobs"][0]["status"], "completed")
            self.assertTrue(status["jobs"][0]["next_prompt_ready"])
            self.assertTrue(status["jobs"][0]["resolved_from_source_agent_status"])

    def test_manual_resolution_is_not_overwritten_by_supervisor_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Manual background resolution should stay authoritative.")

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                task_id="P0.T001",
                run_id="run_background_manual_resolution",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            job_id = result["job_id"]
            updated = complete_background_job(project, job_id, status="needs_recovery", reason="manual fixture")
            self.assertTrue(updated["ok"], json.dumps(updated, indent=2, sort_keys=True))

            status = self._wait_for_job_status(project, job_id, "needs_recovery")
            self.assertFalse(status["jobs"][0]["next_prompt_ready"])
            self.assertTrue(status["jobs"][0]["manual_resolution"])

    def test_supervisor_runs_watchdog_inspector_while_background_job_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Watchdog confirms healthy background progress.")
            configure_watchdog_inspector(project)

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(2)"],
                task_id="P0.T001",
                run_id="run_background_watchdog",
                watchdog_interval_seconds=1,
                watchdog_agent_interval_seconds=1,
                watchdog_question="confirm healthy fixture progress",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "completed")
            watchdog = status["jobs"][0]["watchdog"]
            self.assertGreaterEqual(watchdog["check_count"], 1)
            self.assertEqual(watchdog["last_recommended_status"], "running")
            self.assertTrue(watchdog["last_healthy_progress"])
            self.assertTrue(watchdog["recent_checks"])

    def test_watchdog_inspector_keeps_background_job_heartbeat_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Watchdog refreshes the background lease while inspecting.")
            configure_watchdog_inspector(project)
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)
            workflow_id = paths.workflow_id
            job_id = "bg_watchdog_heartbeat"
            started_at = utc_timestamp()
            self._write_background_registry(
                paths,
                workflow_id,
                [
                    {
                        "job_id": job_id,
                        "workflow_id": workflow_id,
                        "task_id": "P0.T001",
                        "run_id": "run_watchdog_heartbeat",
                        "status": "running",
                        "next_prompt_ready": False,
                        "started_at": started_at,
                        "heartbeat_at": started_at,
                        "watchdog": {
                            "enabled": True,
                            "interval_seconds": 1,
                            "runner_id": "inspector",
                            "status": "pending",
                            "check_count": 0,
                            "recent_checks": [],
                        },
                    }
                ],
            )
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
            result: dict[str, object] = {}

            def inspect() -> None:
                result.update(
                    _run_watchdog_check(
                        project,
                        paths,
                        workflow_id=workflow_id,
                        job_id=job_id,
                        launch={
                            "heartbeat_seconds": 0.5,
                            "watchdog_runner": "inspector",
                            "watchdog_question": "slow needs recovery fixture",
                        },
                        supervisor_log=paths.runtime_dir / "background_jobs" / job_id / "supervisor.log",
                        process=process,
                    )
                )

            thread = threading.Thread(target=inspect)
            thread.start()
            try:
                deadline = time.monotonic() + 3.0
                before = started_at
                while time.monotonic() < deadline:
                    status = list_background_jobs(project, job_id=job_id, refresh=False)
                    watchdog = status["jobs"][0].get("watchdog") or {}
                    if watchdog.get("current_check_id"):
                        before = str(status["jobs"][0]["heartbeat_at"])
                        break
                    time.sleep(0.05)
                time.sleep(1.1)
                during = list_background_jobs(project, job_id=job_id, refresh=False)["jobs"][0]
                self.assertGreater(str(during["heartbeat_at"]), before)
                self.assertTrue(thread.is_alive(), "fixture inspector should still be running during the heartbeat assertion")
            finally:
                thread.join(timeout=5.0)
                process.terminate()
                process.wait(timeout=5.0)

            self.assertFalse(thread.is_alive())
            self.assertTrue(result.get("stop_job"))

    def test_short_background_job_uses_cheap_probe_without_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Short jobs should not spend an agent call on watchdog inspection.")
            configure_watchdog_inspector(project)

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(1.5)"],
                task_id="P0.T001",
                run_id="run_background_probe_only",
                watchdog_interval_seconds=1,
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "completed")
            watchdog = status["jobs"][0]["watchdog"]
            self.assertGreaterEqual(watchdog["probe_count"], 1)
            self.assertEqual(watchdog["check_count"], 0)
            self.assertEqual(watchdog["agent_interval_seconds"], 7200)

    def test_watchdog_can_stop_unhealthy_background_job_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Watchdog stops unhealthy background progress.")
            workflow_config = load_workflow_config(project)
            workflow_config["execution"]["continue_on_fail"] = False
            config_path = WorkflowPaths.from_config(project, workflow_config).config_file("workflow.json")
            config_path.write_text(json.dumps(workflow_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            configure_watchdog_inspector(project)

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                task_id="P0.T001",
                run_id="run_background_watchdog_unhealthy",
                watchdog_interval_seconds=1,
                watchdog_agent_interval_seconds=1,
                watchdog_question="needs recovery fixture",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "needs_recovery")
            job = status["jobs"][0]
            self.assertFalse(job["next_prompt_ready"])
            self.assertIn("stalled fixture", job["status_problem"])
            self.assertEqual(job["watchdog"]["status"], "requires_attention")
            self.assertEqual(job["watchdog"]["last_recommended_status"], "needs_recovery")

    def test_watchdog_result_does_not_override_job_that_finished_during_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Watchdog should not override completed jobs.")
            configure_watchdog_inspector(project)

            result = start_background_job(
                project,
                command=[sys.executable, "-c", "import time; time.sleep(1.2)"],
                task_id="P0.T001",
                run_id="run_background_watchdog_race",
                watchdog_interval_seconds=1,
                watchdog_agent_interval_seconds=1,
                watchdog_question="slow needs recovery fixture",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            status = self._wait_for_job_status(project, result["job_id"], "completed")
            self.assertEqual(status["jobs"][0]["status"], "completed")
            self.assertTrue(status["jobs"][0]["next_prompt_ready"])

    def _wait_for_job_status(self, project: Path, job_id: str, expected: str) -> dict[str, object]:
        deadline = time.monotonic() + 10.0
        latest: dict[str, object] | None = None
        while time.monotonic() < deadline:
            latest = list_background_jobs(project, job_id=job_id)
            jobs = latest.get("jobs")
            if isinstance(jobs, list) and jobs:
                status = str(jobs[0].get("status") or "")
                if status == expected:
                    return latest
            time.sleep(0.1)
        self.fail(f"background job {job_id} did not reach {expected}; latest={latest}")

    def _wait_for_cli_job_status(self, project: Path, job_id: str, expected: str) -> dict[str, object]:
        deadline = time.monotonic() + 10.0
        latest: dict[str, object] | None = None
        while time.monotonic() < deadline:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "background",
                    "status",
                    "--project",
                    str(project),
                    "--job",
                    job_id,
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            latest = json.loads(completed.stdout)
            jobs = latest.get("jobs")
            if isinstance(jobs, list) and jobs:
                status = str(jobs[0].get("status") or "")
                if status == expected:
                    return latest
            time.sleep(0.1)
        self.fail(f"CLI background job {job_id} did not reach {expected}; latest={latest}")

    def _write_background_registry(self, paths: WorkflowPaths, workflow_id: str, jobs: list[dict[str, object]]) -> None:
        (paths.runtime_dir / "background_jobs.json").write_text(
            json.dumps({"schema_version": "1.0", "workflow_id": workflow_id, "jobs": jobs}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _unused_pid(self) -> int:
        for pid in range(4_000_000, 3_999_000, -1):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return pid
            except PermissionError:
                continue
            except OSError:
                continue
        return 999_999


if __name__ == "__main__":
    unittest.main()
