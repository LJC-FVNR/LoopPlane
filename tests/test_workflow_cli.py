from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.init_workflow import LAYOUT_CANONICAL_V16, LAYOUT_COMPATIBILITY_FLAT, init_project
from runtime.scheduler import AtomicOwnerLock
from runtime.workflow_lifecycle import archive_workflow, create_workflow_record, import_workflow_record


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
    raise AssertionError(f"missing workflow registry record {workflow_id}")


def future_timestamp(seconds: int = 300) -> str:
    return (
        datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def stale_timestamp(seconds: int = 3600) -> str:
    return (
        datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


class WorkflowCommandGroupCliTest(unittest.TestCase):
    def test_workflow_group_help_lists_required_history_subcommands(self) -> None:
        result = run_loopplane("workflow", "--help")

        self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
        self.assertIn("usage: loopplane workflow", result.stdout)
        for command in (
            "list",
            "current",
            "show",
            "switch",
            "create",
            "archive",
            "restore",
            "fork",
        ):
            self.assertIn(command, result.stdout)

        for command in (
            ("list",),
            ("current",),
            ("show",),
            ("switch",),
            ("create",),
            ("archive",),
            ("restore",),
            ("fork",),
        ):
            with self.subTest(command=" ".join(command)):
                help_result = run_loopplane("workflow", *command, "--help")
                self.assertEqual(help_result.returncode, EXIT_SUCCESS, help_result.stderr + help_result.stdout)
                self.assertIn("--json", help_result.stdout)
                self.assertIn("--project", help_result.stdout)

    def test_workflow_without_subcommand_exits_with_actionable_usage(self) -> None:
        result = run_loopplane("workflow")

        self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
        self.assertIn("usage: loopplane workflow", result.stderr)
        self.assertIn("missing workflow command", result.stderr)
        self.assertIn("list, current, show, switch, create, archive, restore, fork", result.stderr)

    def test_workflow_unknown_subcommand_uses_argparse_error_shape(self) -> None:
        result = run_loopplane("workflow", "missing-command")

        self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
        self.assertIn("usage: loopplane workflow", result.stderr)
        self.assertIn("invalid choice", result.stderr)

    def test_workflow_list_reports_missing_project_and_missing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            uninitialized.mkdir()

            missing = run_loopplane("workflow", "list", "--project", str(missing_project), "--json")
            uninitialized_result = run_loopplane("workflow", "list", "--project", str(uninitialized), "--json")

            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            missing_payload = json.loads(missing.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "missing_project")
            self.assertIn("recovery_actions", missing_payload)

            self.assertEqual(uninitialized_result.returncode, EXIT_INVALID_CONFIG, uninitialized_result.stderr + uninitialized_result.stdout)
            uninitialized_payload = json.loads(uninitialized_result.stdout)
            self.assertFalse(uninitialized_payload["ok"])
            self.assertEqual(uninitialized_payload["status"], "missing_workspace")
            self.assertIn("Run loopplane init", "\n".join(uninitialized_payload["recovery_actions"]))

    def test_workflow_list_json_and_text_reads_single_canonical_registry_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow list canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workflow", "list", "--project", str(project), "--json")
            text = run_loopplane("workflow", "list", "--project", str(project))

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "listed")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertTrue(payload["current_found"])
            self.assertEqual(payload["workflow_count"], 1)
            workflow = payload["workflows"][0]
            self.assertEqual(workflow["workflow_id"], initialized.workflow_id)
            self.assertEqual(workflow["name"], "Workflow list canonical fixture.")
            self.assertEqual(workflow["status"], "draft")
            self.assertEqual(workflow["workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")
            self.assertEqual(workflow["layout"], "canonical_v16")
            self.assertFalse(workflow["archived"])
            self.assertFalse(workflow["read_only"])
            self.assertTrue(workflow["current"])
            self.assertIn("current", workflow["labels"])
            self.assertIn("created_at", workflow)
            self.assertIn("last_seen_at", workflow)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow list: listed", text.stdout)
            self.assertIn(f"* {initialized.workflow_id}", text.stdout)
            self.assertIn("labels=current", text.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workflow_list_supports_empty_registry_without_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Workflow list empty registry fixture.", layout=LAYOUT_CANONICAL_V16)
            workspace_path = project / ".loopplane" / "workspace.json"
            registry_path = project / ".loopplane" / "workflow_registry.json"
            current_path = project / ".loopplane" / "current_workflow.json"
            workspace = read_json(workspace_path)
            workspace.pop("current_workflow_id", None)
            write_json(workspace_path, workspace)
            registry = read_json(registry_path)
            registry["workflows"] = []
            write_json(registry_path, registry)
            current_path.unlink()

            result = run_loopplane("workflow", "list", "--project", str(project), "--json")
            text = run_loopplane("workflow", "list", "--project", str(project))

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["workflow_count"], 0)
            self.assertEqual(payload["workflows"], [])
            self.assertIsNone(payload["current_workflow_id"])
            self.assertFalse(payload["current_found"])
            self.assertIn("current_workflow.json is missing", "\n".join(payload["warnings"]))
            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("workflows: none", text.stdout)

    def test_workflow_list_marks_current_archived_and_read_only_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow list multi-history fixture.", layout=LAYOUT_CANONICAL_V16)
            imported_id = "wf_20260611_bbbbbbbb"

            archive_workflow(project, initialized.workflow_id, reason="keep for dashboard", updated_by="test")
            import_workflow_record(
                project,
                workflow_id=imported_id,
                name="imported read-only history",
                workflow_root=f".loopplane/imported/{imported_id}",
                updated_by="test",
            )

            result = run_loopplane("workflow", "list", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["workflow_count"], 2)
            by_id = {workflow["workflow_id"]: workflow for workflow in payload["workflows"]}
            archived = by_id[initialized.workflow_id]
            self.assertEqual(archived["status"], "archived")
            self.assertTrue(archived["current"])
            self.assertTrue(archived["archived"])
            self.assertIn("current", archived["labels"])
            self.assertIn("archived", archived["labels"])

            imported = by_id[imported_id]
            self.assertEqual(imported["status"], "read_only_imported")
            self.assertTrue(imported["read_only"])
            self.assertIn("read_only", imported["labels"])

    def test_workflow_list_supports_v15_flat_compatibility_registry_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow list flat compatibility fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)

            result = run_loopplane("workflow", "list", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["workflow_count"], 1)
            workflow = payload["workflows"][0]
            self.assertEqual(workflow["workflow_id"], initialized.workflow_id)
            self.assertEqual(workflow["workflow_root"], ".loopplane/")
            self.assertEqual(workflow["layout"], "compatibility_flat")
            self.assertEqual(workflow["runtime_dir"], ".loopplane/runtime")

    def test_workflow_list_rejects_malformed_registry_and_current_pointer_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed_project = root / "malformed"
            mismatch_project = root / "mismatch"
            init_project(malformed_project, "Workflow list malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            init_project(mismatch_project, "Workflow list pointer mismatch fixture.", layout=LAYOUT_CANONICAL_V16)

            malformed_registry = read_json(malformed_project / ".loopplane" / "workflow_registry.json")
            malformed_registry["workflows"] = "not a list"
            write_json(malformed_project / ".loopplane" / "workflow_registry.json", malformed_registry)

            current = read_json(mismatch_project / ".loopplane" / "current_workflow.json")
            current["current_workflow_id"] = "wf_20260611_deadbeef"
            write_json(mismatch_project / ".loopplane" / "current_workflow.json", current)

            malformed = run_loopplane("workflow", "list", "--project", str(malformed_project), "--json")
            mismatch = run_loopplane("workflow", "list", "--project", str(mismatch_project), "--json")

            self.assertEqual(malformed.returncode, EXIT_INVALID_CONFIG, malformed.stderr + malformed.stdout)
            malformed_payload = json.loads(malformed.stdout)
            self.assertFalse(malformed_payload["ok"])
            self.assertEqual(malformed_payload["status"], "malformed_registry")
            self.assertIn("workflows must be an array", "\n".join(malformed_payload["errors"]))

            self.assertEqual(mismatch.returncode, EXIT_INVALID_CONFIG, mismatch.stderr + mismatch.stdout)
            mismatch_payload = json.loads(mismatch.stdout)
            self.assertFalse(mismatch_payload["ok"])
            self.assertEqual(mismatch_payload["status"], "current_pointer_mismatch")
            self.assertIn("not present", "\n".join(mismatch_payload["errors"]))

    def test_workflow_current_reports_missing_project_workspace_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            missing_pointer_project = root / "missing-pointer"
            uninitialized.mkdir()
            init_project(missing_pointer_project, "Workflow current missing pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            (missing_pointer_project / ".loopplane" / "current_workflow.json").unlink()

            missing = run_loopplane("workflow", "current", "--project", str(missing_project), "--json")
            uninitialized_result = run_loopplane("workflow", "current", "--project", str(uninitialized), "--json")
            missing_pointer = run_loopplane("workflow", "current", "--project", str(missing_pointer_project), "--json")

            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            self.assertEqual(json.loads(missing.stdout)["status"], "missing_project")

            self.assertEqual(uninitialized_result.returncode, EXIT_INVALID_CONFIG, uninitialized_result.stderr + uninitialized_result.stdout)
            uninitialized_payload = json.loads(uninitialized_result.stdout)
            self.assertEqual(uninitialized_payload["status"], "missing_workspace")
            self.assertIn("loopplane init", "\n".join(uninitialized_payload["recovery_actions"]))

            self.assertEqual(missing_pointer.returncode, EXIT_INVALID_CONFIG, missing_pointer.stderr + missing_pointer.stdout)
            pointer_payload = json.loads(missing_pointer.stdout)
            self.assertEqual(pointer_payload["status"], "missing_current_pointer")
            self.assertIn(".loopplane/current_workflow.json", "\n".join(pointer_payload["errors"]))

    def test_workflow_current_json_and_text_reads_canonical_pointer_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow current canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}
            pointer = read_json(project / ".loopplane" / "current_workflow.json")
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            write_json(
                loopplane_home / "registry" / "workspaces.json",
                {
                    "schema_version": "1.6",
                    "generated_at": "2026-06-11T00:00:00Z",
                    "workspaces": [
                        {
                            "workspace_id": initialized.workspace_id,
                            "project_root": project.as_posix(),
                            "current_workflow_id": "wf_20260611_deadbeef",
                        }
                    ],
                },
            )
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane("workflow", "current", "--project", str(project), "--json", env=env)
            text = run_loopplane("workflow", "current", "--project", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "current")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["selection_reason"], pointer["selection_reason"])
            self.assertEqual(payload["updated_at"], pointer["updated_at"])
            self.assertEqual(payload["updated_by"], pointer["updated_by"])
            self.assertEqual(payload["current_workflow"], pointer)
            workflow = payload["workflow"]
            self.assertEqual(workflow["workflow_id"], initialized.workflow_id)
            self.assertEqual(workflow["name"], "Workflow current canonical fixture.")
            self.assertEqual(workflow["status"], "draft")
            self.assertEqual(workflow["workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")
            self.assertEqual(workflow["layout"], "canonical_v16")
            self.assertFalse(workflow["archived"])
            self.assertFalse(workflow["read_only"])
            self.assertTrue(workflow["current"])
            self.assertIn("current", workflow["labels"])
            self.assertIn("mutation_boundary", payload)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow current: current", text.stdout)
            self.assertIn(f"current_workflow_id: {initialized.workflow_id}", text.stdout)
            self.assertIn("selection_reason: initial_workflow", text.stdout)
            self.assertIn("current: true", text.stdout)
            self.assertIn("labels: current", text.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_project_local_execution_commands_do_not_require_global_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry_path.parent.mkdir(parents=True)
            write_json(
                registry_path,
                {
                    "authority": "discovery_only",
                    "schema_version": "1.6",
                    "workspaces": [],
                },
            )
            registry_path.unlink()
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Project-local commands without global registry.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: file_sha256(path) for path in authoritative_files}

            command_results = [
                (
                    "workflow_current",
                    run_loopplane("workflow", "current", "--project", str(project), "--json", env=env),
                    "current",
                ),
                (
                    "workflow_list",
                    run_loopplane("workflow", "list", "--project", str(project), "--json", env=env),
                    "listed",
                ),
                (
                    "status",
                    run_loopplane("status", "--project", str(project), "--json", env=env),
                    "initialized",
                ),
                (
                    "health",
                    run_loopplane("health", "--project", str(project), "--json", env=env),
                    "healthy",
                ),
            ]

            for name, result, expected_status in command_results:
                with self.subTest(command=name):
                    self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertFalse(registry_path.exists(), f"{name} created the global discovery registry")

            workflow_current = json.loads(command_results[0][1].stdout)
            workflow_list = json.loads(command_results[1][1].stdout)
            status = json.loads(command_results[2][1].stdout)
            health = json.loads(command_results[3][1].stdout)
            self.assertEqual(workflow_current["workspace_id"], initialized.workspace_id)
            self.assertEqual(workflow_current["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(workflow_list["workspace_id"], initialized.workspace_id)
            self.assertEqual(workflow_list["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(status["workflow_id"], initialized.workflow_id)
            self.assertEqual(health["workflow_id"], initialized.workflow_id)
            self.assertEqual({path: file_sha256(path) for path in authoritative_files}, before)

    def test_workflow_current_supports_v15_flat_compatibility_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow current flat compatibility fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)

            result = run_loopplane("workflow", "current", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            workflow = payload["workflow"]
            self.assertEqual(workflow["workflow_root"], ".loopplane/")
            self.assertEqual(workflow["layout"], "compatibility_flat")
            self.assertEqual(workflow["runtime_dir"], ".loopplane/runtime")
            self.assertTrue(workflow["current"])

    def test_workflow_current_surfaces_archived_and_read_only_current_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow current archived/read-only fixture.", layout=LAYOUT_CANONICAL_V16)

            archive_workflow(project, initialized.workflow_id, reason="keep for dashboard", updated_by="test")
            archived = run_loopplane("workflow", "current", "--project", str(project), "--json")

            self.assertEqual(archived.returncode, EXIT_SUCCESS, archived.stderr + archived.stdout)
            archived_payload = json.loads(archived.stdout)
            archived_workflow = archived_payload["workflow"]
            self.assertEqual(archived_workflow["workflow_id"], initialized.workflow_id)
            self.assertEqual(archived_workflow["status"], "archived")
            self.assertTrue(archived_workflow["archived"])
            self.assertTrue(archived_workflow["current"])
            self.assertIn("archived", archived_workflow["labels"])
            self.assertIn("current", archived_workflow["labels"])

            imported_id = "wf_20260611_bbbbbbbb"
            import_workflow_record(
                project,
                workflow_id=imported_id,
                name="imported read-only current",
                workflow_root=f".loopplane/imported/{imported_id}",
                make_current=True,
                updated_by="test",
            )
            read_only = run_loopplane("workflow", "current", "--project", str(project), "--json")

            self.assertEqual(read_only.returncode, EXIT_SUCCESS, read_only.stderr + read_only.stdout)
            read_only_payload = json.loads(read_only.stdout)
            read_only_workflow = read_only_payload["workflow"]
            self.assertEqual(read_only_payload["selection_reason"], "workflow_imported")
            self.assertEqual(read_only_workflow["workflow_id"], imported_id)
            self.assertEqual(read_only_workflow["status"], "read_only_imported")
            self.assertTrue(read_only_workflow["read_only"])
            self.assertTrue(read_only_workflow["current"])
            self.assertIn("read_only", read_only_workflow["labels"])
            self.assertIn("current", read_only_workflow["labels"])

    def test_workflow_current_rejects_malformed_registry_pointer_and_unknown_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed_registry_project = root / "malformed-registry"
            malformed_pointer_project = root / "malformed-pointer"
            mismatch_project = root / "mismatch"
            init_project(malformed_registry_project, "Workflow current malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            init_project(malformed_pointer_project, "Workflow current malformed pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            init_project(mismatch_project, "Workflow current pointer mismatch fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(malformed_registry_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"] = "not a list"
            write_json(malformed_registry_project / ".loopplane" / "workflow_registry.json", registry)

            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            current = read_json(mismatch_project / ".loopplane" / "current_workflow.json")
            current["current_workflow_id"] = "wf_20260611_deadbeef"
            write_json(mismatch_project / ".loopplane" / "current_workflow.json", current)

            malformed_registry = run_loopplane("workflow", "current", "--project", str(malformed_registry_project), "--json")
            malformed_pointer = run_loopplane("workflow", "current", "--project", str(malformed_pointer_project), "--json")
            mismatch = run_loopplane("workflow", "current", "--project", str(mismatch_project), "--json")

            self.assertEqual(malformed_registry.returncode, EXIT_INVALID_CONFIG, malformed_registry.stderr + malformed_registry.stdout)
            registry_payload = json.loads(malformed_registry.stdout)
            self.assertEqual(registry_payload["status"], "malformed_registry")
            self.assertIn("workflows must be an array", "\n".join(registry_payload["errors"]))

            self.assertEqual(malformed_pointer.returncode, EXIT_INVALID_CONFIG, malformed_pointer.stderr + malformed_pointer.stdout)
            pointer_payload = json.loads(malformed_pointer.stdout)
            self.assertEqual(pointer_payload["status"], "malformed_current_pointer")
            self.assertIn("Unable to read .loopplane/current_workflow.json", "\n".join(pointer_payload["errors"]))

            self.assertEqual(mismatch.returncode, EXIT_INVALID_CONFIG, mismatch.stderr + mismatch.stdout)
            mismatch_payload = json.loads(mismatch.stdout)
            self.assertEqual(mismatch_payload["status"], "current_pointer_mismatch")
            self.assertIn("not present", "\n".join(mismatch_payload["errors"]))

    def test_workflow_show_reports_missing_workspace_invalid_and_unknown_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            project = root / "project"
            uninitialized.mkdir()
            init_project(project, "Workflow show unknown fixture.", layout=LAYOUT_CANONICAL_V16)

            missing = run_loopplane("workflow", "show", "wf_20260611_deadbeef", "--project", str(missing_project), "--json")
            uninitialized_result = run_loopplane(
                "workflow",
                "show",
                "wf_20260611_deadbeef",
                "--project",
                str(uninitialized),
                "--json",
            )
            invalid = run_loopplane("workflow", "show", "not-a-workflow", "--project", str(project), "--json")
            unknown = run_loopplane("workflow", "show", "wf_20260611_deadbeef", "--project", str(project), "--json")

            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            self.assertEqual(json.loads(missing.stdout)["status"], "missing_project")

            self.assertEqual(uninitialized_result.returncode, EXIT_INVALID_CONFIG, uninitialized_result.stderr + uninitialized_result.stdout)
            uninitialized_payload = json.loads(uninitialized_result.stdout)
            self.assertEqual(uninitialized_payload["status"], "missing_workspace")
            self.assertIn("loopplane init", "\n".join(uninitialized_payload["recovery_actions"]))

            self.assertEqual(invalid.returncode, EXIT_INVALID_CONFIG, invalid.stderr + invalid.stdout)
            invalid_payload = json.loads(invalid.stdout)
            self.assertEqual(invalid_payload["status"], "invalid_workflow_id")
            self.assertIn("workflow_id must match", "\n".join(invalid_payload["errors"]))

            self.assertEqual(unknown.returncode, EXIT_INVALID_CONFIG, unknown.stderr + unknown.stdout)
            unknown_payload = json.loads(unknown.stdout)
            self.assertEqual(unknown_payload["status"], "unknown_workflow")
            self.assertIn("not present", "\n".join(unknown_payload["errors"]))

    def test_workflow_show_json_and_text_reads_canonical_history_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow show canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
                project / ".loopplane" / "workflows" / initialized.workflow_id / "read_models" / "workflow_status.json",
                project / ".loopplane" / "workflows" / initialized.workflow_id / "read_models" / "plan_index.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            write_json(
                loopplane_home / "registry" / "workspaces.json",
                {
                    "schema_version": "1.6",
                    "generated_at": "2026-06-11T00:00:00Z",
                    "workspaces": [
                        {
                            "workspace_id": initialized.workspace_id,
                            "project_root": project.as_posix(),
                            "current_workflow_id": "wf_20260611_deadbeef",
                        }
                    ],
                },
            )
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "workflow",
                "show",
                initialized.workflow_id,
                "--project",
                str(project),
                "--json",
                env=env,
            )
            text = run_loopplane("workflow", "show", initialized.workflow_id, "--project", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "shown")
            self.assertEqual(payload["workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            workflow = payload["workflow"]
            self.assertEqual(workflow["workflow_id"], initialized.workflow_id)
            self.assertEqual(workflow["workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")
            self.assertEqual(workflow["layout"], "canonical_v16")
            self.assertTrue(workflow["current"])
            self.assertIn("current", workflow["labels"])
            self.assertIn("key_paths", workflow)
            self.assertEqual(workflow["key_paths"]["plan_file"]["relative"], f".loopplane/workflows/{initialized.workflow_id}/PLAN.md")
            self.assertTrue(workflow["key_paths"]["read_models_dir"]["exists"])
            read_models = workflow["read_models"]
            self.assertEqual(read_models["freshness"]["status"], "metadata_available")
            self.assertTrue(read_models["files"]["workflow_status.json"]["exists"])
            self.assertEqual(read_models["files"]["workflow_status.json"]["workflow_id"], initialized.workflow_id)
            self.assertIn("progress", workflow)
            self.assertEqual(workflow["progress"]["total_tasks"], 0)
            self.assertIn("mutation_boundary", payload)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow show: shown", text.stdout)
            self.assertIn(f"workflow_id: {initialized.workflow_id}", text.stdout)
            self.assertIn("read_model_freshness: metadata_available", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workflow_show_supports_v15_flat_compatibility_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow show flat compatibility fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)

            result = run_loopplane("workflow", "show", initialized.workflow_id, "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            workflow = payload["workflow"]
            self.assertEqual(workflow["workflow_root"], ".loopplane/")
            self.assertEqual(workflow["layout"], "compatibility_flat")
            self.assertEqual(workflow["key_paths"]["plan_file"]["relative"], "PLAN.md")
            self.assertEqual(workflow["key_paths"]["workflow_config_file"]["relative"], ".loopplane/config/workflow.json")
            self.assertTrue(workflow["read_models"]["files"]["workflow_status.json"]["exists"])

    def test_workflow_show_surfaces_archived_and_read_only_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow show archived/read-only fixture.", layout=LAYOUT_CANONICAL_V16)
            imported_id = "wf_20260611_bbbbbbbb"

            archive_workflow(project, initialized.workflow_id, reason="keep for dashboard", updated_by="test")
            import_workflow_record(
                project,
                workflow_id=imported_id,
                name="imported read-only history",
                workflow_root=f".loopplane/imported/{imported_id}",
                make_current=True,
                updated_by="test",
            )

            archived = run_loopplane("workflow", "show", initialized.workflow_id, "--project", str(project), "--json")
            read_only = run_loopplane("workflow", "show", imported_id, "--project", str(project), "--json")

            self.assertEqual(archived.returncode, EXIT_SUCCESS, archived.stderr + archived.stdout)
            archived_workflow = json.loads(archived.stdout)["workflow"]
            self.assertEqual(archived_workflow["status"], "archived")
            self.assertTrue(archived_workflow["archived"])
            self.assertFalse(archived_workflow["current"])
            self.assertIn("archived", archived_workflow["labels"])

            self.assertEqual(read_only.returncode, EXIT_SUCCESS, read_only.stderr + read_only.stdout)
            read_only_workflow = json.loads(read_only.stdout)["workflow"]
            self.assertEqual(read_only_workflow["status"], "read_only_imported")
            self.assertTrue(read_only_workflow["read_only"])
            self.assertTrue(read_only_workflow["current"])
            self.assertIn("read_only", read_only_workflow["labels"])
            self.assertIn("current", read_only_workflow["labels"])

    def test_workflow_show_rejects_malformed_pointer_and_invalid_selected_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed_pointer_project = root / "malformed-pointer"
            invalid_metadata_project = root / "invalid-metadata"
            malformed = init_project(
                malformed_pointer_project,
                "Workflow show malformed pointer fixture.",
                layout=LAYOUT_CANONICAL_V16,
            )
            invalid = init_project(
                invalid_metadata_project,
                "Workflow show invalid metadata fixture.",
                layout=LAYOUT_CANONICAL_V16,
            )

            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            registry = read_json(invalid_metadata_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"][0]["plan_file"] = "/tmp/outside-plan.md"
            write_json(invalid_metadata_project / ".loopplane" / "workflow_registry.json", registry)

            malformed_result = run_loopplane(
                "workflow",
                "show",
                malformed.workflow_id,
                "--project",
                str(malformed_pointer_project),
                "--json",
            )
            invalid_result = run_loopplane(
                "workflow",
                "show",
                invalid.workflow_id,
                "--project",
                str(invalid_metadata_project),
                "--json",
            )

            self.assertEqual(malformed_result.returncode, EXIT_INVALID_CONFIG, malformed_result.stderr + malformed_result.stdout)
            malformed_payload = json.loads(malformed_result.stdout)
            self.assertEqual(malformed_payload["status"], "malformed_current_pointer")
            self.assertIn("Unable to read .loopplane/current_workflow.json", "\n".join(malformed_payload["errors"]))

            self.assertEqual(invalid_result.returncode, EXIT_INVALID_CONFIG, invalid_result.stderr + invalid_result.stdout)
            invalid_payload = json.loads(invalid_result.stdout)
            self.assertEqual(invalid_payload["status"], "invalid_workflow_metadata")
            self.assertIn("plan_file", "\n".join(invalid_payload["errors"]))

    def test_workflow_switch_json_and_text_updates_project_local_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow switch canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            target_id = "wf_20260611_cccccccc"
            create_workflow_record(
                project,
                workflow_id=target_id,
                name="completed switch target",
                workflow_root=f".loopplane/workflows/{target_id}",
                status="completed",
                updated_by="test",
            )
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            write_json(
                loopplane_home / "registry" / "workspaces.json",
                {
                    "schema_version": "1.6",
                    "generated_at": "2026-06-11T00:00:00Z",
                    "workspaces": [
                        {
                            "workspace_id": initialized.workspace_id,
                            "project_root": project.as_posix(),
                            "current_workflow_id": "wf_20260611_deadbeef",
                        }
                    ],
                },
            )
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "workflow",
                "switch",
                target_id,
                "--project",
                str(project),
                "--json",
                env=env,
            )
            text = run_loopplane("workflow", "switch", target_id, "--project", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "switched")
            self.assertEqual(payload["previous_current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["current_workflow_id"], target_id)
            self.assertEqual(payload["selection_reason"], "cli_workflow_switch")
            self.assertEqual(payload["updated_by"], "loopplane workflow switch")
            self.assertEqual(payload["workflow"]["workflow_id"], target_id)
            self.assertTrue(payload["workflow"]["current"])
            self.assertFalse(payload["safety"]["blockers"])
            self.assertIn("Dashboard-only visual selection", payload["mutation_boundary"])

            current = read_json(project / ".loopplane" / "current_workflow.json")
            workspace = read_json(project / ".loopplane" / "workspace.json")
            self.assertEqual(current["current_workflow_id"], target_id)
            self.assertEqual(current["selection_reason"], "cli_workflow_switch")
            self.assertEqual(workspace["current_workflow_id"], target_id)

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow switch: already_current", text.stdout)
            self.assertIn(f"current_workflow_id: {target_id}", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)

    def test_workflow_switch_recreates_missing_current_pointer_after_safety_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow switch missing pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            (project / ".loopplane" / "current_workflow.json").unlink()

            result = run_loopplane("workflow", "switch", initialized.workflow_id, "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "switched")
            self.assertIsNone(payload["previous_current_workflow_id"])
            self.assertIn("current_workflow.json is missing", "\n".join(payload["warnings"]))
            self.assertEqual(
                read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"],
                initialized.workflow_id,
            )

    def test_workflow_switch_reports_missing_invalid_unknown_and_malformed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            unknown_project = root / "unknown"
            malformed_registry_project = root / "malformed-registry"
            malformed_pointer_project = root / "malformed-pointer"
            uninitialized.mkdir()
            init_project(unknown_project, "Workflow switch unknown fixture.", layout=LAYOUT_CANONICAL_V16)
            malformed = init_project(malformed_registry_project, "Workflow switch malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            pointer = init_project(malformed_pointer_project, "Workflow switch malformed pointer fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(malformed_registry_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"] = "not a list"
            write_json(malformed_registry_project / ".loopplane" / "workflow_registry.json", registry)
            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            cases = [
                (
                    run_loopplane("workflow", "switch", "wf_20260611_deadbeef", "--project", str(missing_project), "--json"),
                    "missing_project",
                ),
                (
                    run_loopplane("workflow", "switch", "wf_20260611_deadbeef", "--project", str(uninitialized), "--json"),
                    "missing_workspace",
                ),
                (
                    run_loopplane("workflow", "switch", "not-a-workflow", "--project", str(unknown_project), "--json"),
                    "invalid_workflow_id",
                ),
                (
                    run_loopplane("workflow", "switch", "wf_20260611_deadbeef", "--project", str(unknown_project), "--json"),
                    "unknown_workflow",
                ),
                (
                    run_loopplane("workflow", "switch", malformed.workflow_id, "--project", str(malformed_registry_project), "--json"),
                    "malformed_registry",
                ),
                (
                    run_loopplane("workflow", "switch", pointer.workflow_id, "--project", str(malformed_pointer_project), "--json"),
                    "malformed_current_pointer",
                ),
            ]

            for result, expected_status in cases:
                with self.subTest(status=expected_status):
                    self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertIn("recovery_actions", payload)

    def test_workflow_switch_rejects_archived_and_read_only_targets_without_mutating_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow switch immutable target fixture.", layout=LAYOUT_CANONICAL_V16)
            current_id = "wf_20260611_dddddddd"
            read_only_id = "wf_20260611_eeeeeeee"
            create_workflow_record(
                project,
                workflow_id=current_id,
                name="mutable current",
                workflow_root=f".loopplane/workflows/{current_id}",
                status="completed",
                make_current=True,
                updated_by="test",
            )
            archive_workflow(project, initialized.workflow_id, reason="keep for dashboard", updated_by="test")
            import_workflow_record(
                project,
                workflow_id=read_only_id,
                name="read-only imported switch target",
                workflow_root=f".loopplane/imported/{read_only_id}",
                updated_by="test",
            )
            before = (project / ".loopplane" / "current_workflow.json").read_bytes()

            archived = run_loopplane("workflow", "switch", initialized.workflow_id, "--project", str(project), "--json")
            read_only = run_loopplane("workflow", "switch", read_only_id, "--project", str(project), "--json")

            self.assertEqual(archived.returncode, EXIT_INVALID_CONFIG, archived.stderr + archived.stdout)
            archived_payload = json.loads(archived.stdout)
            self.assertEqual(archived_payload["status"], "archived_workflow")
            self.assertEqual(archived_payload["workflow_count"], 3)
            self.assertIn(initialized.workflow_id, [record["workflow_id"] for record in archived_payload["workflows"]])
            self.assertIn("restore", "\n".join(archived_payload["recovery_actions"]))
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), before)

            self.assertEqual(read_only.returncode, EXIT_INVALID_CONFIG, read_only.stderr + read_only.stdout)
            read_only_payload = json.loads(read_only.stdout)
            self.assertEqual(read_only_payload["status"], "read_only_workflow")
            self.assertEqual(read_only_payload["workflow_count"], 3)
            self.assertIn(read_only_id, [record["workflow_id"] for record in read_only_payload["workflows"]])
            self.assertIn("fork", "\n".join(read_only_payload["recovery_actions"]))
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), before)

    def test_workflow_switch_rejects_scheduler_lock_active_lease_and_active_running_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow switch runtime safety fixture.", layout=LAYOUT_CANONICAL_V16)
            locked_target = "wf_20260611_abababab"
            lease_target = "wf_20260611_cdcdcdcd"
            blocked_target = "wf_20260611_efefefef"
            active_id = "wf_20260611_aaaaaaaa"
            create_workflow_record(
                project,
                workflow_id=locked_target,
                name="locked switch target",
                workflow_root=f".loopplane/workflows/{locked_target}",
                status="completed",
                updated_by="test",
            )
            create_workflow_record(
                project,
                workflow_id=lease_target,
                name="lease switch target",
                workflow_root=f".loopplane/workflows/{lease_target}",
                status="completed",
                updated_by="test",
            )
            before = (project / ".loopplane" / "current_workflow.json").read_bytes()

            lock = AtomicOwnerLock(
                project
                / ".loopplane"
                / "workflows"
                / initialized.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock",
                "test-owner",
            )
            with lock.acquire():
                locked = run_loopplane("workflow", "switch", locked_target, "--project", str(project), "--json")

            self.assertEqual(locked.returncode, EXIT_INVALID_CONFIG, locked.stderr + locked.stdout)
            locked_payload = json.loads(locked.stdout)
            self.assertEqual(locked_payload["status"], "scheduler_lock_conflict")
            self.assertIn("active_scheduler_lock", {blocker["code"] for blocker in locked_payload["safety"]["blockers"]})
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), before)

            lease_dir = project / ".loopplane" / "workflows" / lease_target / "runtime" / "active_run_leases"
            lease_dir.mkdir(parents=True)
            write_json(
                lease_dir / "run_active.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": lease_target,
                    "run_id": "run_active",
                    "status": "running",
                    "heartbeat_at": future_timestamp(),
                    "lease_expires_at": future_timestamp(),
                },
            )

            leased = run_loopplane("workflow", "switch", lease_target, "--project", str(project), "--json")
            self.assertEqual(leased.returncode, EXIT_INVALID_CONFIG, leased.stderr + leased.stdout)
            leased_payload = json.loads(leased.stdout)
            self.assertEqual(leased_payload["status"], "active_run_conflict")
            self.assertIn("active_run_lease", {blocker["code"] for blocker in leased_payload["safety"]["blockers"]})
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), before)

            create_workflow_record(
                project,
                workflow_id=blocked_target,
                name="blocked by active workflow",
                workflow_root=f".loopplane/workflows/{blocked_target}",
                status="completed",
                updated_by="test",
            )
            create_workflow_record(
                project,
                workflow_id=active_id,
                name="active running workflow",
                workflow_root=f".loopplane/workflows/{active_id}",
                status="running",
                updated_by="test",
            )

            active_conflict = run_loopplane("workflow", "switch", blocked_target, "--project", str(project), "--json")
            self.assertEqual(active_conflict.returncode, EXIT_INVALID_CONFIG, active_conflict.stderr + active_conflict.stdout)
            active_payload = json.loads(active_conflict.stdout)
            self.assertEqual(active_payload["status"], "active_running_conflict")
            self.assertIn("active_running_conflict", {blocker["code"] for blocker in active_payload["safety"]["blockers"]})
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), before)

    def test_workflow_switch_allows_non_running_active_registry_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow switch active status fixture.", layout=LAYOUT_CANONICAL_V16)
            target_id = "wf_20260611_bcbcbcbc"
            create_workflow_record(
                project,
                workflow_id=target_id,
                name="completed switch target",
                workflow_root=f".loopplane/workflows/{target_id}",
                status="completed",
                updated_by="test",
            )
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = read_json(registry_path)
            for record in registry["workflows"]:
                if record["workflow_id"] == initialized.workflow_id:
                    record["status"] = "active"
            write_json(registry_path, registry)

            result = run_loopplane("workflow", "switch", target_id, "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "switched")
            self.assertFalse(payload["safety"]["blockers"])
            current = read_json(project / ".loopplane" / "current_workflow.json")
            self.assertEqual(current["current_workflow_id"], target_id)

    def test_workflow_create_json_and_text_creates_canonical_history_without_overwriting_previous_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Initial workflow create fixture.", layout=LAYOUT_CANONICAL_V16)
            old_root = project / ".loopplane" / "workflows" / initialized.workflow_id
            preserved_files = [
                old_root / "PROJECT_BRIEF.md",
                old_root / "PLAN.md",
                old_root / "config" / "workflow.json",
            ]
            before_old_root = {path: path.read_bytes() for path in preserved_files}
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            global_registry = {
                "schema_version": "1.6",
                "generated_at": "2026-06-11T00:00:00Z",
                "workspaces": [
                    {
                        "workspace_id": initialized.workspace_id,
                        "project_root": project.as_posix(),
                        "current_workflow_id": "wf_20260611_deadbeef",
                    }
                ],
            }
            write_json(loopplane_home / "registry" / "workspaces.json", global_registry)
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "New workflow create fixture.\nPreserve the original workflow history.",
                "--project",
                str(project),
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["previous_current_workflow_id"], initialized.workflow_id)
            self.assertNotEqual(payload["workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["current_workflow_id"], payload["workflow_id"])
            self.assertEqual(payload["selection_reason"], "cli_workflow_create")
            self.assertEqual(payload["updated_by"], "loopplane workflow create")
            self.assertEqual(payload["workflow"]["layout"], "canonical_v16")
            self.assertTrue(payload["workflow"]["current"])
            self.assertIn("PROJECT_BRIEF.md", "\n".join(payload["created"]))
            self.assertIn("mutation_boundary", payload)

            registry = read_json(project / ".loopplane" / "workflow_registry.json")
            current = read_json(project / ".loopplane" / "current_workflow.json")
            workspace = read_json(project / ".loopplane" / "workspace.json")
            workflow_ids = [record["workflow_id"] for record in registry["workflows"]]
            self.assertEqual(len(workflow_ids), 2)
            self.assertIn(initialized.workflow_id, workflow_ids)
            self.assertIn(payload["workflow_id"], workflow_ids)
            self.assertEqual(current["current_workflow_id"], payload["workflow_id"])
            self.assertEqual(workspace["current_workflow_id"], payload["workflow_id"])
            self.assertEqual({path: path.read_bytes() for path in preserved_files}, before_old_root)
            self.assertEqual(read_json(loopplane_home / "registry" / "workspaces.json"), global_registry)
            new_root = project / payload["workflow_root"]
            self.assertTrue((new_root / "PROJECT_BRIEF.md").is_file())
            self.assertTrue((new_root / "PLAN.md").is_file())
            self.assertTrue((new_root / "config" / "workflow.json").is_file())
            self.assertFalse((project / "PLAN.md").exists())

            text = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Second create text output fixture.",
                "--project",
                str(project),
                env=env,
            )
            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow create: created", text.stdout)
            self.assertIn("workflow_root: .loopplane/workflows/", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)

    def test_workflow_create_truncates_long_names_at_word_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Initial workflow create name fixture.", layout=LAYOUT_CANONICAL_V16)
            brief = ("abcdefghij " * 8) + "klmnopqrstuvwx yz"

            result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                brief,
                "--project",
                str(project),
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            name = payload["workflow"]["name"]
            source = " ".join(brief.split())
            self.assertLessEqual(len(name), 96)
            self.assertTrue(source.startswith(name))
            self.assertEqual(source[len(name)], " ")
            self.assertTrue(payload["workflow_name_was_truncated"])
            self.assertEqual(payload["workflow_name_source_excerpt"], source)
            self.assertEqual(payload["workflow_name_limit"], 96)

            project_text = Path(tmp) / "project_text"
            init_project(project_text, "Initial workflow create text fixture.", layout=LAYOUT_CANONICAL_V16)
            text = run_loopplane(
                "workflow",
                "create",
                "--brief",
                brief,
                "--project",
                str(project_text),
            )
            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("workflow_name_was_truncated: true", text.stdout)
            self.assertIn("workflow_name_source_excerpt:", text.stdout)

    def test_workflow_create_reports_missing_workspace_and_invalid_empty_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            initialized_project = root / "project"
            uninitialized.mkdir()
            init_project(initialized_project, "Workflow create invalid brief fixture.", layout=LAYOUT_CANONICAL_V16)

            missing = run_loopplane("workflow", "create", "--brief", "Create fixture.", "--project", str(missing_project), "--json")
            uninitialized_result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Create fixture.",
                "--project",
                str(uninitialized),
                "--json",
            )
            empty = run_loopplane("workflow", "create", "--brief", "   ", "--project", str(initialized_project), "--json")

            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            self.assertEqual(json.loads(missing.stdout)["status"], "missing_project")

            self.assertEqual(uninitialized_result.returncode, EXIT_INVALID_CONFIG, uninitialized_result.stderr + uninitialized_result.stdout)
            uninitialized_payload = json.loads(uninitialized_result.stdout)
            self.assertEqual(uninitialized_payload["status"], "missing_workspace")
            self.assertIn("loopplane init", "\n".join(uninitialized_payload["recovery_actions"]))

            self.assertEqual(empty.returncode, EXIT_INVALID_CONFIG, empty.stderr + empty.stdout)
            empty_payload = json.loads(empty.stdout)
            self.assertEqual(empty_payload["status"], "invalid_brief")
            self.assertIn("--brief", "\n".join(empty_payload["errors"]))

    def test_workflow_create_recreates_missing_current_pointer_after_safety_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow create missing pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            (project / ".loopplane" / "current_workflow.json").unlink()

            result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Create after missing current pointer.",
                "--project",
                str(project),
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "created")
            self.assertIsNone(payload["previous_current_workflow_id"])
            self.assertIn("current_workflow.json is missing", "\n".join(payload["warnings"]))
            self.assertNotEqual(payload["workflow_id"], initialized.workflow_id)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], payload["workflow_id"])

    def test_workflow_create_rejects_runtime_conflicts_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow create runtime safety fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}
            before_roots = sorted(path.name for path in (project / ".loopplane" / "workflows").iterdir() if path.is_dir())
            lock = AtomicOwnerLock(
                project
                / ".loopplane"
                / "workflows"
                / initialized.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock",
                "test-owner",
            )

            with lock.acquire():
                locked = run_loopplane(
                    "workflow",
                    "create",
                    "--brief",
                    "Create should wait for scheduler lock.",
                    "--project",
                    str(project),
                    "--json",
                )

            self.assertEqual(locked.returncode, EXIT_INVALID_CONFIG, locked.stderr + locked.stdout)
            locked_payload = json.loads(locked.stdout)
            self.assertEqual(locked_payload["status"], "scheduler_lock_conflict")
            self.assertIn("active_scheduler_lock", {blocker["code"] for blocker in locked_payload["safety"]["blockers"]})
            self.assertIsNone(locked_payload["workflow_id"])
            self.assertTrue(str(locked_payload["proposed_workflow_id"]).startswith("wf_"))
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)
            self.assertEqual(
                sorted(path.name for path in (project / ".loopplane" / "workflows").iterdir() if path.is_dir()),
                before_roots,
            )

            running_id = "wf_20260611_acacacac"
            create_workflow_record(
                project,
                workflow_id=running_id,
                name="running workflow blocks create",
                workflow_root=f".loopplane/workflows/{running_id}",
                status="running",
                updated_by="test",
            )
            before_after_running = {path: path.read_bytes() for path in authoritative_files}

            active_conflict = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Create should wait for running workflow.",
                "--project",
                str(project),
                "--json",
            )

            self.assertEqual(active_conflict.returncode, EXIT_INVALID_CONFIG, active_conflict.stderr + active_conflict.stdout)
            active_payload = json.loads(active_conflict.stdout)
            self.assertEqual(active_payload["status"], "active_running_conflict")
            self.assertIn("active_running_conflict", {blocker["code"] for blocker in active_payload["safety"]["blockers"]})
            self.assertIsNone(active_payload["workflow_id"])
            self.assertTrue(str(active_payload["proposed_workflow_id"]).startswith("wf_"))
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before_after_running)

    def test_workflow_create_reaps_stale_scheduler_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow create stale lock fixture.", layout=LAYOUT_CANONICAL_V16)
            owner_path = (
                project
                / ".loopplane"
                / "workflows"
                / initialized.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock"
                / "owner.json"
            )
            owner_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                owner_path,
                {
                    "schema_version": "1.5",
                    "owner": "stale-owner",
                    "heartbeat_at": stale_timestamp(),
                    "ttl_seconds": 1,
                },
            )

            result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Create should reap stale scheduler lock.",
                "--project",
                str(project),
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "created")
            self.assertFalse(owner_path.exists())

    def test_workflow_archive_json_and_text_preserve_history_and_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Initial workflow archive fixture.", layout=LAYOUT_CANONICAL_V16)
            create_result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Workflow archive current-pointer fixture.",
                "--project",
                str(project),
                "--json",
            )
            self.assertEqual(create_result.returncode, EXIT_SUCCESS, create_result.stderr + create_result.stdout)
            created_payload = json.loads(create_result.stdout)
            current_id = created_payload["workflow_id"]
            original_root = project / ".loopplane" / "workflows" / initialized.workflow_id
            current_root = project / ".loopplane" / "workflows" / current_id
            preserved_files = [
                original_root / "PROJECT_BRIEF.md",
                original_root / "PLAN.md",
                original_root / "config" / "workflow.json",
                current_root / "PROJECT_BRIEF.md",
                current_root / "PLAN.md",
                current_root / "config" / "workflow.json",
            ]
            before_history = {path: path.read_bytes() for path in preserved_files}
            current_before = (project / ".loopplane" / "current_workflow.json").read_bytes()
            workspace_before = (project / ".loopplane" / "workspace.json").read_bytes()
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            global_registry = {
                "schema_version": "1.6",
                "generated_at": "2026-06-11T00:00:00Z",
                "workspaces": [
                    {
                        "workspace_id": initialized.workspace_id,
                        "project_root": project.as_posix(),
                        "current_workflow_id": "wf_20260611_deadbeef",
                    }
                ],
            }
            write_json(loopplane_home / "registry" / "workspaces.json", global_registry)
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            archived_original = run_loopplane(
                "workflow",
                "archive",
                initialized.workflow_id,
                "--project",
                str(project),
                "--json",
                env=env,
            )

            self.assertEqual(archived_original.returncode, EXIT_SUCCESS, archived_original.stderr + archived_original.stdout)
            payload = json.loads(archived_original.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "archived")
            self.assertEqual(payload["workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["previous_workflow_status"], "draft")
            self.assertEqual(payload["current_workflow_id"], current_id)
            self.assertFalse(payload["current_pointer_updated"])
            self.assertEqual(payload["updated_by"], "loopplane workflow archive")
            self.assertTrue(payload["workflow"]["archived"])
            self.assertFalse(payload["workflow"]["current"])
            self.assertIn("archived", payload["workflow"]["labels"])
            self.assertFalse(payload["safety"]["blockers"])
            self.assertIn("does not update .loopplane/current_workflow.json", payload["mutation_boundary"])
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), current_before)
            self.assertEqual((project / ".loopplane" / "workspace.json").read_bytes(), workspace_before)
            self.assertEqual(read_json(loopplane_home / "registry" / "workspaces.json"), global_registry)
            self.assertEqual({path: path.read_bytes() for path in preserved_files}, before_history)
            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "archived")
            self.assertEqual(registry_record(project, current_id)["status"], "draft")

            archived_current_text = run_loopplane(
                "workflow",
                "archive",
                current_id,
                "--project",
                str(project),
                env=env,
            )

            self.assertEqual(archived_current_text.returncode, EXIT_SUCCESS, archived_current_text.stderr + archived_current_text.stdout)
            self.assertIn("loopplane workflow archive: archived", archived_current_text.stdout)
            self.assertIn(f"current_workflow_id: {current_id}", archived_current_text.stdout)
            self.assertIn("current_pointer_updated: false", archived_current_text.stdout)
            self.assertIn("current: true", archived_current_text.stdout)
            self.assertIn("mutation_boundary:", archived_current_text.stdout)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], current_id)
            self.assertEqual(read_json(project / ".loopplane" / "workspace.json")["current_workflow_id"], current_id)
            self.assertEqual({path: path.read_bytes() for path in preserved_files}, before_history)
            self.assertEqual(registry_record(project, current_id)["status"], "archived")

    def test_workflow_archive_reports_missing_invalid_unknown_and_malformed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            unknown_project = root / "unknown"
            malformed_registry_project = root / "malformed-registry"
            malformed_pointer_project = root / "malformed-pointer"
            uninitialized.mkdir()
            init_project(unknown_project, "Workflow archive unknown fixture.", layout=LAYOUT_CANONICAL_V16)
            malformed = init_project(malformed_registry_project, "Workflow archive malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            pointer = init_project(malformed_pointer_project, "Workflow archive malformed pointer fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(malformed_registry_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"] = "not a list"
            write_json(malformed_registry_project / ".loopplane" / "workflow_registry.json", registry)
            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            cases = [
                (
                    run_loopplane("workflow", "archive", "wf_20260611_deadbeef", "--project", str(missing_project), "--json"),
                    "missing_project",
                ),
                (
                    run_loopplane("workflow", "archive", "wf_20260611_deadbeef", "--project", str(uninitialized), "--json"),
                    "missing_workspace",
                ),
                (
                    run_loopplane("workflow", "archive", "not-a-workflow", "--project", str(unknown_project), "--json"),
                    "invalid_workflow_id",
                ),
                (
                    run_loopplane("workflow", "archive", "wf_20260611_deadbeef", "--project", str(unknown_project), "--json"),
                    "unknown_workflow",
                ),
                (
                    run_loopplane("workflow", "archive", malformed.workflow_id, "--project", str(malformed_registry_project), "--json"),
                    "malformed_registry",
                ),
                (
                    run_loopplane("workflow", "archive", pointer.workflow_id, "--project", str(malformed_pointer_project), "--json"),
                    "malformed_current_pointer",
                ),
            ]

            for result, expected_status in cases:
                with self.subTest(status=expected_status):
                    self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertIn("recovery_actions", payload)

    def test_workflow_archive_rejects_already_archived_and_read_only_without_mutating_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow archive immutable fixture.", layout=LAYOUT_CANONICAL_V16)
            read_only_id = "wf_20260611_fafafafa"
            archive_workflow(project, initialized.workflow_id, reason="already archived", updated_by="test")
            import_workflow_record(
                project,
                workflow_id=read_only_id,
                name="read-only imported archive target",
                workflow_root=f".loopplane/imported/{read_only_id}",
                updated_by="test",
            )
            registry_before = (project / ".loopplane" / "workflow_registry.json").read_bytes()

            archived = run_loopplane("workflow", "archive", initialized.workflow_id, "--project", str(project), "--json")
            read_only = run_loopplane("workflow", "archive", read_only_id, "--project", str(project), "--json")

            self.assertEqual(archived.returncode, EXIT_INVALID_CONFIG, archived.stderr + archived.stdout)
            archived_payload = json.loads(archived.stdout)
            self.assertEqual(archived_payload["status"], "already_archived_workflow")
            self.assertIn("already archived", "\n".join(archived_payload["errors"]))
            self.assertEqual((project / ".loopplane" / "workflow_registry.json").read_bytes(), registry_before)

            self.assertEqual(read_only.returncode, EXIT_INVALID_CONFIG, read_only.stderr + read_only.stdout)
            read_only_payload = json.loads(read_only.stdout)
            self.assertEqual(read_only_payload["status"], "read_only_workflow")
            self.assertIn("read-only", "\n".join(read_only_payload["errors"]))
            self.assertEqual((project / ".loopplane" / "workflow_registry.json").read_bytes(), registry_before)

    def test_workflow_archive_allows_missing_current_pointer_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow archive missing pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.unlink()

            result = run_loopplane("workflow", "archive", initialized.workflow_id, "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "archived")
            self.assertIsNone(payload["current_workflow_id"])
            self.assertFalse(payload["current_pointer_updated"])
            self.assertIn("current_workflow.json is missing", "\n".join(payload["warnings"]))
            self.assertFalse(current_path.exists())
            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "archived")

    def test_workflow_archive_rejects_running_lock_and_active_lease_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            running_project = root / "running"
            lock_project = root / "lock"
            lease_project = root / "lease"
            running = init_project(running_project, "Workflow archive running fixture.", layout=LAYOUT_CANONICAL_V16)
            locked = init_project(lock_project, "Workflow archive lock fixture.", layout=LAYOUT_CANONICAL_V16)
            leased = init_project(lease_project, "Workflow archive lease fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(running_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"][0]["status"] = "running"
            write_json(running_project / ".loopplane" / "workflow_registry.json", registry)
            running_before = (running_project / ".loopplane" / "workflow_registry.json").read_bytes()
            running_result = run_loopplane("workflow", "archive", running.workflow_id, "--project", str(running_project), "--json")

            self.assertEqual(running_result.returncode, EXIT_INVALID_CONFIG, running_result.stderr + running_result.stdout)
            running_payload = json.loads(running_result.stdout)
            self.assertEqual(running_payload["status"], "active_running_conflict")
            self.assertIn("active_running_conflict", {blocker["code"] for blocker in running_payload["safety"]["blockers"]})
            self.assertEqual((running_project / ".loopplane" / "workflow_registry.json").read_bytes(), running_before)

            lock_before = (lock_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lock = AtomicOwnerLock(
                lock_project
                / ".loopplane"
                / "workflows"
                / locked.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock",
                "test-owner",
            )
            with lock.acquire():
                lock_result = run_loopplane("workflow", "archive", locked.workflow_id, "--project", str(lock_project), "--json")

            self.assertEqual(lock_result.returncode, EXIT_INVALID_CONFIG, lock_result.stderr + lock_result.stdout)
            lock_payload = json.loads(lock_result.stdout)
            self.assertEqual(lock_payload["status"], "scheduler_lock_conflict")
            self.assertIn("active_scheduler_lock", {blocker["code"] for blocker in lock_payload["safety"]["blockers"]})
            self.assertEqual((lock_project / ".loopplane" / "workflow_registry.json").read_bytes(), lock_before)

            lease_dir = lease_project / ".loopplane" / "workflows" / leased.workflow_id / "runtime" / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                lease_dir / "run_active.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": leased.workflow_id,
                    "run_id": "run_active",
                    "status": "running",
                    "heartbeat_at": future_timestamp(),
                    "lease_expires_at": future_timestamp(),
                },
            )
            lease_before = (lease_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lease_result = run_loopplane("workflow", "archive", leased.workflow_id, "--project", str(lease_project), "--json")

            self.assertEqual(lease_result.returncode, EXIT_INVALID_CONFIG, lease_result.stderr + lease_result.stdout)
            lease_payload = json.loads(lease_result.stdout)
            self.assertEqual(lease_payload["status"], "active_run_conflict")
            self.assertIn("active_run_lease", {blocker["code"] for blocker in lease_payload["safety"]["blockers"]})
            self.assertEqual((lease_project / ".loopplane" / "workflow_registry.json").read_bytes(), lease_before)

    def test_workflow_restore_json_and_text_preserve_history_and_update_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Initial workflow restore fixture.", layout=LAYOUT_CANONICAL_V16)
            create_result = run_loopplane(
                "workflow",
                "create",
                "--brief",
                "Workflow restore current-pointer fixture.",
                "--project",
                str(project),
                "--json",
            )
            self.assertEqual(create_result.returncode, EXIT_SUCCESS, create_result.stderr + create_result.stdout)
            created_payload = json.loads(create_result.stdout)
            current_id = created_payload["workflow_id"]
            archive_result = run_loopplane(
                "workflow",
                "archive",
                initialized.workflow_id,
                "--project",
                str(project),
                "--json",
            )
            self.assertEqual(archive_result.returncode, EXIT_SUCCESS, archive_result.stderr + archive_result.stdout)

            original_root = project / ".loopplane" / "workflows" / initialized.workflow_id
            current_root = project / ".loopplane" / "workflows" / current_id
            preserved_files = [
                original_root / "PROJECT_BRIEF.md",
                original_root / "PLAN.md",
                original_root / "config" / "workflow.json",
                current_root / "PROJECT_BRIEF.md",
                current_root / "PLAN.md",
                current_root / "config" / "workflow.json",
            ]
            before_history = {path: path.read_bytes() for path in preserved_files}
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            global_registry = {
                "schema_version": "1.6",
                "generated_at": "2026-06-11T00:00:00Z",
                "workspaces": [
                    {
                        "workspace_id": initialized.workspace_id,
                        "project_root": project.as_posix(),
                        "current_workflow_id": "wf_20260611_deadbeef",
                    }
                ],
            }
            write_json(loopplane_home / "registry" / "workspaces.json", global_registry)
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            restored = run_loopplane(
                "workflow",
                "restore",
                initialized.workflow_id,
                "--project",
                str(project),
                "--json",
                env=env,
            )

            self.assertEqual(restored.returncode, EXIT_SUCCESS, restored.stderr + restored.stdout)
            payload = json.loads(restored.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "restored")
            self.assertEqual(payload["workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["previous_workflow_status"], "archived")
            self.assertEqual(payload["previous_current_workflow_id"], current_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertTrue(payload["current_pointer_updated"])
            self.assertEqual(payload["selection_reason"], "cli_workflow_restore")
            self.assertEqual(payload["updated_by"], "loopplane workflow restore")
            self.assertEqual(payload["workflow"]["status"], "active")
            self.assertFalse(payload["workflow"]["archived"])
            self.assertTrue(payload["workflow"]["current"])
            self.assertFalse(payload["safety"]["blockers"])
            self.assertIn("updates .loopplane/current_workflow.json only after runtime safety checks pass", payload["mutation_boundary"])
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["selection_reason"], "cli_workflow_restore")
            self.assertEqual(read_json(project / ".loopplane" / "workspace.json")["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "active")
            self.assertEqual(registry_record(project, current_id)["status"], "draft")
            self.assertEqual(read_json(loopplane_home / "registry" / "workspaces.json"), global_registry)
            self.assertEqual({path: path.read_bytes() for path in preserved_files}, before_history)

            text_project = Path(tmp) / "text-project"
            text_initialized = init_project(text_project, "Workflow restore text fixture.", layout=LAYOUT_CANONICAL_V16)
            archive_workflow(text_project, text_initialized.workflow_id, reason="text restore", updated_by="test")
            text = run_loopplane("workflow", "restore", text_initialized.workflow_id, "--project", str(text_project), env=env)

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow restore: restored", text.stdout)
            self.assertIn(f"current_workflow_id: {text_initialized.workflow_id}", text.stdout)
            self.assertIn("current_pointer_updated: true", text.stdout)
            self.assertIn("workflow_status: active", text.stdout)
            self.assertIn("archived: false", text.stdout)
            self.assertIn("current: true", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)

    def test_workflow_restore_reports_missing_invalid_unknown_and_malformed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            unknown_project = root / "unknown"
            malformed_registry_project = root / "malformed-registry"
            malformed_pointer_project = root / "malformed-pointer"
            uninitialized.mkdir()
            init_project(unknown_project, "Workflow restore unknown fixture.", layout=LAYOUT_CANONICAL_V16)
            malformed = init_project(malformed_registry_project, "Workflow restore malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            pointer = init_project(malformed_pointer_project, "Workflow restore malformed pointer fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(malformed_registry_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"] = "not a list"
            write_json(malformed_registry_project / ".loopplane" / "workflow_registry.json", registry)
            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            cases = [
                (
                    run_loopplane("workflow", "restore", "wf_20260611_deadbeef", "--project", str(missing_project), "--json"),
                    "missing_project",
                ),
                (
                    run_loopplane("workflow", "restore", "wf_20260611_deadbeef", "--project", str(uninitialized), "--json"),
                    "missing_workspace",
                ),
                (
                    run_loopplane("workflow", "restore", "not-a-workflow", "--project", str(unknown_project), "--json"),
                    "invalid_workflow_id",
                ),
                (
                    run_loopplane("workflow", "restore", "wf_20260611_deadbeef", "--project", str(unknown_project), "--json"),
                    "unknown_workflow",
                ),
                (
                    run_loopplane("workflow", "restore", malformed.workflow_id, "--project", str(malformed_registry_project), "--json"),
                    "malformed_registry",
                ),
                (
                    run_loopplane("workflow", "restore", pointer.workflow_id, "--project", str(malformed_pointer_project), "--json"),
                    "malformed_current_pointer",
                ),
            ]

            for result, expected_status in cases:
                with self.subTest(status=expected_status):
                    self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertIn("recovery_actions", payload)

    def test_workflow_restore_rejects_non_archived_and_read_only_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow restore immutable fixture.", layout=LAYOUT_CANONICAL_V16)
            read_only_id = "wf_20260611_fafafafa"
            import_workflow_record(
                project,
                workflow_id=read_only_id,
                name="read-only imported restore target",
                workflow_root=f".loopplane/imported/{read_only_id}",
                updated_by="test",
            )
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            non_archived = run_loopplane("workflow", "restore", initialized.workflow_id, "--project", str(project), "--json")
            read_only = run_loopplane("workflow", "restore", read_only_id, "--project", str(project), "--json")

            self.assertEqual(non_archived.returncode, EXIT_INVALID_CONFIG, non_archived.stderr + non_archived.stdout)
            non_archived_payload = json.loads(non_archived.stdout)
            self.assertEqual(non_archived_payload["status"], "workflow_not_archived")
            self.assertIn("not archived", "\n".join(non_archived_payload["errors"]))
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(read_only.returncode, EXIT_INVALID_CONFIG, read_only.stderr + read_only.stdout)
            read_only_payload = json.loads(read_only.stdout)
            self.assertEqual(read_only_payload["status"], "read_only_workflow")
            self.assertIn("fork", "\n".join(read_only_payload["recovery_actions"]))
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workflow_restore_recreates_missing_current_pointer_after_safety_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow restore missing pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            archive_workflow(project, initialized.workflow_id, reason="restore missing pointer", updated_by="test")
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.unlink()

            result = run_loopplane("workflow", "restore", initialized.workflow_id, "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "restored")
            self.assertIsNone(payload["previous_current_workflow_id"])
            self.assertIn("current_workflow.json is missing", "\n".join(payload["warnings"]))
            self.assertEqual(read_json(current_path)["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "active")

    def test_workflow_restore_rejects_running_lock_and_active_lease_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            running_project = root / "running"
            lock_project = root / "lock"
            lease_project = root / "lease"
            running = init_project(running_project, "Workflow restore running fixture.", layout=LAYOUT_CANONICAL_V16)
            locked = init_project(lock_project, "Workflow restore lock fixture.", layout=LAYOUT_CANONICAL_V16)
            leased = init_project(lease_project, "Workflow restore lease fixture.", layout=LAYOUT_CANONICAL_V16)

            archive_workflow(running_project, running.workflow_id, reason="running conflict target", updated_by="test")
            create_workflow_record(
                running_project,
                workflow_id="wf_20260611_aaaaaaaa",
                name="running workflow blocks restore",
                workflow_root=".loopplane/workflows/wf_20260611_aaaaaaaa",
                status="running",
                updated_by="test",
            )
            running_before = {
                path: path.read_bytes()
                for path in (
                    running_project / ".loopplane" / "workspace.json",
                    running_project / ".loopplane" / "workflow_registry.json",
                    running_project / ".loopplane" / "current_workflow.json",
                )
            }
            running_result = run_loopplane("workflow", "restore", running.workflow_id, "--project", str(running_project), "--json")

            self.assertEqual(running_result.returncode, EXIT_INVALID_CONFIG, running_result.stderr + running_result.stdout)
            running_payload = json.loads(running_result.stdout)
            self.assertEqual(running_payload["status"], "active_running_conflict")
            self.assertIn("active_running_conflict", {blocker["code"] for blocker in running_payload["safety"]["blockers"]})
            self.assertEqual({path: path.read_bytes() for path in running_before}, running_before)

            archive_workflow(lock_project, locked.workflow_id, reason="lock conflict target", updated_by="test")
            lock_before = (lock_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lock = AtomicOwnerLock(
                lock_project
                / ".loopplane"
                / "workflows"
                / locked.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock",
                "test-owner",
            )
            with lock.acquire():
                lock_result = run_loopplane("workflow", "restore", locked.workflow_id, "--project", str(lock_project), "--json")

            self.assertEqual(lock_result.returncode, EXIT_INVALID_CONFIG, lock_result.stderr + lock_result.stdout)
            lock_payload = json.loads(lock_result.stdout)
            self.assertEqual(lock_payload["status"], "scheduler_lock_conflict")
            self.assertIn("active_scheduler_lock", {blocker["code"] for blocker in lock_payload["safety"]["blockers"]})
            self.assertEqual((lock_project / ".loopplane" / "workflow_registry.json").read_bytes(), lock_before)

            archive_workflow(lease_project, leased.workflow_id, reason="lease conflict target", updated_by="test")
            lease_dir = lease_project / ".loopplane" / "workflows" / leased.workflow_id / "runtime" / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                lease_dir / "run_active.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": leased.workflow_id,
                    "run_id": "run_active",
                    "status": "running",
                    "heartbeat_at": future_timestamp(),
                    "lease_expires_at": future_timestamp(),
                },
            )
            lease_before = (lease_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lease_result = run_loopplane("workflow", "restore", leased.workflow_id, "--project", str(lease_project), "--json")

            self.assertEqual(lease_result.returncode, EXIT_INVALID_CONFIG, lease_result.stderr + lease_result.stdout)
            lease_payload = json.loads(lease_result.stdout)
            self.assertEqual(lease_payload["status"], "active_run_conflict")
            self.assertIn("active_run_lease", {blocker["code"] for blocker in lease_payload["safety"]["blockers"]})
            self.assertEqual((lease_project / ".loopplane" / "workflow_registry.json").read_bytes(), lease_before)

    def test_workflow_fork_json_and_text_preserve_source_history_and_update_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Initial workflow fork fixture.", layout=LAYOUT_CANONICAL_V16)
            source_root = project / ".loopplane" / "workflows" / initialized.workflow_id
            preserved_files = [
                source_root / "PROJECT_BRIEF.md",
                source_root / "PLAN.md",
                source_root / "config" / "workflow.json",
            ]
            before_hashes = {path: file_sha256(path) for path in preserved_files}
            loopplane_home = Path(tmp) / "home"
            (loopplane_home / "registry").mkdir(parents=True)
            global_registry = {
                "schema_version": "1.6",
                "generated_at": "2026-06-11T00:00:00Z",
                "workspaces": [
                    {
                        "workspace_id": initialized.workspace_id,
                        "project_root": project.as_posix(),
                        "current_workflow_id": "wf_20260611_deadbeef",
                    }
                ],
            }
            write_json(loopplane_home / "registry" / "workspaces.json", global_registry)
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            forked = run_loopplane(
                "workflow",
                "fork",
                initialized.workflow_id,
                "--name",
                "Forked retry",
                "--project",
                str(project),
                "--json",
                env=env,
            )

            self.assertEqual(forked.returncode, EXIT_SUCCESS, forked.stderr + forked.stdout)
            payload = json.loads(forked.stdout)
            fork_id = payload["workflow_id"]
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "forked")
            self.assertEqual(payload["source_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["forked_workflow_id"], fork_id)
            self.assertNotEqual(fork_id, initialized.workflow_id)
            self.assertEqual(payload["previous_current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["current_workflow_id"], fork_id)
            self.assertTrue(payload["current_pointer_updated"])
            self.assertEqual(payload["selection_reason"], "cli_workflow_fork")
            self.assertEqual(payload["updated_by"], "loopplane workflow fork")
            self.assertEqual(payload["workflow"]["status"], "forked")
            self.assertEqual(payload["workflow"]["registry_record"]["forked_from"], initialized.workflow_id)
            self.assertEqual(payload["workflow"]["registry_record"]["source_workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")
            self.assertTrue(payload["workflow"]["current"])
            self.assertFalse(payload["workflow"]["archived"])
            self.assertFalse(payload["workflow"]["read_only"])
            self.assertEqual(payload["source_workflow"]["status"], "draft")
            self.assertFalse(payload["safety"]["blockers"])
            self.assertIn("records source lineage without mutating", payload["mutation_boundary"])
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["current_workflow_id"], fork_id)
            self.assertEqual(read_json(project / ".loopplane" / "current_workflow.json")["selection_reason"], "cli_workflow_fork")
            self.assertEqual(read_json(project / ".loopplane" / "workspace.json")["current_workflow_id"], fork_id)
            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "draft")
            self.assertEqual(registry_record(project, fork_id)["status"], "forked")
            self.assertTrue((project / ".loopplane" / "workflows" / fork_id / "config" / "workflow.json").is_file())
            self.assertEqual({path: file_sha256(path) for path in preserved_files}, before_hashes)
            self.assertEqual(read_json(loopplane_home / "registry" / "workspaces.json"), global_registry)

            text_project = Path(tmp) / "text-project"
            text_initialized = init_project(text_project, "Workflow fork text fixture.", layout=LAYOUT_CANONICAL_V16)
            text = run_loopplane(
                "workflow",
                "fork",
                text_initialized.workflow_id,
                "--name",
                "Text fork",
                "--project",
                str(text_project),
                env=env,
            )

            self.assertEqual(text.returncode, EXIT_SUCCESS, text.stderr + text.stdout)
            self.assertIn("loopplane workflow fork: forked", text.stdout)
            self.assertIn(f"source_workflow_id: {text_initialized.workflow_id}", text.stdout)
            self.assertIn("forked_workflow_id: wf_", text.stdout)
            self.assertIn("current_pointer_updated: true", text.stdout)
            self.assertIn("workflow_status: forked", text.stdout)
            self.assertIn("forked_from:", text.stdout)
            self.assertIn("current: true", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)

    def test_workflow_fork_reports_missing_invalid_unknown_and_malformed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_project = root / "missing-project"
            uninitialized = root / "uninitialized"
            unknown_project = root / "unknown"
            malformed_registry_project = root / "malformed-registry"
            malformed_pointer_project = root / "malformed-pointer"
            uninitialized.mkdir()
            init_project(unknown_project, "Workflow fork unknown fixture.", layout=LAYOUT_CANONICAL_V16)
            malformed = init_project(malformed_registry_project, "Workflow fork malformed registry fixture.", layout=LAYOUT_CANONICAL_V16)
            pointer = init_project(malformed_pointer_project, "Workflow fork malformed pointer fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(malformed_registry_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"] = "not a list"
            write_json(malformed_registry_project / ".loopplane" / "workflow_registry.json", registry)
            (malformed_pointer_project / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            missing_name = run_loopplane("workflow", "fork", pointer.workflow_id, "--project", str(malformed_pointer_project))
            self.assertEqual(missing_name.returncode, 2, missing_name.stderr + missing_name.stdout)
            self.assertIn("--name", missing_name.stderr)

            cases = [
                (
                    run_loopplane(
                        "workflow",
                        "fork",
                        "wf_20260611_deadbeef",
                        "--name",
                        "missing project",
                        "--project",
                        str(missing_project),
                        "--json",
                    ),
                    "missing_project",
                ),
                (
                    run_loopplane(
                        "workflow",
                        "fork",
                        "wf_20260611_deadbeef",
                        "--name",
                        "missing workspace",
                        "--project",
                        str(uninitialized),
                        "--json",
                    ),
                    "missing_workspace",
                ),
                (
                    run_loopplane("workflow", "fork", "not-a-workflow", "--name", "bad id", "--project", str(unknown_project), "--json"),
                    "invalid_workflow_id",
                ),
                (
                    run_loopplane("workflow", "fork", pointer.workflow_id, "--name", "   ", "--project", str(malformed_pointer_project), "--json"),
                    "invalid_name",
                ),
                (
                    run_loopplane(
                        "workflow",
                        "fork",
                        "wf_20260611_deadbeef",
                        "--name",
                        "unknown",
                        "--project",
                        str(unknown_project),
                        "--json",
                    ),
                    "unknown_workflow",
                ),
                (
                    run_loopplane(
                        "workflow",
                        "fork",
                        malformed.workflow_id,
                        "--name",
                        "bad registry",
                        "--project",
                        str(malformed_registry_project),
                        "--json",
                    ),
                    "malformed_registry",
                ),
                (
                    run_loopplane(
                        "workflow",
                        "fork",
                        pointer.workflow_id,
                        "--name",
                        "bad pointer",
                        "--project",
                        str(malformed_pointer_project),
                        "--json",
                    ),
                    "malformed_current_pointer",
                ),
            ]

            for result, expected_status in cases:
                with self.subTest(status=expected_status):
                    self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], expected_status)
                    self.assertIn("recovery_actions", payload)

    def test_workflow_fork_allows_archived_and_read_only_sources_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Workflow fork immutable source fixture.", layout=LAYOUT_CANONICAL_V16)
            read_only_id = "wf_20260611_fafafafa"
            archive_workflow(project, initialized.workflow_id, reason="fork archived source", updated_by="test")
            import_workflow_record(
                project,
                workflow_id=read_only_id,
                name="read-only imported fork target",
                workflow_root=f".loopplane/imported/{read_only_id}",
                updated_by="test",
            )
            source_root = project / ".loopplane" / "workflows" / initialized.workflow_id
            before_hashes = {
                source_root / "PROJECT_BRIEF.md": file_sha256(source_root / "PROJECT_BRIEF.md"),
                source_root / "PLAN.md": file_sha256(source_root / "PLAN.md"),
                source_root / "config" / "workflow.json": file_sha256(source_root / "config" / "workflow.json"),
            }
            read_only_before = registry_record(project, read_only_id)

            archived = run_loopplane(
                "workflow",
                "fork",
                initialized.workflow_id,
                "--name",
                "Archived source fork",
                "--project",
                str(project),
                "--json",
            )
            read_only = run_loopplane(
                "workflow",
                "fork",
                read_only_id,
                "--name",
                "Read-only source fork",
                "--project",
                str(project),
                "--json",
            )

            self.assertEqual(archived.returncode, EXIT_SUCCESS, archived.stderr + archived.stdout)
            archived_payload = json.loads(archived.stdout)
            self.assertEqual(archived_payload["source_workflow"]["status"], "archived")
            self.assertTrue(archived_payload["source_workflow"]["archived"])
            archived_fork_id = archived_payload["workflow_id"]
            self.assertEqual(registry_record(project, archived_fork_id)["status"], "forked")
            self.assertFalse(registry_record(project, archived_fork_id)["read_only"])
            self.assertFalse(registry_record(project, archived_fork_id)["archived"])

            self.assertEqual(read_only.returncode, EXIT_SUCCESS, read_only.stderr + read_only.stdout)
            read_only_payload = json.loads(read_only.stdout)
            self.assertEqual(read_only_payload["source_workflow"]["status"], "read_only_imported")
            self.assertTrue(read_only_payload["source_workflow"]["read_only"])
            read_only_fork_id = read_only_payload["workflow_id"]
            self.assertEqual(registry_record(project, read_only_fork_id)["status"], "forked")
            self.assertFalse(registry_record(project, read_only_fork_id)["read_only"])
            self.assertFalse(registry_record(project, read_only_fork_id)["archived"])

            self.assertEqual(registry_record(project, initialized.workflow_id)["status"], "archived")
            self.assertTrue(registry_record(project, initialized.workflow_id)["archived"])
            self.assertEqual(registry_record(project, read_only_id), read_only_before)
            self.assertEqual({path: file_sha256(path) for path in before_hashes}, before_hashes)

    def test_workflow_fork_rejects_running_lock_and_active_lease_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            running_project = root / "running"
            lock_project = root / "lock"
            lease_project = root / "lease"
            running = init_project(running_project, "Workflow fork running fixture.", layout=LAYOUT_CANONICAL_V16)
            locked = init_project(lock_project, "Workflow fork lock fixture.", layout=LAYOUT_CANONICAL_V16)
            leased = init_project(lease_project, "Workflow fork lease fixture.", layout=LAYOUT_CANONICAL_V16)

            registry = read_json(running_project / ".loopplane" / "workflow_registry.json")
            registry["workflows"][0]["status"] = "running"
            write_json(running_project / ".loopplane" / "workflow_registry.json", registry)
            running_before = {
                path: path.read_bytes()
                for path in (
                    running_project / ".loopplane" / "workspace.json",
                    running_project / ".loopplane" / "workflow_registry.json",
                    running_project / ".loopplane" / "current_workflow.json",
                )
            }
            running_result = run_loopplane(
                "workflow",
                "fork",
                running.workflow_id,
                "--name",
                "running fork",
                "--project",
                str(running_project),
                "--json",
            )

            self.assertEqual(running_result.returncode, EXIT_INVALID_CONFIG, running_result.stderr + running_result.stdout)
            running_payload = json.loads(running_result.stdout)
            self.assertEqual(running_payload["status"], "active_running_conflict")
            self.assertIn("active_running_conflict", {blocker["code"] for blocker in running_payload["safety"]["blockers"]})
            self.assertEqual({path: path.read_bytes() for path in running_before}, running_before)

            lock_before = (lock_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lock = AtomicOwnerLock(
                lock_project
                / ".loopplane"
                / "workflows"
                / locked.workflow_id
                / "runtime"
                / "lock"
                / "scheduler_instance_lock",
                "test-owner",
            )
            with lock.acquire():
                lock_result = run_loopplane(
                    "workflow",
                    "fork",
                    locked.workflow_id,
                    "--name",
                    "locked fork",
                    "--project",
                    str(lock_project),
                    "--json",
                )

            self.assertEqual(lock_result.returncode, EXIT_INVALID_CONFIG, lock_result.stderr + lock_result.stdout)
            lock_payload = json.loads(lock_result.stdout)
            self.assertEqual(lock_payload["status"], "scheduler_lock_conflict")
            self.assertIn("active_scheduler_lock", {blocker["code"] for blocker in lock_payload["safety"]["blockers"]})
            self.assertEqual((lock_project / ".loopplane" / "workflow_registry.json").read_bytes(), lock_before)

            lease_dir = lease_project / ".loopplane" / "workflows" / leased.workflow_id / "runtime" / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                lease_dir / "run_active.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": leased.workflow_id,
                    "run_id": "run_active",
                    "status": "running",
                    "heartbeat_at": future_timestamp(),
                    "lease_expires_at": future_timestamp(),
                },
            )
            lease_before = (lease_project / ".loopplane" / "workflow_registry.json").read_bytes()
            lease_result = run_loopplane(
                "workflow",
                "fork",
                leased.workflow_id,
                "--name",
                "leased fork",
                "--project",
                str(lease_project),
                "--json",
            )

            self.assertEqual(lease_result.returncode, EXIT_INVALID_CONFIG, lease_result.stderr + lease_result.stdout)
            lease_payload = json.loads(lease_result.stdout)
            self.assertEqual(lease_payload["status"], "active_run_conflict")
            self.assertIn("active_run_lease", {blocker["code"] for blocker in lease_payload["safety"]["blockers"]})
            self.assertEqual((lease_project / ".loopplane" / "workflow_registry.json").read_bytes(), lease_before)


if __name__ == "__main__":
    unittest.main()
