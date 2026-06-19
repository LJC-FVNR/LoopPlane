from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.dashboard import render_static_dashboard
from runtime.control import load_control_status
from runtime.init_workflow import init_project
from runtime.read_models import rebuild_read_models
from runtime.scheduler import load_scheduler_snapshot, run_scheduler, select_next_action
from tests.test_scheduler import append_jsonl, read_jsonl, write_active_plan


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def run_loopplane(*args: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(LoopPlane), *args, "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr + completed.stdout)
    return json.loads(completed.stdout)


class ControlRequestCliTest(unittest.TestCase):
    def test_status_matches_response_for_control_request_without_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Control status handles malformed request IDs.")
            append_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl", {"action": "pause"})

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            status = load_control_status(project)
            self.assertEqual(status["pending_count"], 0)
            self.assertEqual(status["applied_count"], 1)
            self.assertTrue(status["controls"][0]["synthetic_request_id"])
            self.assertEqual(status["controls"][0]["status"], "applied")
            self.assertEqual(
                status["controls"][0]["request_id"],
                result["selected_action"]["execution_result"]["request_id"],
            )

    def test_completed_status_reports_scheduler_not_running_even_with_stale_state_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Completed status should not expose stale scheduler running flag.")
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "completed"
            state["scheduler"]["running"] = True
            state["scheduler"]["active_run_id"] = "run_stale"
            state["scheduler"]["active_task_id"] = "T001"
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with patch(
                "runtime.control._load_completion_marker_status",
                return_value={"exists": True, "fresh": True, "stale_reasons": []},
            ):
                status = load_control_status(project)

            self.assertEqual(status["status"], "completed")
            self.assertFalse(status["scheduler"]["running"])
            self.assertIsNone(status["scheduler"]["active_run_id"])
            self.assertIsNone(status["runtime_state"]["scheduler"]["active_task_id"])

    def test_status_reports_stale_completion_marker_without_completed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Status should expose stale completion markers.")
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "completed"
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            marker_path = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            marker_path.write_text(
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

            status = run_loopplane("status", "--project", str(project))

            self.assertEqual(status["status"], "completion_marker_stale")
            self.assertEqual(status["runtime_status"], "completed")
            self.assertFalse(status["completion_marker"]["fresh"])
            self.assertIn("plan_sha256_mismatch", status["completion_marker"]["stale_reasons"])
            self.assertTrue(status["warnings"])

            text = subprocess.run(
                [sys.executable, str(LoopPlane), "status", "--project", str(project)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("loopplane status: completion_marker_stale", text.stdout)
            self.assertIn("runtime_status: completed", text.stdout)
            self.assertIn("completion_marker: stale", text.stdout)

    def test_pause_resume_stop_smoke_via_control_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Control pause resume stop.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            workflow_id = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))["workflow_id"]

            def registry_status() -> str:
                registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
                return next(record["status"] for record in registry["workflows"] if record["workflow_id"] == workflow_id)

            pause = run_loopplane("pause", "--project", str(project))
            self.assertEqual(pause["request"]["type"], "pause")
            pause_result = run_scheduler(project, max_ticks=1)
            self.assertEqual(pause_result["selected_action"]["action"], "handle_control_request")
            pause_response = pause_result["selected_action"]["execution_result"]
            self.assertEqual(pause_response["status"], "applied")
            self.assertEqual(pause_response["resulting_workflow_status"], "paused")
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "paused")
            self.assertTrue(state["scheduler"]["paused"])
            self.assertEqual(registry_status(), "paused")

            paused_action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(paused_action["action"], "wait_paused")
            self.assertTrue(paused_action["would_wait"])

            status = run_loopplane("status", "--project", str(project))
            self.assertEqual(status["status"], "paused")
            self.assertEqual(status["pending_count"], 0)
            self.assertEqual(status["applied_count"], 1)

            resume = run_loopplane("resume", "--project", str(project))
            self.assertEqual(resume["request"]["type"], "resume")
            resume_result = run_scheduler(project, max_ticks=1)
            resume_response = resume_result["selected_action"]["execution_result"]
            self.assertEqual(resume_response["status"], "applied")
            self.assertEqual(resume_response["resulting_workflow_status"], "running")
            resumed_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_state["status"], "running")
            self.assertFalse(resumed_state["scheduler"]["paused"])
            self.assertEqual(registry_status(), "running")
            runnable_action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(runnable_action["action"], "run_worker")

            stop = run_loopplane("stop", "--project", str(project))
            self.assertEqual(stop["request"]["type"], "stop")
            stop_result = run_scheduler(project, max_ticks=1)
            stop_response = stop_result["selected_action"]["execution_result"]
            self.assertEqual(stop_response["status"], "applied")
            self.assertEqual(stop_response["resulting_workflow_status"], "stopped")
            stopped_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(stopped_state["status"], "stopped")
            self.assertTrue(stopped_state["scheduler"]["stop_requested"])
            self.assertEqual(registry_status(), "stopped")
            stopped_action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(stopped_action["action"], "wait_stopped")

            requests = read_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl")
            responses = read_jsonl(project / ".loopplane" / "runtime" / "control_responses.jsonl")
            self.assertEqual([record["type"] for record in requests], ["pause", "resume", "stop"])
            self.assertEqual([record["status"] for record in responses], ["applied", "applied", "applied"])

    def test_start_attach_logs_migrate_and_dashboard_control_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Control command surface.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})

            start = run_loopplane("start", "--project", str(project))
            self.assertEqual(start["request"]["type"], "start")
            self.assertEqual(start["request"]["payload"]["detach"], False)
            start_result = run_scheduler(project, max_ticks=1)
            self.assertEqual(start_result["selected_action"]["execution_result"]["resulting_workflow_status"], "running")

            attach = run_loopplane("attach", "--request", "--project", str(project))
            self.assertEqual(attach["request"]["type"], "attach")
            attach_result = run_scheduler(project, max_ticks=1)
            self.assertEqual(attach_result["selected_action"]["execution_result"]["status"], "applied")

            migrate = run_loopplane("migrate", "--project", str(project))
            self.assertEqual(migrate["status"], "no_op")
            self.assertEqual(migrate["modified_files"], [])

            logs = run_loopplane("logs", "--project", str(project), "--lines", "20")
            self.assertTrue(logs["events"])
            self.assertEqual([record["type"] for record in logs["control_requests"]], ["start", "attach"])
            self.assertEqual([record["status"] for record in logs["control_responses"]], ["applied", "applied"])

            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            workflow_status = json.loads((project / ".loopplane" / "read_models" / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["control"]["applied_count"], 2)
            self.assertEqual(workflow_status["control"]["latest_type"], "attach")

            dashboard = render_static_dashboard(project)
            self.assertTrue(dashboard["ok"], json.dumps(dashboard, indent=2, sort_keys=True))
            self.assertIn("control_requests", dashboard["covered_sections"])
            html = (project / dashboard["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Execution Controls", html)
            self.assertIn("loopplane pause --project", html)


if __name__ == "__main__":
    unittest.main()
