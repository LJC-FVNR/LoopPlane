from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from runtime.loopplane_home import ensure_loopplane_home_layout
from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.schema_validation import (
    SCHEMA_DIR,
    available_schema_files,
    validate_loopplane_home_schemas,
    validate_project_schemas,
)
from runtime.workspace_boundary_policy import evaluate_worker_write_boundary
from runtime.workspace_nesting import detect_nested_loopplane_instances, evaluate_nested_workspace_operation


FIXED_TIME = "2026-06-12T00:00:00Z"


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def schema_payload(name: str) -> dict[str, object]:
    return read_json(SCHEMA_DIR / name)


class JsonSchemaCoverageTest(unittest.TestCase):
    def test_all_schema_files_are_valid_draft_2020_12_json_schemas(self) -> None:
        schema_files = available_schema_files()

        self.assertGreaterEqual(len(schema_files), 40)
        for schema_file in schema_files:
            with self.subTest(schema=schema_file.name):
                Draft202012Validator.check_schema(read_json(schema_file))

    def test_project_validation_registers_v16_optional_metadata_and_dashboard_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "v1.6 schema validation fixture.")
            workflow = read_json(project / ".loopplane" / "config" / "workflow.json")
            workspace = read_json(project / ".loopplane" / "workspace.json")
            workflow_id = str(workflow["workflow_id"])
            workspace_id = str(workspace["workspace_id"])

            write_json(
                project / ".loopplane" / "config" / "instance.json",
                {
                    "schema_version": "1.6",
                    "workspace_id": workspace_id,
                    "current_workflow_id": workflow_id,
                    "installed_at": FIXED_TIME,
                    "installed_by": "loopplane test",
                    "layout": "compatibility_flat",
                    "workflow_root": ".loopplane",
                    "project_root": ".",
                },
            )
            write_json(
                project / ".loopplane" / "config" / "workflow_defaults.json",
                {
                    "schema_version": "1.6",
                    "layout": "compatibility_flat",
                    "workflow_root": ".loopplane",
                    "brief_file": "PROJECT_BRIEF.md",
                    "plan_file": "PLAN.md",
                    "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
                    "planning_dir": ".loopplane/planning",
                    "runtime_dir": ".loopplane/runtime",
                    "read_models_dir": ".loopplane/read_models",
                    "requests_dir": ".loopplane/requests",
                    "results_dir": ".loopplane/results",
                    "version_control_config_file": ".loopplane/config/version_control.json",
                },
            )
            write_json(
                project / ".loopplane" / "config" / "package_manifest.json",
                {
                    "schema_version": "loopplane-project-package-manifest-1",
                    "package_name": "loopplane",
                    "package_version": "0.0.0",
                    "package_metadata_schema_version": "loopplane-skill-package-1",
                    "runtime_version": "1.5.0",
                    "tool_version": "loopplane 1.5.0",
                    "layout": "compatibility_flat",
                    "workflow_root": ".loopplane",
                    "project_root": ".",
                    "workflow_id": workflow_id,
                    "package_roots": ["runtime", "scripts"],
                    "project_managed_files": [".loopplane/config/package_manifest.json"],
                    "protected_project_paths": [".loopplane/config/workflow.json"],
                },
            )
            write_json(
                project / ".loopplane" / "config" / "local" / "agent_runners.local.json",
                {
                    "schema_version": "1.6",
                    "runners": {
                        "worker": {
                            "role": "worker",
                            "adapter": "codex_cli",
                            "enabled": True,
                            "command": "codex",
                        }
                    },
                },
            )
            write_json(
                project / ".loopplane" / "runtime" / "dashboard_server.json",
                dashboard_server_state(project, workspace_id=workspace_id, workflow_id=workflow_id),
            )

            result = validate_project_schemas(project)

        self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertIn(".loopplane/config/instance.json", result["checked_files"])
        self.assertIn(".loopplane/config/workflow_defaults.json", result["checked_files"])
        self.assertIn(".loopplane/config/package_manifest.json", result["checked_files"])
        self.assertIn(".loopplane/config/local/agent_runners.local.json", result["checked_files"])
        self.assertIn(".loopplane/runtime/dashboard_server.json", result["checked_files"])
        self.assertIn("workflow_instance.schema.json", result["schemas_used"])
        self.assertIn("workflow_defaults.schema.json", result["schemas_used"])
        self.assertIn("project_package_manifest.schema.json", result["schemas_used"])
        self.assertIn("agent_runners_local.schema.json", result["schemas_used"])
        self.assertIn("dashboard_server.schema.json", result["schemas_used"])

    def test_project_validation_checks_existing_objective_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "objective report schema target fixture.")
            workflow = read_json(project / ".loopplane" / "config" / "workflow.json")
            report_path = project / ".loopplane" / "runtime" / "objectives" / "final_objective_verification.json"
            write_json(
                report_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": workflow["workflow_id"],
                    "scope": "workflow",
                    "phase_id": None,
                    "phase_title": None,
                    "status": "satisfied",
                    "verified_at": FIXED_TIME,
                    "plan_sha256": "sha256:test",
                    "objective_results": [
                        {
                            "objective_id": "FO1",
                            "status": "satisfied",
                            "verdict": "satisfied",
                            "confidence": "high",
                            "evidence_reviewed": [],
                            "agent_rationale": "Satisfied for schema coverage.",
                        }
                    ],
                    "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
                },
            )

            result = validate_project_schemas(project)

        self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertIn(".loopplane/runtime/objectives/final_objective_verification.json", result["checked_files"])
        self.assertIn("objective_verification_report.schema.json", result["schemas_used"])

    def test_project_validation_ignores_objective_verifier_run_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "objective run json schema target fixture.")
            run_dir = project / ".loopplane" / "runtime" / "objectives" / "run_20260616_fixture"
            write_json(run_dir / "agent_status.json", {"schema_version": "1.5", "role": "objective_verifier", "status": "completed"})
            write_json(run_dir / "metadata.json", {"run_id": "run_20260616_fixture"})
            write_json(run_dir / "objective_selection.json", {"scope": "phase", "target_objective_ids": ["PO1"]})

            result = validate_project_schemas(project)

        self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertNotIn(".loopplane/runtime/objectives/run_20260616_fixture/agent_status.json", result["checked_files"])
        self.assertNotIn(".loopplane/runtime/objectives/run_20260616_fixture/metadata.json", result["checked_files"])
        self.assertNotIn(".loopplane/runtime/objectives/run_20260616_fixture/objective_selection.json", result["checked_files"])

    def test_project_validation_follows_configured_dashboard_server_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "dashboard state path schema fixture.")
            workflow = read_json(project / ".loopplane" / "config" / "workflow.json")
            workspace = read_json(project / ".loopplane" / "workspace.json")
            workflow_id = str(workflow["workflow_id"])
            workspace_id = str(workspace["workspace_id"])
            dashboard_path = project / ".loopplane" / "config" / "dashboard.json"
            dashboard = read_json(dashboard_path)
            dashboard["server_state_file"] = "{{workflow_root}}/runtime/custom/dashboard_server.json"
            write_json(dashboard_path, dashboard)
            write_json(
                project / ".loopplane" / "runtime" / "custom" / "dashboard_server.json",
                dashboard_server_state(
                    project,
                    workspace_id=workspace_id,
                    workflow_id=workflow_id,
                    server_state_file=".loopplane/runtime/custom/dashboard_server.json",
                ),
            )

            result = validate_project_schemas(project)

        self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertIn(".loopplane/runtime/custom/dashboard_server.json", result["checked_files"])
        self.assertIn("dashboard_server.schema.json", result["schemas_used"])

    def test_loopplane_home_validation_registers_discovery_files_and_runner_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "loopplane-home"
            ensure_loopplane_home_layout(home)
            lock_path = home / "locks" / "runner_locks" / "shared.lock"
            write_json(
                lock_path,
                {
                    "schema_version": "1.6",
                    "lock_type": "runner_resource",
                    "lock_scope": "machine",
                    "lock_key": "shared",
                    "lock_path": lock_path.as_posix(),
                    "global_concurrency_limit": 1,
                    "queue_when_busy": False,
                    "run_id": "run-1",
                    "workflow_id": "wf_dead",
                    "runner_id": "worker",
                    "role": "worker",
                    "pid": 12345,
                    "acquired_at": FIXED_TIME,
                    "heartbeat_at": FIXED_TIME,
                },
            )

            result = validate_loopplane_home_schemas(home)

        self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertIn("config.json", result["checked_files"])
        self.assertIn("registry/workspaces.json", result["checked_files"])
        self.assertIn("runners/agent_runners.local.json", result["checked_files"])
        self.assertIn("dashboard/servers.json", result["checked_files"])
        self.assertIn("locks/runner_locks/shared.lock", result["checked_files"])
        self.assertIn("loopplane_home_config.schema.json", result["schemas_used"])
        self.assertIn("loopplane_home_workspaces.schema.json", result["schemas_used"])
        self.assertIn("agent_runners_local.schema.json", result["schemas_used"])
        self.assertIn("loopplane_home_dashboard_servers.schema.json", result["schemas_used"])
        self.assertIn("runner_resource_lock.schema.json", result["schemas_used"])

    def test_representative_v16_result_schemas_accept_runtime_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "runtime schema fixture.")
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            run_dir = project / ".loopplane" / "runtime" / "runs" / "run-1"
            run_dir.mkdir(parents=True)

            samples = {
                "migration_export_manifest.schema.json": migration_export_manifest_sample(project),
                "migration_import_result.schema.json": migration_import_result_sample(project),
                "git_ref_bundle_export_result.schema.json": git_ref_bundle_export_result_sample(project),
                "git_ref_bundle_import_result.schema.json": git_ref_bundle_import_result_sample(project),
                "nested_workspace_detection.schema.json": detect_nested_loopplane_instances(project),
                "nested_workspace_policy.schema.json": evaluate_nested_workspace_operation(
                    project,
                    command="workspace register",
                    explicit_target=True,
                ),
                "worker_write_boundary.schema.json": evaluate_worker_write_boundary(
                    project,
                    paths,
                    task_id=None,
                    run_dir=run_dir,
                    agent_status={"changed_files": []},
                ),
            }

        for schema_name, payload in samples.items():
            with self.subTest(schema=schema_name):
                validator = Draft202012Validator(schema_payload(schema_name))
                errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
                self.assertEqual(errors, [], [error.message for error in errors])


