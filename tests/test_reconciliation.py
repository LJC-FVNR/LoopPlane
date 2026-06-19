from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import init_project
from runtime.reconciliation import run_reconciler
from runtime.validation import run_validator
from tests.test_human_summaries import configure_fake_summary_agent
from tests.test_validation import (
    write_absorption_plan,
    write_absorption_worker_run,
    write_plan,
    write_worker_run,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ReconcilerTest(unittest.TestCase):
    def test_cli_reconcile_pass_marks_plan_and_writes_latest_from_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reconcile passing validation.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)
            run_validator(project, task_id="T001", run_dir=run_dir)
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active_plan_sha256"] = "sha256:previous-plan"
            state["manual_plan_change"] = {
                "accepted_plan_sha256": "sha256:previous-plan",
                "current_plan_sha256": "sha256:manual-edit",
                "reconciliation_required": True,
            }
            state["configuration_problems"] = [
                {"code": "manual_plan_change_detected", "message": "Manual plan edit pending acknowledgement."}
            ]
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "reconcile",
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
            self.assertEqual(payload["status"], "reconciled")
            self.assertEqual(payload["accepted_task_ids"], ["T001"])
            self.assertIn("- [x] T001: Produce result artifact", (project / "PLAN.md").read_text(encoding="utf-8"))
            latest = json.loads((project / ".loopplane" / "results" / "T001" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["task_id"], "T001")
            self.assertEqual(latest["latest_run_id"], run_dir.name)
            self.assertEqual(latest["latest_run_dir"], ".loopplane/results/T001/runs/run_fixture")
            self.assertEqual(latest["validation_path"], ".loopplane/results/T001/runs/run_fixture/validation.json")
            self.assertEqual(latest["updated_by"], "reconciler")
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["failures"], [])
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertIn("validation_passed", [event["event_type"] for event in events])
            self.assertIn("plan_updated", [event["event_type"] for event in events])
            self.assertIn("read_model_rebuild_requested", [event["event_type"] for event in events])
            self.assertTrue((project / ".loopplane" / "runtime" / "read_model_rebuild_request.json").is_file())
            state_after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("manual_plan_change", state_after)
            self.assertEqual(state_after["active_plan_sha256"], "sha256:previous-plan")

    def test_reconciler_refuses_to_complete_without_validation_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No validation no completion.")
            write_plan(project)
            run_dir = write_worker_run(project, create_artifact=True)

            result = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "invalid_validation")
            self.assertIn("- [ ] T001: Produce result artifact", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertFalse((project / ".loopplane" / "results" / "T001" / "latest.json").exists())

    def test_reconcile_failed_validation_updates_failure_registry_without_advancing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reconcile failing validation.")
            write_plan(project)
            run_dir = write_worker_run(project)
            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "reconcile",
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
            result = json.loads(completed.stdout)

            self.assertEqual(validation["status"], "fail")
            self.assertEqual(completed.returncode, 4, completed.stderr + completed.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "validation_failed")
            self.assertIn("- [ ] T001: Produce result artifact", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertFalse((project / ".loopplane" / "results" / "T001" / "latest.json").exists())
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(len(registry["failures"]), 1)
            failure = registry["failures"][0]
            self.assertEqual(failure["task_id"], "T001")
            self.assertEqual(failure["failure_class"], "validation_failed")
            self.assertEqual(failure["status"], "unrecovered")
            self.assertTrue(failure["budget_remaining"])
            self.assertEqual(failure["source_validation_path"], ".loopplane/results/T001/runs/run_fixture/validation.json")
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "recovery_pending")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertIn("validation_failed", [event["event_type"] for event in events])
            self.assertIn("failure_registry_updated", [event["event_type"] for event in events])

    def test_reconcile_human_approval_strategy_auto_authorizes_without_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reconcile human validation.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="human_approval: release manager approval required")
            run_dir = write_worker_run(project, create_artifact=True)
            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            result = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass_with_warnings")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "reconciled")
            self.assertIn("- [x] T001: Produce result artifact", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertTrue((project / ".loopplane" / "results" / "T001" / "latest.json").exists())
            approvals = read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl")
            self.assertEqual(approvals, [])
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "reconciled")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertIn("validation_passed", event_types)
            self.assertIn("plan_updated", event_types)

    def test_reconcile_absorbed_tasks_updates_only_accepted_latest_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reconcile absorbed tasks.")
            configure_fake_summary_agent(project)
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T001", "T002"})
            run_validator(project, task_id="T001", run_dir=run_dir)

            result = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertTrue(result["ok"])
            self.assertEqual(result["accepted_task_ids"], ["T001", "T002"])
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: T001 task", plan_text)
            self.assertIn("- [x] T002: T002 task", plan_text)
            t001_latest = json.loads((project / ".loopplane" / "results" / "T001" / "latest.json").read_text(encoding="utf-8"))
            t002_latest = json.loads((project / ".loopplane" / "results" / "T002" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(t001_latest["latest_run_dir"], ".loopplane/results/T001/runs/run_absorption")
            self.assertEqual(t002_latest["latest_run_dir"], ".loopplane/results/T001/runs/run_absorption")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertIn("task_absorbed", event_types)
            self.assertEqual(event_types.count("plan_updated"), 2)
            node_summary = json.loads((run_dir / "node_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(node_summary["multi_task_absorption"]["accepted_task_ids"], ["T002"])

    def test_rejected_absorption_candidate_is_not_marked_or_given_latest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reconcile rejected absorption.")
            configure_fake_summary_agent(project)
            write_absorption_plan(
                project,
                [
                    {"task_id": "T001"},
                    {"task_id": "T002", "depends_on": ["T001"]},
                ],
            )
            run_dir = write_absorption_worker_run(project, candidate_ids=["T002"], artifact_task_ids={"T001"})
            validation = run_validator(project, task_id="T001", run_dir=run_dir)

            result = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["accepted_task_ids"], ["T001"])
            self.assertEqual(validation["rejected_task_ids"], ["T002"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["accepted_task_ids"], ["T001"])
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] T001: T001 task", plan_text)
            self.assertIn("- [ ] T002: T002 task", plan_text)
            self.assertTrue((project / ".loopplane" / "results" / "T001" / "latest.json").is_file())
            self.assertFalse((project / ".loopplane" / "results" / "T002" / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
