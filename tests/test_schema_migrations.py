from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.exit_codes import EXIT_MIGRATION_REQUIRED, EXIT_SUCCESS
from runtime.init_workflow import InitConflictError, init_project
from runtime.migrations import migrate_project
from runtime.schema_validation import (
    SCHEMA_VERSION,
    SCHEMA_DIR,
    WORKFLOW_HISTORY_STATUSES,
    available_schema_files,
    validate_project_schemas,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def run_loopplane(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def workflow_history_record(template: dict[str, object], workflow_id: str, status: str) -> dict[str, object]:
    record = dict(template)
    record["workflow_id"] = workflow_id
    record["name"] = f"{status} workflow history"
    record["status"] = status
    record["workflow_root"] = f".loopplane/workflows/{workflow_id}"
    record["plan_file"] = f".loopplane/workflows/{workflow_id}/PLAN.md"
    record["read_models_dir"] = f".loopplane/workflows/{workflow_id}/read_models"
    record["runtime_dir"] = f".loopplane/workflows/{workflow_id}/runtime"
    record["requests_dir"] = f".loopplane/workflows/{workflow_id}/requests"
    record["completion_marker"] = f".loopplane/workflows/{workflow_id}/runtime/plan_loop_complete.json"
    record["archived"] = status == "archived"
    record["read_only"] = status == "read_only_imported"
    record["summary"] = {
        "one_line": f"{status} workflow history fixture.",
        "tasks_total": 1,
        "tasks_completed": int(status in {"completed", "archived", "superseded"}),
        "tasks_blocked": 0,
    }
    return record


def mark_legacy_schema(project: Path, version: str = "1.4") -> None:
    for relative in (
        ".loopplane/config/schema_version.json",
        ".loopplane/config/workflow.json",
        ".loopplane/runtime/state.json",
        ".loopplane/read_models/workflow_status.json",
    ):
        path = project / relative
        data = read_json(path)
        data["schema_version"] = version
        if path.name == "schema_version.json":
            data["required_runtime_version"] = f">={version}.0"
            data["files"] = {str(key): version for key in data.get("files", {})}
        write_json(path, data)


class SchemaValidationTest(unittest.TestCase):
    def test_workflow_registry_status_enum_matches_v16_spec(self) -> None:
        schema = read_json(SCHEMA_DIR / "workflow_registry.schema.json")
        enum = schema["properties"]["workflows"]["items"]["properties"]["status"]["enum"]

        self.assertEqual(sorted(enum), sorted(WORKFLOW_HISTORY_STATUSES))
        self.assertEqual(
            set(enum),
            {
                "draft",
                "ready",
                "active",
                "running",
                "paused",
                "stopped",
                "objective_unresolved",
                "completed",
                "failed",
                "archived",
                "read_only_imported",
                "forked",
                "superseded",
            },
        )

    def test_fresh_project_validates_against_registered_schema_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Schema validation fixture.")

            result = validate_project_schemas(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "pass")
            self.assertGreaterEqual(len(result["checked_files"]), 10)
            self.assertGreaterEqual(len(available_schema_files()), 10)
            self.assertIn(".loopplane/workspace.json", result["checked_files"])
            self.assertIn(".loopplane/workflow_registry.json", result["checked_files"])
            self.assertIn(".loopplane/current_workflow.json", result["checked_files"])
            self.assertIn(".loopplane/config/workflow.json", result["checked_files"])
            self.assertIn("workspace.schema.json", result["schemas_used"])
            self.assertIn("workflow_registry.schema.json", result["schemas_used"])
            self.assertIn("current_workflow.schema.json", result["schemas_used"])
            self.assertIn("workflow.schema.json", result["schemas_used"])

    def test_legacy_flat_project_without_workspace_identity_materializes_compatibility_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Legacy flat validation fixture.")
            workflow_id = read_json(project / ".loopplane" / "config" / "workflow.json")["workflow_id"]
            (project / ".loopplane" / "workspace.json").unlink()
            (project / ".loopplane" / "workflow_registry.json").unlink()
            (project / ".loopplane" / "current_workflow.json").unlink()

            result = validate_project_schemas(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn(".loopplane/workspace.json", result["checked_files"])
            self.assertIn(".loopplane/workflow_registry.json", result["checked_files"])
            self.assertIn(".loopplane/current_workflow.json", result["checked_files"])
            self.assertIn("workspace.schema.json", result["schemas_used"])
            self.assertIn("workflow_registry.schema.json", result["schemas_used"])
            self.assertIn("current_workflow.schema.json", result["schemas_used"])
            self.assertIn("created v1.6 compatibility metadata files", "\n".join(result["warnings"]))
            registry = read_json(project / ".loopplane" / "workflow_registry.json")
            self.assertEqual(registry["workflows"][0]["workflow_id"], workflow_id)
            self.assertEqual(registry["workflows"][0]["workflow_root"], ".loopplane/")
            current = read_json(project / ".loopplane" / "current_workflow.json")
            self.assertEqual(current["current_workflow_id"], workflow_id)

    def test_existing_workspace_identity_missing_boundary_fields_defaults_for_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Boundary defaults compatibility fixture.")
            workspace_path = project / ".loopplane" / "workspace.json"
            workspace = read_json(workspace_path)
            workspace.pop("workspace_boundary")
            workspace.pop("allow_out_of_boundary_writes")
            write_json(workspace_path, workspace)

            result = validate_project_schemas(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("defaulted missing workspace boundary config fields", "\n".join(result["warnings"]))
            updated = read_json(workspace_path)
            self.assertEqual(updated["workspace_boundary"], "project_root")
            self.assertFalse(updated["allow_out_of_boundary_writes"])
            self.assertEqual(updated["boundary_defaults_added_by"], "loopplane schema-validation")

    def test_workspace_id_must_be_distinct_from_workflow_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workspace identity validation fixture.")
            workflow = read_json(project / ".loopplane" / "config" / "workflow.json")
            workspace_path = project / ".loopplane" / "workspace.json"
            workspace = read_json(workspace_path)
            workspace["workspace_id"] = workflow["workflow_id"]
            write_json(workspace_path, workspace)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("workspace_id must be distinct from workflow_id", "\n".join(result["errors"]))

    def test_workflow_registry_schema_and_workspace_link_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workflow registry validation fixture.")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = read_json(registry_path)
            registry["workspace_id"] = "ws_mismatched_registry"
            registry["workflows"][0]["status"] = "initialized"
            write_json(registry_path, registry)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            errors = "\n".join(result["errors"])
            self.assertIn("workspace_id 'ws_mismatched_registry' does not match workspace.json", errors)
            self.assertIn("workflow-history status", errors)

    def test_current_workflow_pointer_schema_and_links_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Current workflow pointer validation fixture.")
            current_path = project / ".loopplane" / "current_workflow.json"
            current = read_json(current_path)
            current["workspace_id"] = "ws_mismatched_current"
            current["current_workflow_id"] = "wf_20260611_deadbeef"
            write_json(current_path, current)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            errors = "\n".join(result["errors"])
            self.assertIn("workspace_id 'ws_mismatched_current' does not match workspace.json", errors)
            self.assertIn("does not match workflow.json", errors)
            self.assertIn("does not reference a workflow in .loopplane/workflow_registry.json", errors)

    def test_current_workflow_pointer_rejects_workspace_id_conflation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Current workflow pointer conflation fixture.")
            workspace = read_json(project / ".loopplane" / "workspace.json")
            current_path = project / ".loopplane" / "current_workflow.json"
            current = read_json(current_path)
            current["current_workflow_id"] = workspace["workspace_id"]
            write_json(current_path, current)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("current_workflow_id does not match", "\n".join(result["errors"]))

    def test_workspace_compatibility_current_workflow_id_must_link_to_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workspace compatibility pointer fixture.")
            workspace_path = project / ".loopplane" / "workspace.json"
            workspace = read_json(workspace_path)
            workspace["current_workflow_id"] = "wf_20260611_deadbeef"
            write_json(workspace_path, workspace)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            errors = "\n".join(result["errors"])
            self.assertIn(".loopplane/workspace.json: current_workflow_id 'wf_20260611_deadbeef'", errors)
            self.assertIn("does not match current_workflow.json", errors)
            self.assertIn("does not reference a workflow in .loopplane/workflow_registry.json", errors)
            with self.assertRaises(InitConflictError) as raised:
                init_project(project, "Workspace compatibility pointer fixture.")
            self.assertIn("workspace.json", "\n".join(raised.exception.conflicts))
            self.assertIn("does not match workflow.json", "\n".join(raised.exception.conflicts))

    def test_workflow_registry_duplicate_workflow_ids_are_rejected_by_schema_and_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Duplicate workflow registry fixture.")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = read_json(registry_path)
            registry["workflows"].append(dict(registry["workflows"][0]))
            write_json(registry_path, registry)

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("duplicate workflow_id", "\n".join(result["errors"]))
            with self.assertRaises(InitConflictError) as raised:
                init_project(project, "Duplicate workflow registry fixture.")
            self.assertIn("duplicate workflow_id", "\n".join(raised.exception.conflicts))

    def test_workflow_registry_allows_many_non_active_histories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            brief = "Many non-active workflow histories fixture."
            init_project(project, brief)
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = read_json(registry_path)
            template = registry["workflows"][0]
            registry["workflows"].extend(
                [
                    workflow_history_record(template, "wf_20260611_11111111", "completed"),
                    workflow_history_record(template, "wf_20260611_22222222", "failed"),
                    workflow_history_record(template, "wf_20260611_33333333", "archived"),
                    workflow_history_record(template, "wf_20260611_44444444", "superseded"),
                ]
            )
            write_json(registry_path, registry)
            before = registry_path.read_bytes()

            validation = validate_project_schemas(project)
            repeat_init = init_project(project, brief)

            self.assertTrue(validation["ok"], json.dumps(validation, indent=2, sort_keys=True))
            self.assertIn(".loopplane/workflow_registry.json", repeat_init.preserved)
            self.assertEqual(registry_path.read_bytes(), before)

    def test_workflow_registry_rejects_multiple_active_running_histories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index, statuses in enumerate((("running", "running"), ("active", "running"), ("active", "active"))):
                project = Path(tmp) / f"project-{index}"
                brief = f"Active-running conflict fixture {index}."
                init_project(project, brief)
                if index == 0:
                    workspace_path = project / ".loopplane" / "workspace.json"
                    workspace = read_json(workspace_path)
                    workspace["single_active_running_workflow"] = False
                    write_json(workspace_path, workspace)
                registry_path = project / ".loopplane" / "workflow_registry.json"
                registry = read_json(registry_path)
                template = registry["workflows"][0]
                registry["workflows"][0]["status"] = statuses[0]
                registry["workflows"].append(
                    workflow_history_record(template, f"wf_20260611_5555555{index}", statuses[1])
                )
                write_json(registry_path, registry)

                validation = validate_project_schemas(project)

                self.assertFalse(validation["ok"], json.dumps(validation, indent=2, sort_keys=True))
                self.assertIn("one active-running workflow per workspace", "\n".join(validation["errors"]))
                with self.assertRaises(InitConflictError) as raised:
                    init_project(project, brief)
                self.assertIn("one active-running workflow per workspace", "\n".join(raised.exception.conflicts))

    def test_partial_v16_identity_files_require_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Partial v1.6 identity fixture.")
            (project / ".loopplane" / "current_workflow.json").unlink()

            result = validate_project_schemas(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn(
                ".loopplane/current_workflow.json: required when any v1.6 workspace identity file exists",
                "\n".join(result["errors"]),
            )

    def test_required_field_and_enum_failures_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_project = Path(tmp) / "missing"
            init_project(missing_project, "Missing required field fixture.")
            workflow_path = missing_project / ".loopplane" / "config" / "workflow.json"
            workflow = read_json(workflow_path)
            workflow.pop("workflow_id")
            write_json(workflow_path, workflow)

            missing_result = validate_project_schemas(missing_project)

            self.assertFalse(missing_result["ok"])
            self.assertIn("missing required field 'workflow_id'", "\n".join(missing_result["errors"]))

            enum_project = Path(tmp) / "enum"
            init_project(enum_project, "Enum validation fixture.")
            vc_path = enum_project / ".loopplane" / "config" / "version_control.json"
            version_control = read_json(vc_path)
            version_control["provider"] = "svn"
            write_json(vc_path, version_control)

            enum_result = validate_project_schemas(enum_project)

            self.assertFalse(enum_result["ok"])
            self.assertIn("version_control.json.provider", "\n".join(enum_result["errors"]))
            self.assertIn("svn", "\n".join(enum_result["errors"]))

    def test_unsupported_schema_blocks_scheduler_with_exit_code_9(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Unsupported schema fixture.")
            schema_path = project / ".loopplane" / "config" / "schema_version.json"
            schema = read_json(schema_path)
            schema["schema_version"] = "9.9"
            write_json(schema_path, schema)

            result = run_loopplane("preview", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_MIGRATION_REQUIRED, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["next_action"], "wait_config")
            self.assertEqual(payload["selected"]["problem"]["code"], "schema_migration_required")


class MigrationTest(unittest.TestCase):
    def test_migrate_current_project_is_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No-op migration fixture.")

            result = migrate_project(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "no_op")
            self.assertEqual(result["modified_files"], [])
            self.assertIsNone(result["backup_dir"])

    def test_migrate_legacy_schema_creates_backups_records_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Legacy migration fixture.")
            mark_legacy_schema(project)

            preview = run_loopplane("preview", "--project", str(project), "--json")
            self.assertEqual(preview.returncode, EXIT_MIGRATION_REQUIRED, preview.stderr + preview.stdout)

            migrated = run_loopplane("migrate", "--project", str(project), "--json")

            self.assertEqual(migrated.returncode, EXIT_SUCCESS, migrated.stderr + migrated.stdout)
            payload = json.loads(migrated.stdout)
            self.assertEqual(payload["status"], "migrated", json.dumps(payload, indent=2, sort_keys=True))
            self.assertTrue(payload["modified_files"])
            self.assertTrue((project / payload["migration_script"]).is_file())
            self.assertTrue((project / payload["migration_record_path"]).is_file())
            self.assertTrue((project / payload["migration_records_path"]).is_file())
            for backup in payload["backup_files"]:
                self.assertTrue((project / backup).is_file(), backup)

            schema = read_json(project / ".loopplane" / "config" / "schema_version.json")
            self.assertEqual(schema["schema_version"], SCHEMA_VERSION)
            self.assertTrue(all(version == SCHEMA_VERSION for version in schema["files"].values()))

            no_op = run_loopplane("migrate", "--project", str(project), "--json")

            self.assertEqual(no_op.returncode, EXIT_SUCCESS, no_op.stderr + no_op.stdout)
            self.assertEqual(json.loads(no_op.stdout)["status"], "no_op")


if __name__ == "__main__":
    unittest.main()