def dashboard_server_state(
    project: Path,
    *,
    workspace_id: str,
    workflow_id: str,
    server_state_file: str = ".loopplane/runtime/dashboard_server.json",
) -> dict[str, object]:
    return {
        "schema_version": "1.5",
        "ok": True,
        "status": "serving",
        "server_mode": True,
        "project_root": project.resolve().as_posix(),
        "workspace_id": workspace_id,
        "workflow_id": workflow_id,
        "selected_workflow_id": workflow_id,
        "current_workflow_id": workflow_id,
        "selection_scope": "dashboard_visualization_only",
        "started_at": FIXED_TIME,
        "host": "127.0.0.1",
        "port": 3766,
        "url": "http://127.0.0.1:3766/",
        "api_base_url": "http://127.0.0.1:3766/api",
        "pid": 12345,
        "token": None,
        "token_file": None,
        "token_required": False,
        "mutating_api_requires_token": True,
        "same_origin_required": True,
        "server_state_file": server_state_file,
        "errors": [],
        "warnings": [],
    }


def migration_export_manifest_sample(project: Path) -> dict[str, object]:
    return {
        "schema_version": "loopplane-migration-export-1",
        "profile": "archive",
        "created_at": FIXED_TIME,
        "project_root": ".",
        "workspace_boundary": "project_root",
        "resolved_workspace_boundary": project.resolve().as_posix(),
        "workspace_id": "ws_schemafixture1",
        "current_workflow_id": "wf_20260612_deadbeef",
        "archive_format": "tar",
        "files": [
            {
                "path": ".loopplane/workspace.json",
                "category": "state",
                "source": ".loopplane/workspace.json",
                "size": 12,
                "sha256": "0" * 64,
            }
        ],
        "excluded_paths": [
            {
                "path": ".loopplane/config/local/agent_runners.local.json",
                "reason": "machine_local_config",
            },
            {
                "path": ".loopplane/runtime/dashboard_server.json",
                "reason": "stale_runtime_state",
            },
        ],
        "archive_profile": {
            "read_only_import": True,
        },
        "migration_intent": {
            "target": "read_only_archive",
        },
    }


