from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.loopplane_home import loopplane_home_layout
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SECURITY_POLICY_VIOLATION, EXIT_SUCCESS
from runtime.init_workflow import LAYOUT_CANONICAL_V16, LAYOUT_COMPATIBILITY_FLAT, init_project


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def run_loopplane(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        check=False,
    )


class WorkspaceCommandCliTest(unittest.TestCase):
    def test_workspace_group_help_and_unknown_subcommand_behavior(self) -> None:
        help_result = run_loopplane("workspace", "--help")

        self.assertEqual(help_result.returncode, EXIT_SUCCESS, help_result.stderr + help_result.stdout)
        self.assertIn("usage: loopplane workspace", help_result.stdout)
        self.assertIn("current", help_result.stdout)
        self.assertIn("register", help_result.stdout)
        self.assertIn("unregister", help_result.stdout)
        self.assertIn("scan", help_result.stdout)
        self.assertIn("list", help_result.stdout)
        self.assertIn("doctor", help_result.stdout)

        current_help = run_loopplane("workspace", "current", "--help")

        self.assertEqual(current_help.returncode, EXIT_SUCCESS, current_help.stderr + current_help.stdout)
        self.assertIn("--project", current_help.stdout)
        self.assertIn("--json", current_help.stdout)

        register_help = run_loopplane("workspace", "register", "--help")

        self.assertEqual(register_help.returncode, EXIT_SUCCESS, register_help.stderr + register_help.stdout)
        self.assertIn("<project>", register_help.stdout)
        self.assertIn("--json", register_help.stdout)

        unregister_help = run_loopplane("workspace", "unregister", "--help")

        self.assertEqual(unregister_help.returncode, EXIT_SUCCESS, unregister_help.stderr + unregister_help.stdout)
        self.assertIn("<workspace_id>", unregister_help.stdout)
        self.assertIn("--json", unregister_help.stdout)

        scan_help = run_loopplane("workspace", "scan", "--help")

        self.assertEqual(scan_help.returncode, EXIT_SUCCESS, scan_help.stderr + scan_help.stdout)
        self.assertIn("<directory>", scan_help.stdout)
        self.assertIn("--json", scan_help.stdout)

        list_help = run_loopplane("workspace", "list", "--help")

        self.assertEqual(list_help.returncode, EXIT_SUCCESS, list_help.stderr + list_help.stdout)
        self.assertIn("--json", list_help.stdout)

        doctor_help = run_loopplane("workspace", "doctor", "--help")

        self.assertEqual(doctor_help.returncode, EXIT_SUCCESS, doctor_help.stderr + doctor_help.stdout)
        self.assertIn("--project", doctor_help.stdout)
        self.assertIn("--json", doctor_help.stdout)

        unknown = run_loopplane("workspace", "missing-command")

        self.assertEqual(unknown.returncode, EXIT_INVALID_CONFIG, unknown.stderr + unknown.stdout)
        self.assertIn("invalid choice", unknown.stderr)

    def test_workspace_current_json_reads_canonical_v16_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Canonical workspace current fixture.", layout=LAYOUT_CANONICAL_V16)

            result = run_loopplane("workspace", "current", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "current")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["workspace_project_root"], ".")
            self.assertEqual(payload["repo_root"], ".")
            self.assertEqual(payload["resolved_project_root"], project.resolve().as_posix())
            self.assertEqual(payload["resolved_repo_root"], project.resolve().as_posix())
            self.assertEqual(payload["workspace_boundary"], "project_root")
            self.assertEqual(payload["resolved_workspace_boundary"], project.resolve().as_posix())
            self.assertFalse(payload["allow_out_of_boundary_writes"])
            self.assertEqual(payload["workspace_identity"]["repo_root"], ".")
            self.assertEqual(payload["workspace_identity"]["workspace_boundary"], "project_root")
            self.assertEqual(payload["workspace_identity"]["resolved_workspace_boundary"], project.resolve().as_posix())
            self.assertEqual(payload["workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")
            self.assertEqual(
                payload["workflow_config_file"],
                f".loopplane/workflows/{initialized.workflow_id}/config/workflow.json",
            )
            self.assertEqual(
                payload["workflow_paths"]["runtime_dir"],
                f".loopplane/workflows/{initialized.workflow_id}/runtime",
            )
            self.assertEqual(payload["workflow_count"], 1)

    def test_workspace_current_json_materializes_v15_flat_compatibility_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Flat compatibility current fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                (project / relative).unlink()

            result = run_loopplane("workspace", "current", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "current")
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["workflow_root"], ".loopplane")
            self.assertEqual(payload["workflow_config_file"], ".loopplane/config/workflow.json")
            self.assertEqual(payload["workflow_paths"]["runtime_dir"], ".loopplane/runtime")
            self.assertEqual(payload["compatibility_metadata"]["status"], "created")
            self.assertEqual(
                sorted(payload["compatibility_metadata"]["created"]),
                [
                    ".loopplane/current_workflow.json",
                    ".loopplane/workflow_registry.json",
                    ".loopplane/workspace.json",
                ],
            )
            for relative in payload["compatibility_metadata"]["created"]:
                self.assertTrue((project / relative).is_file())

    def test_workspace_current_json_reports_missing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result = run_loopplane("workspace", "current", "--project", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "missing_workspace")
            self.assertIn("recovery_actions", payload)

    def test_workspace_register_json_upserts_loopplane_home_registry_without_mutating_project_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace register fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workspace", "register", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "registered")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["loopplane_home"], loopplane_home.resolve().as_posix())
            self.assertEqual(payload["registry_file"], (loopplane_home / "registry" / "workspaces.json").resolve().as_posix())
            self.assertEqual(payload["registry_count"], 1)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            registry = json.loads((loopplane_home / "registry" / "workspaces.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["schema_version"], "1.6")
            self.assertEqual(registry["authority"], "discovery_only")
            self.assertEqual(len(registry["workspaces"]), 1)
            entry = registry["workspaces"][0]
            self.assertEqual(entry["workspace_id"], initialized.workspace_id)
            self.assertEqual(entry["project_root"], project.resolve().as_posix())
            self.assertEqual(entry["loopplane_dir"], (project / ".loopplane").resolve().as_posix())
            self.assertEqual(entry["repo_root"], project.resolve().as_posix())
            self.assertEqual(entry["status"], "registered")
            self.assertEqual(entry["current_workflow_id"], initialized.workflow_id)

            layout = loopplane_home_layout(loopplane_home)
            self.assertTrue(layout.runner_locks_dir.is_dir())
            self.assertTrue(layout.dashboard_locks_dir.is_dir())
            self.assertTrue(layout.package_cache_dir.is_dir())
            self.assertTrue(layout.logs_dir.is_dir())
            self.assertEqual(
                json.loads(layout.config_file.read_text(encoding="utf-8")),
                {"authority": "discovery_only", "schema_version": "1.6"},
            )
            self.assertEqual(
                json.loads(layout.agent_runners_local_file.read_text(encoding="utf-8")),
                {"schema_version": "1.6", "runners": {}},
            )
            self.assertEqual(
                json.loads(layout.dashboard_servers_file.read_text(encoding="utf-8")),
                {"schema_version": "1.6", "servers": []},
            )

    def test_workspace_register_is_idempotent_and_preserves_dashboard_registry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace register idempotency fixture.")

            first = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(first.returncode, EXIT_SUCCESS, first.stderr + first.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workspaces"][0]["dashboard"] = {"last_port": 3767, "last_url": "http://127.0.0.1:3767"}
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            second = run_loopplane("workspace", "register", str(project), "--json", env=env)

            self.assertEqual(second.returncode, EXIT_SUCCESS, second.stderr + second.stdout)
            payload = json.loads(second.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "updated")
            self.assertEqual(payload["replaced_count"], 1)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(len(registry["workspaces"]), 1)
            entry = registry["workspaces"][0]
            self.assertEqual(entry["workspace_id"], initialized.workspace_id)
            self.assertEqual(entry["dashboard"], {"last_port": 3767, "last_url": "http://127.0.0.1:3767"})

    def test_workspace_register_rejects_missing_and_uninitialized_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            uninitialized = root / "uninitialized"
            uninitialized.mkdir()
            missing = root / "missing"

            uninitialized_result = run_loopplane("workspace", "register", str(uninitialized), "--json", env=env)
            missing_result = run_loopplane("workspace", "register", str(missing), "--json", env=env)

            self.assertEqual(uninitialized_result.returncode, EXIT_INVALID_CONFIG, uninitialized_result.stderr + uninitialized_result.stdout)
            uninitialized_payload = json.loads(uninitialized_result.stdout)
            self.assertFalse(uninitialized_payload["ok"])
            self.assertEqual(uninitialized_payload["status"], "missing_workspace")
            self.assertFalse((loopplane_home / "registry" / "workspaces.json").exists())

            self.assertEqual(missing_result.returncode, EXIT_INVALID_CONFIG, missing_result.stderr + missing_result.stdout)
            missing_payload = json.loads(missing_result.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "missing_project")
            self.assertFalse((loopplane_home / "registry" / "workspaces.json").exists())

    def test_workspace_unregister_removes_registry_entry_without_mutating_project_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace unregister fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_other_registered_workspace",
                    "name": "other",
                    "project_root": "/tmp/other",
                    "loopplane_dir": "/tmp/other/.loopplane",
                    "repo_root": "/tmp/other",
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": "wf_other",
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_loopplane("workspace", "unregister", initialized.workspace_id, "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "unregistered")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["removed_count"], 1)
            self.assertEqual(payload["registry_count"], 1)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            updated_registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["workspace_id"] for entry in updated_registry["workspaces"]], ["ws_other_registered_workspace"])

            current = run_loopplane("workspace", "current", "--project", str(project), "--json", env=env)
            self.assertEqual(current.returncode, EXIT_SUCCESS, current.stderr + current.stdout)
            current_payload = json.loads(current.stdout)
            self.assertEqual(current_payload["workspace_id"], initialized.workspace_id)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workspace_unregister_rejects_unknown_malformed_and_missing_workspace_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            init_project(project, "Workspace unregister invalid fixture.", layout=LAYOUT_CANONICAL_V16)
            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            before = registry_path.read_bytes()

            unknown = run_loopplane("workspace", "unregister", "ws_unknown_missing", "--json", env=env)
            malformed = run_loopplane("workspace", "unregister", "not-a-workspace-id", "--json", env=env)
            missing = run_loopplane("workspace", "unregister", "--json", env=env)

            self.assertEqual(unknown.returncode, EXIT_INVALID_CONFIG, unknown.stderr + unknown.stdout)
            unknown_payload = json.loads(unknown.stdout)
            self.assertFalse(unknown_payload["ok"])
            self.assertEqual(unknown_payload["status"], "not_registered")
            self.assertEqual(unknown_payload["registry_count"], 1)

            self.assertEqual(malformed.returncode, EXIT_INVALID_CONFIG, malformed.stderr + malformed.stdout)
            malformed_payload = json.loads(malformed.stdout)
            self.assertFalse(malformed_payload["ok"])
            self.assertEqual(malformed_payload["status"], "invalid_workspace_id")

            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            missing_payload = json.loads(missing.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "missing_workspace_id")
            self.assertEqual(registry_path.read_bytes(), before)

    def test_workspace_scan_rebuilds_registry_scope_without_mutating_project_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_root = root / "scan-root"
            project = scan_root / "project"
            flat_project = scan_root / "flat-project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace scan fixture.", layout=LAYOUT_CANONICAL_V16)
            init_project(flat_project, "Flat scan fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                (flat_project / relative).unlink()
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workspaces"][0]["dashboard"] = {"last_port": 3767, "last_url": "http://127.0.0.1:3767"}
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_stale_registered_workspace",
                    "name": "stale",
                    "project_root": (scan_root / "stale").resolve().as_posix(),
                    "loopplane_dir": (scan_root / "stale" / ".loopplane").resolve().as_posix(),
                    "repo_root": (scan_root / "stale").resolve().as_posix(),
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": "wf_stale",
                }
            )
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_outside_registered_workspace",
                    "name": "outside",
                    "project_root": (root / "outside").resolve().as_posix(),
                    "loopplane_dir": (root / "outside" / ".loopplane").resolve().as_posix(),
                    "repo_root": (root / "outside").resolve().as_posix(),
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": "wf_outside",
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_loopplane("workspace", "scan", str(scan_root), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "scanned")
            self.assertEqual(payload["discovered_count"], 1)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["workspaces"][0]["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["workspaces"][0]["project_root"], project.resolve().as_posix())
            self.assertEqual(payload["workspaces"][0]["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["skipped"][0]["status"], "missing_project_local_truth")
            self.assertEqual(payload["skipped"][0]["project_root"], flat_project.resolve().as_posix())
            self.assertEqual(payload["registry_update"]["status"], "rebuilt_scan_scope")
            self.assertEqual(payload["registry_update"]["previous_registry_count"], 3)
            self.assertEqual(payload["registry_update"]["registry_count"], 2)
            self.assertEqual(payload["registry_update"]["removed_stale_count"], 1)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                self.assertFalse((flat_project / relative).exists())

            updated_registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_registry["authority"], "discovery_only")
            updated_by_id = {entry["workspace_id"]: entry for entry in updated_registry["workspaces"]}
            self.assertEqual(set(updated_by_id), {initialized.workspace_id, "ws_outside_registered_workspace"})
            self.assertEqual(
                updated_by_id[initialized.workspace_id]["dashboard"],
                {"last_port": 3767, "last_url": "http://127.0.0.1:3767"},
            )
            self.assertEqual(updated_by_id[initialized.workspace_id]["current_workflow_id"], initialized.workflow_id)

    def test_workspace_scan_accepts_multiple_roots_and_rebuilds_deleted_registry_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_root_a = root / "scan-root-a"
            scan_root_b = root / "scan-root-b"
            project_a = scan_root_a / "project-a"
            project_b = scan_root_b / "nested" / "project-b"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized_a = init_project(project_a, "Workspace scan multi-root fixture A.", layout=LAYOUT_CANONICAL_V16)
            initialized_b = init_project(project_b, "Workspace scan multi-root fixture B.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project_a / ".loopplane" / "workspace.json",
                project_a / ".loopplane" / "workflow_registry.json",
                project_a / ".loopplane" / "current_workflow.json",
                project_b / ".loopplane" / "workspace.json",
                project_b / ".loopplane" / "workflow_registry.json",
                project_b / ".loopplane" / "current_workflow.json",
            ]
            before_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authoritative_files}

            register = run_loopplane("workspace", "register", str(project_a), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            self.assertTrue(registry_path.is_file())
            registry_path.unlink()

            result = run_loopplane(
                "workspace",
                "scan",
                str(scan_root_a),
                str(scan_root_b),
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "scanned")
            self.assertEqual(
                payload["scan_roots"],
                [scan_root_a.resolve().as_posix(), scan_root_b.resolve().as_posix()],
            )
            self.assertEqual(payload["discovered_count"], 2)
            self.assertEqual(payload["skipped_count"], 0)
            self.assertEqual(payload["registry_update"]["status"], "rebuilt_scan_scope")
            self.assertEqual(payload["registry_update"]["mode"], "scan_scopes")
            self.assertEqual(payload["registry_update"]["previous_registry_count"], 0)
            self.assertEqual(payload["registry_update"]["registry_count"], 2)
            self.assertEqual(payload["registry_update"]["removed_stale_count"], 0)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authoritative_files},
                before_hashes,
            )

            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["authority"], "discovery_only")
            by_id = {entry["workspace_id"]: entry for entry in registry["workspaces"]}
            self.assertEqual(set(by_id), {initialized_a.workspace_id, initialized_b.workspace_id})
            self.assertEqual(by_id[initialized_a.workspace_id]["project_root"], project_a.resolve().as_posix())
            self.assertEqual(by_id[initialized_b.workspace_id]["project_root"], project_b.resolve().as_posix())

            listed = run_loopplane("workspace", "list", "--json", env=env)
            self.assertEqual(listed.returncode, EXIT_SUCCESS, listed.stderr + listed.stdout)
            list_payload = json.loads(listed.stdout)
            self.assertEqual(list_payload["workspace_count"], 2)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authoritative_files},
                before_hashes,
            )

    def test_workspace_commands_detect_nested_instances_without_flagging_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent-project"
            child = parent / "services" / "child-project"
            sibling = root / "sibling-project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            parent_initialized = init_project(parent, "Parent workspace.", layout=LAYOUT_CANONICAL_V16)
            child_initialized = init_project(child, "Child workspace.", layout=LAYOUT_CANONICAL_V16)
            sibling_initialized = init_project(sibling, "Sibling workspace.", layout=LAYOUT_CANONICAL_V16)

            parent_current = run_loopplane("workspace", "current", "--project", str(parent), "--json", env=env)
            child_current = run_loopplane("workspace", "current", "--project", str(child), "--json", env=env)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry_before_blocked_scan = registry_path.read_bytes() if registry_path.exists() else None
            blocked_scan = run_loopplane("workspace", "scan", str(root), "--json", env=env)

            self.assertEqual(parent_current.returncode, EXIT_SUCCESS, parent_current.stderr + parent_current.stdout)
            parent_payload = json.loads(parent_current.stdout)
            self.assertEqual(parent_payload["nested_workspaces"]["status"], "nested_detected")
            self.assertEqual(parent_payload["nested_workspaces"]["parent_count"], 0)
            self.assertEqual(parent_payload["nested_workspaces"]["child_count"], 1)
            self.assertEqual(
                parent_payload["nested_workspaces"]["children"][0]["workspace_id"],
                child_initialized.workspace_id,
            )
            self.assertIn("Nested LoopPlane child workspace detected", parent_payload["warnings"][0])

            self.assertEqual(child_current.returncode, EXIT_SUCCESS, child_current.stderr + child_current.stdout)
            child_payload = json.loads(child_current.stdout)
            self.assertEqual(child_payload["nested_workspaces"]["status"], "nested_detected")
            self.assertEqual(child_payload["nested_workspaces"]["parent_count"], 1)
            self.assertEqual(child_payload["nested_workspaces"]["child_count"], 0)
            self.assertEqual(
                child_payload["nested_workspaces"]["parents"][0]["workspace_id"],
                parent_initialized.workspace_id,
            )
            self.assertIn("Nested LoopPlane parent workspace detected", child_payload["warnings"][0])

            self.assertEqual(blocked_scan.returncode, EXIT_SECURITY_POLICY_VIOLATION, blocked_scan.stderr + blocked_scan.stdout)
            blocked_payload = json.loads(blocked_scan.stdout)
            self.assertFalse(blocked_payload["ok"])
            self.assertEqual(blocked_payload["status"], "nested_workspace_requires_explicit_namespace")
            self.assertEqual(blocked_payload["registry_update"]["status"], "blocked_nested_workspace")
            registry_after_blocked_scan = registry_path.read_bytes() if registry_path.exists() else None
            self.assertEqual(registry_after_blocked_scan, registry_before_blocked_scan)

            scan = run_loopplane("workspace", "scan", str(root), "--allow-nested-workspace", "--json", env=env)
            parent_doctor = run_loopplane("workspace", "doctor", "--project", str(parent), "--json", env=env)
            child_doctor = run_loopplane("workspace", "doctor", "--project", str(child), "--json", env=env)
            sibling_doctor = run_loopplane("workspace", "doctor", "--project", str(sibling), "--json", env=env)

            self.assertEqual(scan.returncode, EXIT_SUCCESS, scan.stderr + scan.stdout)
            scan_payload = json.loads(scan.stdout)
            self.assertEqual(scan_payload["discovered_count"], 3)
            self.assertEqual(scan_payload["nested_workspace_count"], 2)
            by_project = {workspace["project_root"]: workspace for workspace in scan_payload["workspaces"]}
            self.assertEqual(by_project[parent.resolve().as_posix()]["nested_workspace_count"], 1)
            self.assertEqual(by_project[child.resolve().as_posix()]["nested_workspace_count"], 1)
            self.assertEqual(by_project[sibling.resolve().as_posix()]["nested_workspace_count"], 0)
            self.assertEqual(by_project[sibling.resolve().as_posix()]["workspace_id"], sibling_initialized.workspace_id)
            self.assertIn("Nested LoopPlane child workspace detected", "\n".join(scan_payload["warnings"]))
            self.assertIn("Nested LoopPlane parent workspace detected", "\n".join(scan_payload["warnings"]))

            self.assertEqual(parent_doctor.returncode, EXIT_SUCCESS, parent_doctor.stderr + parent_doctor.stdout)
            parent_doctor_payload = json.loads(parent_doctor.stdout)
            self.assertEqual(parent_doctor_payload["status"], "warning")
            self.assertEqual(parent_doctor_payload["project"]["nested_workspaces"]["child_count"], 1)
            self.assertIn(
                "nested_child_workspace",
                {issue["code"] for issue in parent_doctor_payload["issues"]},
            )

            self.assertEqual(child_doctor.returncode, EXIT_SUCCESS, child_doctor.stderr + child_doctor.stdout)
            child_doctor_payload = json.loads(child_doctor.stdout)
            self.assertEqual(child_doctor_payload["status"], "warning")
            self.assertEqual(child_doctor_payload["project"]["nested_workspaces"]["parent_count"], 1)
            self.assertIn(
                "nested_parent_workspace",
                {issue["code"] for issue in child_doctor_payload["issues"]},
            )

            self.assertEqual(sibling_doctor.returncode, EXIT_SUCCESS, sibling_doctor.stderr + sibling_doctor.stdout)
            sibling_doctor_payload = json.loads(sibling_doctor.stdout)
            self.assertEqual(sibling_doctor_payload["status"], "healthy")
            self.assertEqual(sibling_doctor_payload["project"]["nested_workspaces"]["nested_workspace_count"], 0)
            self.assertNotIn(
                "nested_child_workspace",
                {issue["code"] for issue in sibling_doctor_payload["issues"]},
            )
            self.assertNotIn(
                "nested_parent_workspace",
                {issue["code"] for issue in sibling_doctor_payload["issues"]},
            )

    def test_nested_workspace_operations_require_namespace_or_explicit_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent-project"
            child = parent / "services" / "child-project"
            sibling = root / "sibling-project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            parent_initialized = init_project(parent, "Parent workspace.", layout=LAYOUT_CANONICAL_V16)
            init_project(child, "Child workspace.", layout=LAYOUT_CANONICAL_V16)
            init_project(sibling, "Sibling workspace.", layout=LAYOUT_CANONICAL_V16)

            current_default = run_loopplane("workspace", "current", "--json", env=env, cwd=parent)
            workflow_default = run_loopplane("workflow", "current", "--json", env=env, cwd=parent)
            control_requests = (
                parent
                / ".loopplane"
                / "workflows"
                / parent_initialized.workflow_id
                / "runtime"
                / "control_requests.jsonl"
            )
            control_requests_before_blocked_start = control_requests.read_bytes() if control_requests.exists() else None
            start_default = run_loopplane("start", "--json", env=env, cwd=parent)
            checkpoint_default = run_loopplane(
                "vc",
                "checkpoint",
                "--reason",
                "nested-default-denied",
                "--json",
                env=env,
                cwd=parent,
            )

            for result in (current_default, workflow_default, start_default, checkpoint_default):
                with self.subTest(command=result.args):
                    self.assertEqual(result.returncode, EXIT_SECURITY_POLICY_VIOLATION, result.stderr + result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(payload["status"], "nested_workspace_requires_explicit_namespace")
                    self.assertEqual(payload["nested_workspaces"]["nested_workspace_count"], 1)
                    self.assertIn(parent_initialized.workspace_id, payload["valid_namespaces"])

            control_requests_after_blocked_start = control_requests.read_bytes() if control_requests.exists() else None
            self.assertEqual(control_requests_after_blocked_start, control_requests_before_blocked_start)

            namespaced_current = run_loopplane(
                "workspace",
                "current",
                "--workspace-namespace",
                parent_initialized.workspace_id,
                "--json",
                env=env,
                cwd=parent,
            )
            namespaced_start = run_loopplane(
                "start",
                "--workspace-namespace",
                parent_initialized.workspace_id,
                "--json",
                env=env,
                cwd=parent,
            )
            explicit_current = run_loopplane("workflow", "current", "--project", str(parent), "--json", env=env)
            sibling_start = run_loopplane("start", "--json", env=env, cwd=sibling)

            self.assertEqual(namespaced_current.returncode, EXIT_SUCCESS, namespaced_current.stderr + namespaced_current.stdout)
            self.assertEqual(json.loads(namespaced_current.stdout)["workspace_id"], parent_initialized.workspace_id)
            self.assertEqual(namespaced_start.returncode, EXIT_SUCCESS, namespaced_start.stderr + namespaced_start.stdout)
            self.assertEqual(json.loads(namespaced_start.stdout)["status"], "pending")
            self.assertEqual(explicit_current.returncode, EXIT_SUCCESS, explicit_current.stderr + explicit_current.stdout)
            self.assertEqual(json.loads(explicit_current.stdout)["workspace_id"], parent_initialized.workspace_id)
            self.assertEqual(sibling_start.returncode, EXIT_SUCCESS, sibling_start.stderr + sibling_start.stdout)

    def test_workspace_scan_reports_empty_missing_and_invalid_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty"
            empty.mkdir()
            invalid_file = root / "not-a-directory.txt"
            invalid_file.write_text("not a directory\n", encoding="utf-8")
            missing = root / "missing"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspaces": [
                            {
                                "workspace_id": "ws_empty_stale_workspace",
                                "name": "stale",
                                "project_root": (empty / "stale").resolve().as_posix(),
                                "loopplane_dir": (empty / "stale" / ".loopplane").resolve().as_posix(),
                                "repo_root": (empty / "stale").resolve().as_posix(),
                                "status": "registered",
                                "last_seen_at": "2026-06-11T00:00:00Z",
                                "current_workflow_id": "wf_stale",
                            },
                            {
                                "workspace_id": "ws_preserved_outside_workspace",
                                "name": "outside",
                                "project_root": (root / "outside").resolve().as_posix(),
                                "loopplane_dir": (root / "outside" / ".loopplane").resolve().as_posix(),
                                "repo_root": (root / "outside").resolve().as_posix(),
                                "status": "registered",
                                "last_seen_at": "2026-06-11T00:00:00Z",
                                "current_workflow_id": "wf_outside",
                            },
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            empty_result = run_loopplane("workspace", "scan", str(empty), "--json", env=env)
            after_empty = registry_path.read_bytes()
            missing_result = run_loopplane("workspace", "scan", str(missing), "--json", env=env)
            invalid_result = run_loopplane("workspace", "scan", str(invalid_file), "--json", env=env)

            self.assertEqual(empty_result.returncode, EXIT_SUCCESS, empty_result.stderr + empty_result.stdout)
            empty_payload = json.loads(empty_result.stdout)
            self.assertTrue(empty_payload["ok"])
            self.assertEqual(empty_payload["discovered_count"], 0)
            self.assertEqual(empty_payload["skipped_count"], 0)
            self.assertEqual(empty_payload["registry_update"]["removed_stale_count"], 1)
            self.assertEqual(empty_payload["registry_update"]["registry_count"], 1)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["workspace_id"] for entry in registry["workspaces"]], ["ws_preserved_outside_workspace"])

            self.assertEqual(missing_result.returncode, EXIT_INVALID_CONFIG, missing_result.stderr + missing_result.stdout)
            missing_payload = json.loads(missing_result.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "missing_scan_directory")

            self.assertEqual(invalid_result.returncode, EXIT_INVALID_CONFIG, invalid_result.stderr + invalid_result.stdout)
            invalid_payload = json.loads(invalid_result.stdout)
            self.assertFalse(invalid_payload["ok"])
            self.assertEqual(invalid_payload["status"], "invalid_scan_directory")
            self.assertEqual(registry_path.read_bytes(), after_empty)

    def test_workspace_list_reports_registered_canonical_and_flat_workspaces_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            flat = root / "flat"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            canonical_initialized = init_project(canonical, "Workspace list canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            flat_initialized = init_project(flat, "Workspace list flat fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)

            canonical_register = run_loopplane("workspace", "register", str(canonical), "--json", env=env)
            flat_register = run_loopplane("workspace", "register", str(flat), "--json", env=env)

            self.assertEqual(canonical_register.returncode, EXIT_SUCCESS, canonical_register.stderr + canonical_register.stdout)
            self.assertEqual(flat_register.returncode, EXIT_SUCCESS, flat_register.stderr + flat_register.stdout)
            authoritative_files = [
                canonical / ".loopplane" / "workspace.json",
                canonical / ".loopplane" / "workflow_registry.json",
                canonical / ".loopplane" / "current_workflow.json",
                flat / ".loopplane" / "workspace.json",
                flat / ".loopplane" / "workflow_registry.json",
                flat / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workspace", "list", "--json", env=env)
            text_result = run_loopplane("workspace", "list", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "listed")
            self.assertEqual(payload["registry_authority"], "discovery_only")
            self.assertEqual(payload["workspace_count"], 2)
            self.assertEqual(payload["available_count"], 2)
            self.assertEqual(payload["stale_count"], 0)
            by_id = {workspace["workspace_id"]: workspace for workspace in payload["workspaces"]}
            self.assertEqual(set(by_id), {canonical_initialized.workspace_id, flat_initialized.workspace_id})
            self.assertEqual(by_id[canonical_initialized.workspace_id]["health"]["status"], "ok")
            self.assertEqual(by_id[canonical_initialized.workspace_id]["current_workflow_id"], canonical_initialized.workflow_id)
            self.assertEqual(
                by_id[canonical_initialized.workspace_id]["health"]["project_local_workflow_root"],
                f".loopplane/workflows/{canonical_initialized.workflow_id}",
            )
            self.assertEqual(by_id[flat_initialized.workspace_id]["health"]["status"], "ok")
            self.assertEqual(by_id[flat_initialized.workspace_id]["current_workflow_id"], flat_initialized.workflow_id)
            self.assertIn(
                by_id[flat_initialized.workspace_id]["health"]["project_local_workflow_root"],
                {".loopplane", ".loopplane/"},
            )
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text_result.returncode, EXIT_SUCCESS, text_result.stderr + text_result.stdout)
            self.assertIn("loopplane workspace list: listed", text_result.stdout)
            self.assertIn("registry_authority: discovery_only", text_result.stdout)
            self.assertIn(f"workspace_count: {payload['workspace_count']}", text_result.stdout)
            self.assertIn(canonical_initialized.workspace_id, text_result.stdout)
            self.assertIn(flat_initialized.workspace_id, text_result.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workspace_list_reports_empty_registry_and_stale_paths_without_writing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            empty_result = run_loopplane("workspace", "list", "--json", env=env)

            self.assertEqual(empty_result.returncode, EXIT_SUCCESS, empty_result.stderr + empty_result.stdout)
            empty_payload = json.loads(empty_result.stdout)
            self.assertTrue(empty_payload["ok"])
            self.assertFalse(empty_payload["registry_exists"])
            self.assertEqual(empty_payload["registry_authority"], "discovery_only")
            self.assertEqual(empty_payload["workspace_count"], 0)
            self.assertEqual(empty_payload["workspaces"], [])
            self.assertFalse((loopplane_home / "registry" / "workspaces.json").exists())

            partial_project = root / "partial"
            (partial_project / ".loopplane").mkdir(parents=True)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspaces": [
                            {
                                "workspace_id": "ws_missing_registered_workspace",
                                "name": "missing",
                                "project_root": (root / "missing").resolve().as_posix(),
                                "loopplane_dir": (root / "missing" / ".loopplane").resolve().as_posix(),
                                "repo_root": (root / "missing").resolve().as_posix(),
                                "status": "registered",
                                "last_seen_at": "2026-06-11T00:00:00Z",
                                "current_workflow_id": "wf_missing",
                            },
                            {
                                "workspace_id": "ws_partial_registered_workspace",
                                "name": "partial",
                                "project_root": partial_project.resolve().as_posix(),
                                "loopplane_dir": (partial_project / ".loopplane").resolve().as_posix(),
                                "repo_root": partial_project.resolve().as_posix(),
                                "status": "registered",
                                "last_seen_at": "2026-06-11T00:00:00Z",
                                "current_workflow_id": "wf_partial",
                            },
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            before = registry_path.read_bytes()

            stale_result = run_loopplane("workspace", "list", "--json", env=env)

            self.assertEqual(stale_result.returncode, EXIT_SUCCESS, stale_result.stderr + stale_result.stdout)
            payload = json.loads(stale_result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["workspace_count"], 2)
            self.assertEqual(payload["available_count"], 0)
            self.assertEqual(payload["stale_count"], 2)
            self.assertEqual(payload["missing_count"], 1)
            by_id = {workspace["workspace_id"]: workspace for workspace in payload["workspaces"]}
            self.assertEqual(by_id["ws_missing_registered_workspace"]["health"]["status"], "missing_project")
            self.assertEqual(by_id["ws_partial_registered_workspace"]["health"]["status"], "missing_project_local_truth")
            self.assertIn(".loopplane/workspace.json", by_id["ws_partial_registered_workspace"]["health"]["missing_files"])
            self.assertEqual(registry_path.read_bytes(), before)

    def test_loopplane_home_discovery_layer_handles_absent_override_stale_and_local_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_home = root / "user-home"
            user_home.mkdir()
            default_loopplane_home = (user_home / ".loopplane").resolve()
            absent_env = {**os.environ, "HOME": str(user_home)}
            absent_env.pop("LOOPPLANE_HOME", None)

            missing_home = run_loopplane("workspace", "list", "--json", env=absent_env)

            self.assertEqual(missing_home.returncode, EXIT_SUCCESS, missing_home.stderr + missing_home.stdout)
            missing_payload = json.loads(missing_home.stdout)
            self.assertTrue(missing_payload["ok"])
            self.assertEqual(missing_payload["loopplane_home"], default_loopplane_home.as_posix())
            self.assertFalse(missing_payload["registry_exists"])
            self.assertEqual(missing_payload["workspace_count"], 0)
            self.assertFalse((default_loopplane_home / "registry" / "workspaces.json").exists())

            project = root / "project"
            loopplane_home = root / "override-home"
            env = {**absent_env, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "LOOPPLANE_HOME aggregate authority fixture.", layout=LAYOUT_CANONICAL_V16)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authoritative_files}

            register = run_loopplane("workspace", "register", str(project), "--json", env=env)

            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            register_payload = json.loads(register.stdout)
            override_registry = loopplane_home.resolve() / "registry" / "workspaces.json"
            self.assertEqual(register_payload["loopplane_home"], loopplane_home.resolve().as_posix())
            self.assertEqual(register_payload["registry_file"], override_registry.as_posix())
            self.assertTrue(override_registry.is_file())
            self.assertFalse((default_loopplane_home / "registry" / "workspaces.json").exists())

            registry = json.loads(override_registry.read_text(encoding="utf-8"))
            self.assertEqual(registry["authority"], "discovery_only")
            registry["workspaces"][0]["workspace_id"] = "ws_conflicting_global_workspace"
            registry["workspaces"][0]["current_workflow_id"] = "wf_20260611_deadbeef"
            registry["workspaces"][0]["workflow_root"] = "/tmp/global-registry-must-not-win"
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_missing_global_workspace",
                    "name": "missing",
                    "project_root": (root / "missing").resolve().as_posix(),
                    "loopplane_dir": (root / "missing" / ".loopplane").resolve().as_posix(),
                    "repo_root": (root / "missing").resolve().as_posix(),
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": "wf_missing",
                }
            )
            override_registry.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            poisoned_registry = override_registry.read_bytes()

            workspace_current = run_loopplane("workspace", "current", "--project", str(project), "--json", env=env)
            workflow_current = run_loopplane("workflow", "current", "--project", str(project), "--json", env=env)
            listed = run_loopplane("workspace", "list", "--json", env=env)
            doctor = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)

            self.assertEqual(workspace_current.returncode, EXIT_SUCCESS, workspace_current.stderr + workspace_current.stdout)
            workspace_payload = json.loads(workspace_current.stdout)
            self.assertEqual(workspace_payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(workspace_payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(workspace_payload["workflow_root"], f".loopplane/workflows/{initialized.workflow_id}")

            self.assertEqual(workflow_current.returncode, EXIT_SUCCESS, workflow_current.stderr + workflow_current.stdout)
            workflow_payload = json.loads(workflow_current.stdout)
            self.assertEqual(workflow_payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(workflow_payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(
                workflow_payload["workflow"]["workflow_root"],
                f".loopplane/workflows/{initialized.workflow_id}",
            )

            self.assertEqual(listed.returncode, EXIT_SUCCESS, listed.stderr + listed.stdout)
            listed_payload = json.loads(listed.stdout)
            self.assertEqual(listed_payload["registry_authority"], "discovery_only")
            self.assertEqual(listed_payload["stale_count"], 2)
            listed_by_id = {workspace["workspace_id"]: workspace for workspace in listed_payload["workspaces"]}
            self.assertEqual(
                listed_by_id["ws_conflicting_global_workspace"]["health"]["project_local_workspace_id"],
                initialized.workspace_id,
            )
            self.assertEqual(
                listed_by_id["ws_conflicting_global_workspace"]["health"]["project_local_current_workflow_id"],
                initialized.workflow_id,
            )
            self.assertEqual(listed_by_id["ws_missing_global_workspace"]["health"]["status"], "missing_project")

            self.assertEqual(doctor.returncode, EXIT_SUCCESS, doctor.stderr + doctor.stdout)
            doctor_payload = json.loads(doctor.stdout)
            self.assertEqual(doctor_payload["status"], "warning")
            self.assertEqual(doctor_payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(doctor_payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(doctor_payload["global_registry"]["registry_authority"], "discovery_only")
            issue_codes = {issue["code"] for issue in doctor_payload["issues"]}
            self.assertIn("stale_global_registry_entry", issue_codes)
            self.assertIn("global_registry_workspace_mismatch", issue_codes)
            self.assertIn("global_registry_current_workflow_mismatch", issue_codes)
            self.assertEqual(
                {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in authoritative_files},
                before_hashes,
            )
            self.assertEqual(override_registry.read_bytes(), poisoned_registry)

    def test_workspace_list_rejects_invalid_global_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text('{"schema_version":"1.6","workspaces":{}}\n', encoding="utf-8")

            result = run_loopplane("workspace", "list", "--json", env=env)

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "invalid_global_registry")
            self.assertEqual(payload["workspace_count"], 0)

    def test_workspace_doctor_reports_healthy_registered_canonical_workspace_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace doctor canonical fixture.", layout=LAYOUT_CANONICAL_V16)
            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)
            text_result = run_loopplane("workspace", "doctor", "--project", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["current_workflow_id"], initialized.workflow_id)
            self.assertEqual(payload["project"]["status"], "healthy")
            self.assertEqual(payload["project"]["layout"], "canonical_v16")
            self.assertEqual(payload["global_registry"]["status"], "ok")
            self.assertEqual(payload["issues"], [])
            self.assertIn("read-only diagnostics", payload["mutation_boundary"])
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text_result.returncode, EXIT_SUCCESS, text_result.stderr + text_result.stdout)
            self.assertIn("loopplane workspace doctor: healthy", text_result.stdout)
            self.assertIn("project_local: healthy", text_result.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workspace_doctor_reports_healthy_flat_compatibility_workspace_without_mutating_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "flat"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace doctor flat fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)
            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["workspace_id"], initialized.workspace_id)
            self.assertEqual(payload["project"]["layout"], "compatibility_flat")
            self.assertEqual(payload["project"]["compatibility"]["status"], "supported")
            self.assertEqual(payload["project"]["compatibility"]["created_files"], [])
            self.assertIn(payload["project"]["workflow_root"], {".loopplane", ".loopplane/"})
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

    def test_workspace_doctor_reports_missing_metadata_without_materializing_flat_compatibility_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "flat"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            init_project(project, "Workspace doctor missing metadata fixture.", layout=LAYOUT_COMPATIBILITY_FLAT)
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                (project / relative).unlink()

            result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "unhealthy")
            self.assertEqual(payload["project"]["status"], "missing_workspace_metadata")
            self.assertEqual(payload["project"]["compatibility"]["status"], "flat_config_present")
            self.assertEqual(payload["project"]["compatibility"]["created_files"], [])
            self.assertIn("missing_workspace_metadata", {issue["code"] for issue in payload["issues"]})
            self.assertIn("recovery_actions", payload)
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                self.assertFalse((project / relative).exists())

    def test_workspace_doctor_reports_invalid_project_local_json_files(self) -> None:
        cases = {
            ".loopplane/workspace.json": "invalid_workspace_json",
            ".loopplane/workflow_registry.json": "invalid_workflow_registry_json",
            ".loopplane/current_workflow.json": "invalid_current_workflow_json",
        }
        for relative, expected_code in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                loopplane_home = root / "home"
                env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
                init_project(project, "Workspace doctor invalid JSON fixture.", layout=LAYOUT_CANONICAL_V16)
                (project / relative).write_text("{not json\n", encoding="utf-8")

                result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)

                self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["project"]["status"], "invalid_workspace_metadata")
                self.assertIn(expected_code, {issue["code"] for issue in payload["issues"]})
                self.assertTrue(payload["recovery_actions"])

    def test_workspace_doctor_reports_current_pointer_not_found_in_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            init_project(project, "Workspace doctor dangling pointer fixture.", layout=LAYOUT_CANONICAL_V16)
            current_path = project / ".loopplane" / "current_workflow.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["current_workflow_id"] = "wf_20260611_deadbeef"
            current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["project"]["status"], "invalid_workspace_metadata")
            self.assertIn("current_workflow_not_registered", {issue["code"] for issue in payload["issues"]})

    def test_workspace_doctor_reports_stale_missing_and_disagreeing_global_registry_entries_as_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            missing_project = root / "missing"
            loopplane_home = root / "home"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
            initialized = init_project(project, "Workspace doctor global registry fixture.", layout=LAYOUT_CANONICAL_V16)
            register = run_loopplane("workspace", "register", str(project), "--json", env=env)
            self.assertEqual(register.returncode, EXIT_SUCCESS, register.stderr + register.stdout)
            registry_path = loopplane_home / "registry" / "workspaces.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workspaces"][0]["current_workflow_id"] = "wf_20260611_deadbeef"
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_missing_registered_workspace",
                    "name": "missing",
                    "project_root": missing_project.resolve().as_posix(),
                    "loopplane_dir": (missing_project / ".loopplane").resolve().as_posix(),
                    "repo_root": missing_project.resolve().as_posix(),
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": "wf_missing",
                }
            )
            registry["workspaces"].append(
                {
                    "workspace_id": "ws_wrong_registered_workspace",
                    "name": "wrong",
                    "project_root": project.resolve().as_posix(),
                    "loopplane_dir": (project / ".loopplane").resolve().as_posix(),
                    "repo_root": project.resolve().as_posix(),
                    "status": "registered",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "current_workflow_id": initialized.workflow_id,
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            authoritative_files = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before = {path: path.read_bytes() for path in authoritative_files}

            result = run_loopplane("workspace", "doctor", "--project", str(project), "--json", env=env)
            text_result = run_loopplane("workspace", "doctor", "--project", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "warning")
            self.assertEqual(payload["project"]["status"], "healthy")
            self.assertEqual(payload["global_registry"]["registry_authority"], "discovery_only")
            self.assertEqual(payload["global_registry"]["stale_count"], 3)
            issue_codes = {issue["code"] for issue in payload["issues"]}
            self.assertIn("stale_global_registry_entry", issue_codes)
            self.assertIn("duplicate_global_registry_entries", issue_codes)
            self.assertIn("global_registry_workspace_mismatch", issue_codes)
            self.assertIn("global_registry_current_workflow_mismatch", issue_codes)
            self.assertTrue(payload["recovery_actions"])
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)

            self.assertEqual(text_result.returncode, EXIT_SUCCESS, text_result.stderr + text_result.stdout)
            self.assertIn("loopplane workspace doctor: warning", text_result.stdout)
            self.assertIn("registry_authority: discovery_only", text_result.stdout)
            self.assertIn("recovery_actions:", text_result.stdout)
            self.assertEqual({path: path.read_bytes() for path in authoritative_files}, before)


if __name__ == "__main__":
    unittest.main()
