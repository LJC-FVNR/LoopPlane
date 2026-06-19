from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from runtime.control import load_control_status
from runtime.final_verifier import run_final_verifier
from runtime.init_workflow import init_project
from runtime.scheduler import run_scheduler
from runtime.workflow_lifecycle import (
    WorkflowLifecycleError,
    archive_workflow,
    create_workflow_record,
    ensure_compatibility_workflow_metadata,
    fork_workflow,
    import_workflow_record,
    restore_workflow,
    supersede_workflow,
)
from tests.test_final_verifier import write_completed_task_evidence, write_final_plan


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry_record(project: Path, workflow_id: str) -> dict:
    registry = read_json(project / ".loopplane" / "workflow_registry.json")
    for record in registry["workflows"]:
        if record["workflow_id"] == workflow_id:
            return record
    raise AssertionError(f"missing registry record {workflow_id}")


class WorkflowLifecycleTest(unittest.TestCase):
    def test_existing_v15_flat_instance_is_represented_as_single_workflow_without_moving_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Compatibility flat workflow fixture.")
            workflow_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            runtime_state = read_json(project / ".loopplane" / "runtime" / "state.json")
            runtime_state["status"] = "waiting_config"
            write_json(project / ".loopplane" / "runtime" / "state.json", runtime_state)
            workflow_status = {
                "schema_version": "1.5",
                "workflow_id": workflow_id,
                "status": "waiting_config",
                "summary": "Legacy flat summary.",
                "progress": {
                    "total_tasks": 4,
                    "completed_tasks": 2,
                    "blocked_tasks": 1,
                },
            }
            write_json(project / ".loopplane" / "read_models" / "workflow_status.json", workflow_status)
            (project / ".loopplane" / "requests" / "chat_requests.jsonl").write_text(
                '{"request_id":"legacy"}\n',
                encoding="utf-8",
            )
            legacy_result = project / ".loopplane" / "results" / "T001" / "latest.json"
            legacy_result.parent.mkdir(parents=True, exist_ok=True)
            write_json(legacy_result, {"legacy": True})
            planning_file = project / ".loopplane" / "planning" / "PLAN_DRAFT.md"
            planning_file.write_text("legacy draft\n", encoding="utf-8")
            preserved_files = [
                project / "PROJECT_BRIEF.md",
                project / "PLAN.md",
                project / ".loopplane" / "SHARED_CONTEXT.md",
                project / ".loopplane" / "config" / "workflow.json",
                project / ".loopplane" / "runtime" / "state.json",
                project / ".loopplane" / "read_models" / "workflow_status.json",
                project / ".loopplane" / "requests" / "chat_requests.jsonl",
                legacy_result,
                planning_file,
            ]
            before_hashes = {path: file_sha256(path) for path in preserved_files}
            for relative in ("workspace.json", "workflow_registry.json", "current_workflow.json"):
                (project / ".loopplane" / relative).unlink()

            result = ensure_compatibility_workflow_metadata(project, updated_by="test")
            status = load_control_status(project)

            self.assertEqual(result["status"], "created")
            self.assertEqual(
                result["created"],
                [
                    ".loopplane/current_workflow.json",
                    ".loopplane/workflow_registry.json",
                    ".loopplane/workspace.json",
                ],
            )
            registry = read_json(project / ".loopplane" / "workflow_registry.json")
            self.assertEqual(len(registry["workflows"]), 1)
            record = registry["workflows"][0]
            self.assertEqual(record["workflow_id"], workflow_id)
            self.assertEqual(record["workflow_root"], ".loopplane/")
            self.assertEqual(record["plan_file"], "PLAN.md")
            self.assertEqual(record["runtime_dir"], ".loopplane/runtime")
            self.assertEqual(record["read_models_dir"], ".loopplane/read_models")
            self.assertEqual(record["requests_dir"], ".loopplane/requests")
            self.assertEqual(record["completion_marker"], ".loopplane/runtime/plan_loop_complete.json")
            self.assertEqual(record["status"], "draft")
            self.assertEqual(record["summary"]["one_line"], "Legacy flat summary.")
            self.assertEqual(record["summary"]["tasks_total"], 4)
            self.assertEqual(record["summary"]["tasks_completed"], 2)
            self.assertEqual(record["summary"]["tasks_blocked"], 1)
            current = read_json(project / ".loopplane" / "current_workflow.json")
            self.assertEqual(current["current_workflow_id"], workflow_id)
            self.assertEqual(read_json(project / ".loopplane" / "workspace.json")["current_workflow_id"], workflow_id)
            self.assertEqual(status["workflow_id"], workflow_id)
            self.assertEqual(status["runtime_state"]["status"], "waiting_config")
            self.assertEqual(before_hashes, {path: file_sha256(path) for path in preserved_files})

    def test_final_verifier_marks_current_registry_record_completed_without_pointer_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Completion lifecycle registry fixture.")
            workflow_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            pointer_before = read_json(project / ".loopplane" / "current_workflow.json")
            write_final_plan(project)
            write_completed_task_evidence(project)

            result = run_final_verifier(project, owner="test_final_verifier")

            self.assertEqual(result["status"], "pass", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["workflow_registry_update"]["status"], "workflow_completed")
            record = registry_record(project, workflow_id)
            self.assertEqual(record["status"], "completed")
            self.assertFalse(record["archived"])
            self.assertFalse(record["read_only"])
            self.assertEqual(record["completion_marker"], ".loopplane/runtime/plan_loop_complete.json")
            self.assertEqual(record["final_verification_report"], ".loopplane/runtime/final_verification_report.json")
            self.assertEqual(record["summary"]["tasks_total"], 2)
            self.assertEqual(record["summary"]["tasks_completed"], 1)
            self.assertEqual(record["summary"]["tasks_blocked"], 0)
            self.assertEqual(record["summary"]["tasks_skipped"], 1)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json"), pointer_before)

    def test_scheduler_complete_refreshes_registry_when_completion_marker_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Scheduler completion lifecycle registry fixture.")
            workflow_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            write_final_plan(project)
            write_completed_task_evidence(project)
            self.assertEqual(run_final_verifier(project, owner="test")["status"], "pass")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = read_json(registry_path)
            registry["workflows"][0]["status"] = "draft"
            registry["workflows"][0]["summary"]["tasks_total"] = 0
            write_json(registry_path, registry)

            result = run_scheduler(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["selected_action"]["action"], "complete")
            record = registry_record(project, workflow_id)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["summary"]["tasks_total"], 2)
            self.assertEqual(record["completion_marker"], ".loopplane/runtime/plan_loop_complete.json")

    def test_scheduler_complete_is_read_only_when_registry_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Scheduler completion idempotence fixture.")
            workflow_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            write_final_plan(project)
            write_completed_task_evidence(project)
            self.assertEqual(run_final_verifier(project, owner="test")["status"], "pass")

            first = run_scheduler(project)
            self.assertTrue(first["ok"], json.dumps(first, indent=2, sort_keys=True))
            record_before = registry_record(project, workflow_id)
            watched = {
                key: record_before.get(key)
                for key in ("completed_at", "completed_by", "last_seen_at", "completion_marker", "status")
            }
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = read_json(state_path)
            scheduler = state.setdefault("scheduler", {})
            scheduler["running"] = True
            write_json(state_path, state)

            second = run_scheduler(project, max_ticks=2)

            self.assertTrue(second["ok"], json.dumps(second, indent=2, sort_keys=True))
            self.assertEqual(second["ticks_run"], 0)
            self.assertEqual(second["selected_action"]["action"], "complete")
            self.assertFalse(second["selected_action"]["execution_result"]["workflow_registry_update"]["mutated"])
            record_after = registry_record(project, workflow_id)
            self.assertEqual(
                {key: record_after.get(key) for key in watched},
                watched,
            )
            self.assertFalse(read_json(state_path)["scheduler"]["running"])

    def test_lifecycle_transitions_preserve_history_and_update_current_only_when_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Lifecycle transition registry fixture.")
            original_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            second_id = "wf_20260611_aaaaaaaa"
            imported_id = "wf_20260611_bbbbbbbb"

            create = create_workflow_record(
                project,
                workflow_id=second_id,
                name="second attempt",
                workflow_root=f".loopplane/workflows/{second_id}",
                make_current=True,
                updated_by="test",
            )
            self.assertEqual(create["status"], "workflow_created")
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], second_id)
            self.assertEqual(read_json(project / ".loopplane" / "workspace.json")["current_workflow_id"], second_id)

            archive_workflow(project, original_id, reason="keep for dashboard", updated_by="test")
            archived = registry_record(project, original_id)
            self.assertEqual(archived["status"], "archived")
            self.assertTrue(archived["archived"])
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], second_id)

            restore_workflow(project, original_id, make_current=False, updated_by="test")
            restored = registry_record(project, original_id)
            self.assertEqual(restored["status"], "active")
            self.assertFalse(restored["archived"])
            self.assertFalse(restored["read_only"])
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], second_id)

            archive_workflow(project, original_id, reason="restore with current pointer", updated_by="test")
            restore_workflow(project, original_id, make_current=True, updated_by="test")
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], original_id)

            supersede_workflow(project, original_id, superseded_by=second_id, updated_by="test")
            superseded = registry_record(project, original_id)
            self.assertEqual(superseded["status"], "superseded")
            self.assertEqual(superseded["superseded_by"], second_id)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], original_id)

            imported = import_workflow_record(
                project,
                workflow_id=imported_id,
                name="imported history",
                workflow_root=f".loopplane/imported/{imported_id}",
                updated_by="test",
            )
            self.assertEqual(imported["status"], "workflow_imported")
            imported_record = registry_record(project, imported_id)
            self.assertEqual(imported_record["status"], "read_only_imported")
            self.assertTrue(imported_record["read_only"])
            self.assertFalse(imported_record["archived"])

            with self.assertRaises(WorkflowLifecycleError):
                restore_workflow(project, imported_id, updated_by="test")
            with self.assertRaises(WorkflowLifecycleError):
                restore_workflow(project, second_id, updated_by="test")

            registry = read_json(project / ".loopplane" / "workflow_registry.json")
            self.assertEqual(
                [record["workflow_id"] for record in registry["workflows"]],
                [original_id, second_id, imported_id],
            )

    def test_fork_adds_new_history_and_can_explicitly_select_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Fork lifecycle registry fixture.")
            source_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            fork_id = "wf_20260611_cccccccc"

            result = fork_workflow(
                project,
                source_id,
                new_workflow_id=fork_id,
                name="forked retry",
                make_current=True,
                updated_by="test",
            )

            self.assertEqual(result["status"], "workflow_forked")
            forked = registry_record(project, fork_id)
            self.assertEqual(forked["status"], "forked")
            self.assertEqual(forked["forked_from"], source_id)
            self.assertEqual(forked["workflow_root"], f".loopplane/workflows/{fork_id}")
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], fork_id)
            self.assertEqual(registry_record(project, source_id)["status"], "draft")

    def test_active_running_policy_is_enforced_by_lifecycle_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Active-running lifecycle policy fixture.")
            original_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            archive_workflow(project, original_id, updated_by="test")
            restore_workflow(project, original_id, make_current=True, updated_by="test")

            with self.assertRaises(WorkflowLifecycleError) as raised:
                create_workflow_record(
                    project,
                    workflow_id="wf_20260611_dddddddd",
                    name="conflicting active workflow",
                    workflow_root=".loopplane/workflows/wf_20260611_dddddddd",
                    status="running",
                    updated_by="test",
                )

            self.assertIn("one active-running workflow per workspace", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