def migration_import_result_sample(project: Path) -> dict[str, object]:
    return {
        "schema_version": "loopplane-migration-import-1",
        "ok": True,
        "status": "imported",
        "profile": "archive",
        "archive": "/tmp/export.tar",
        "target": project.resolve().as_posix(),
        "project_root": project.resolve().as_posix(),
        "workspace_id": "ws_schemafixture1",
        "workflow_id": "wf_20260612_deadbeef",
        "read_only": True,
        "post_import_actions": ["Run loopplane workflow fork before mutation."],
        "imported_count": 1,
        "warnings": [],
        "errors": [],
        "recovery_actions": [],
    }


def git_ref_bundle_export_result_sample(project: Path) -> dict[str, object]:
    return {
        "schema_version": "loopplane-git-ref-bundle-export-1",
        "generated_at": FIXED_TIME,
        "status": "exported",
        "ok": True,
        "project_root": project.resolve().as_posix(),
        "workflow_id": "wf_20260612_deadbeef",
        "output": "/tmp/loopplane-refs.bundle",
        "git": {"available": True},
        "repository": {"detected": True, "inside_work_tree": True},
        "bundle": {
            "path": "/tmp/loopplane-refs.bundle",
            "format": "git-bundle",
            "ref_count": 1,
            "refs": ["refs/loopplane/checkpoints/1"],
            "managed_refs_only": True,
        },
        "checkpoint_log": {"path": ".loopplane/runtime/git_checkpoints.jsonl", "record_count": 1},
        "safety": git_bundle_safety(),
        "warnings": [],
        "errors": [],
    }


def git_ref_bundle_import_result_sample(project: Path) -> dict[str, object]:
    return {
        "schema_version": "loopplane-git-ref-bundle-import-1",
        "generated_at": FIXED_TIME,
        "status": "imported",
        "ok": True,
        "project_root": project.resolve().as_posix(),
        "workflow_id": "wf_20260612_deadbeef",
        "input": "/tmp/loopplane-refs.bundle",
        "git": {"available": True},
        "repository": {"detected": True, "inside_work_tree": True},
        "bundle": {
            "path": "/tmp/loopplane-refs.bundle",
            "format": "git-bundle",
            "head_count": 1,
            "importable_ref_count": 1,
            "ignored_ref_count": 0,
            "managed_refs_only": True,
        },
        "import": {
            "performed": True,
            "imported_count": 1,
            "refs": ["refs/loopplane/checkpoints/1"],
            "managed_refs_only": True,
            "non_managed_refs_updated": False,
        },
        "checkpoint_log": {"path": ".loopplane/runtime/git_checkpoints.jsonl", "record_count": 1},
        "safety": git_bundle_safety(),
        "warnings": [],
        "errors": [],
    }


def git_bundle_safety() -> dict[str, object]:
    return {
        "no_remote_push": True,
        "no_remote_fetch": True,
        "remote_operations_performed": False,
        "branch_switch_performed": False,
        "history_rewritten": False,
        "user_history_modified": False,
        "user_branch_modified": False,
    }


if __name__ == "__main__":
    unittest.main()
