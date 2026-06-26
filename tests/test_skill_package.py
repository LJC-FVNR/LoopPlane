from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.init_workflow import init_project
from runtime.skill_package import (
    ACCEPTED_MVP_DEFERRED_RELEASE_ITEMS,
    PACKAGE_FILE_REQUIREMENT_GROUPS,
    MVP_REQUIRED_RELEASE_ITEMS,
    RUNTIME_SCHEMA_PACKAGE_FILES,
    check_archived_read_only_mutation_rejection_release_gate,
    check_docs_completed_requirements_not_stubbed,
    check_docs_smoke_examples_are_not_substitutes,
    check_docs_status_classification_language,
    check_dashboard_history_switching_release_gate,
    check_loopplane_home_authority_separation_release_gate,
    check_migration_stale_state_exclusion_release_gate,
    check_v15_flat_compatibility_release_gate,
    check_v16_canonical_workflow_root_release_gate,
    check_v16_json_examples_parseable,
    check_v16_jsonl_examples_parseable,
    check_v16_runtime_schema_version_release_gate,
    check_required_adapters_no_notimplemented,
    check_required_command_handlers,
    check_required_deferred_release_classification,
    check_recommended_cli_fixture_flows,
    check_package_file_coverage,
    check_workspace_registry_current_pointer_release_gate,
    doctor_skill_package,
    pack_skill_package,
    _dashboard_history_gate_embedded_payload,
    _discover_install_cli_program,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
CLI_ADAPTER_FIXTURE_BIN = REPO_ROOT / "tests" / "fixtures" / "cli_adapters" / "bin"
SKILL_NAME = "loopplane"
AGENT_SKILL_RELATIVE_ROOTS = (
    Path(".codex") / "skills" / SKILL_NAME,
    Path(".claude") / "skills" / SKILL_NAME,
)


def run_loopplane(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=process_env,
    )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_file_group(name: str) -> dict[str, object]:
    for group in PACKAGE_FILE_REQUIREMENT_GROUPS:
        if group.get("name") == name:
            return group
    raise AssertionError(f"missing package file group: {name}")


def _write_group_files(root: Path, group: dict[str, object], *, skip: set[str]) -> None:
    files = group.get("files")
    if not isinstance(files, tuple):
        raise AssertionError(f"package group files must be a tuple: {group}")
    for relative in files:
        if relative in skip:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n", encoding="utf-8")


def _copy_cli_fixture_bin(root: Path) -> Path:
    fixture_bin = root / "fixture-bin"
    fixture_bin.mkdir()
    for executable in ("codex", "claude"):
        target = fixture_bin / executable
        shutil.copy2(CLI_ADAPTER_FIXTURE_BIN / executable, target)
        target.chmod(target.stat().st_mode | 0o111)
    return fixture_bin


def _env_with_cli_fixtures(root: Path) -> dict[str, str]:
    return {
        "LOOPPLANE_HOME": (root / "loopplane-home").as_posix(),
        "PATH": CLI_ADAPTER_FIXTURE_BIN.as_posix() + os.pathsep + os.environ.get("PATH", ""),
    }


def _env_with_selected_cli_fixtures(root: Path, executables: tuple[str, ...]) -> dict[str, str]:
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    for executable in executables:
        target = bin_dir / executable
        shutil.copy2(CLI_ADAPTER_FIXTURE_BIN / executable, target)
        target.chmod(target.stat().st_mode | 0o111)
    for executable in ("git", "python3"):
        source = shutil.which(executable) or (sys.executable if executable == "python3" else None)
        if source:
            target = bin_dir / executable
            if not target.exists():
                target.symlink_to(source)
    empty_home = root / "empty-home"
    empty_home.mkdir(exist_ok=True)
    return {
        "LOOPPLANE_HOME": (root / "loopplane-home").as_posix(),
        "HOME": empty_home.as_posix(),
        "PATH": bin_dir.as_posix(),
    }


def _env_without_agent_cli(root: Path) -> dict[str, str]:
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    empty_home = root / "empty-home"
    empty_home.mkdir(exist_ok=True)
    git_path = shutil.which("git")
    if git_path:
        (bin_dir / "git").symlink_to(git_path)
    return {
        "LOOPPLANE_HOME": (root / "loopplane-home").as_posix(),
        "HOME": empty_home.as_posix(),
        "PATH": bin_dir.as_posix(),
    }


def _assert_project_agent_skill_projection(testcase: unittest.TestCase, project: Path) -> None:
    for relative_root in AGENT_SKILL_RELATIVE_ROOTS:
        skill_root = project / relative_root
        entrypoint = skill_root / "SKILL.md"
        testcase.assertTrue(entrypoint.is_file(), entrypoint)
        skill_text = entrypoint.read_text(encoding="utf-8")
        testcase.assertIn(f"name: {SKILL_NAME}", skill_text)
        testcase.assertIn("description:", skill_text)
        testcase.assertTrue((skill_root / "references" / "PROTOCOL.md").is_file())
        testcase.assertTrue((skill_root / "scripts" / "loopplane").is_file())
        testcase.assertTrue(os.access(skill_root / "scripts" / "loopplane", os.X_OK))
        testcase.assertTrue((skill_root / "runtime" / "skill_package.py").is_file())
        testcase.assertTrue((skill_root / "agents" / "openai.yaml").is_file())
        manifest = json.loads((skill_root / ".loopplane_projection.json").read_text(encoding="utf-8"))
        testcase.assertEqual(manifest["schema_version"], "loopplane-agent-skill-projection-1")
        testcase.assertEqual(manifest["skill_name"], SKILL_NAME)
        testcase.assertIn("SKILL.md", manifest["managed_files"])
        testcase.assertIn("references/PROTOCOL.md", manifest["managed_files"])


def _write_task_failing_cli(path: Path, *, label: str, exit_code: int) -> None:
    path.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "from __future__ import annotations",
                "import sys",
                "args = sys.argv[1:]",
                "if args == ['--version']:",
                f"    print('fake {label} 1.0')",
                "    raise SystemExit(0)",
                "if args == ['auth', 'status']:",
                f"    print('fake {label} authenticated')",
                "    raise SystemExit(0)",
                f"print('{label.upper()} TASK FAILURE requested', file=sys.stderr)",
                f"raise SystemExit({exit_code})",
                "",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)


class SkillPackageDoctorTest(unittest.TestCase):
    def test_current_package_passes_doctor(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["missing_files"])
        self.assertFalse(result["missing_dirs"])

    def test_cli_skill_doctor_json_does_real_work(self) -> None:
        result = run_loopplane("skill", "doctor", "--json")

        self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
        self.assertNotIn("not implemented", result.stdout.lower())
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertIn("scripts/doctor.sh", payload["required_files_checked"])
        self.assertIn("runtime/watchdog.py", payload["required_files_checked"])
        self.assertIn("dashboard/public/static_dashboard.js", payload["required_files_checked"])

    def test_cli_skill_doctor_text_does_real_work(self) -> None:
        result = run_loopplane("skill", "doctor")

        self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
        self.assertIn("LoopPlane skill package doctor: pass", result.stdout)
        self.assertNotIn("not implemented", result.stdout.lower())

    def test_doctor_reports_grouped_package_file_coverage(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        coverage_check = next(
            check for check in result["checks"] if check["name"] == "package_file_coverage"
        )
        self.assertEqual(coverage_check["status"], "pass", coverage_check)
        groups = {entry["name"]: entry for entry in coverage_check["required_groups"]}
        self.assertEqual(groups["command_scripts"]["classification"], "required")
        self.assertEqual(groups["codex_skill_interface_metadata"]["classification"], "required")
        self.assertIn("agents/openai.yaml", groups["codex_skill_interface_metadata"]["files"])
        self.assertIn("scripts/loopplane", groups["command_scripts"]["files"])
        self.assertIn("scripts/install_local.sh", groups["command_scripts"]["files"])
        self.assertIn("scripts/doctor.sh", groups["command_scripts"]["files"])
        self.assertIn("runtime/scheduler.py", groups["core_runtime_files"]["files"])
        self.assertIn("runtime/plan_objectives.py", groups["core_runtime_files"]["files"])
        self.assertIn("runtime/objective_verification.py", groups["core_runtime_files"]["files"])
        self.assertIn("runtime/read_model_builder.py", groups["core_runtime_files"]["files"])
        self.assertIn("runtime/template_presets.py", groups["core_runtime_files"]["files"])
        schema_group = groups["runtime_schema_files"]
        self.assertEqual(schema_group["classification"], "required")
        self.assertIn("runtime/schemas/workspace.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/loopplane_home_workspaces.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/migration_export_manifest.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/runner_resource_lock.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/workflow_template.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/workflow_preset.schema.json", schema_group["files"])
        self.assertIn("runtime/schemas/template_instance.schema.json", schema_group["files"])
        self.assertEqual(sorted(schema_group["files"]), sorted(RUNTIME_SCHEMA_PACKAGE_FILES))
        template_group = groups["workflow_template_presets"]
        self.assertEqual(template_group["classification"], "required")
        self.assertIn("templates/workflows/README.md", template_group["files"])
        self.assertIn("templates/workflows/research-topic-exploration/template.json", template_group["files"])
        self.assertIn(
            "templates/workflows/dashboard-performance-investigation/examples/local_dashboard_latency.preset.json",
            template_group["files"],
        )
        dashboard_group = groups["dashboard_server_package_files"]
        self.assertEqual(dashboard_group["classification"], "required")
        self.assertIn("runtime/dashboard.py", dashboard_group["files"])
        self.assertIn("dashboard/public/static_dashboard.css", dashboard_group["files"])
        self.assertIn("dashboard/public/static_dashboard.js", dashboard_group["files"])
        optional_classes = {
            entry["classification"]
            for entry in coverage_check["optional_or_deferred_groups"]
        }
        self.assertIn("optional", optional_classes)
        self.assertIn("deferred", optional_classes)

    def test_doctor_checks_agent_skill_entrypoint_compatibility(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        compatibility = next(
            check
            for check in result["checks"]
            if check["name"] == "agent_skill_entrypoint_compatibility"
        )
        self.assertEqual(compatibility["status"], "pass", compatibility)
        self.assertEqual(compatibility["frontmatter"]["name"], SKILL_NAME)
        self.assertEqual(compatibility["expected_skill_directory"], SKILL_NAME)
        self.assertEqual(compatibility["codex_skill_root"], f".codex/skills/{SKILL_NAME}")
        self.assertEqual(compatibility["claude_code_skill_root"], f".claude/skills/{SKILL_NAME}")

    def test_package_file_coverage_gate_fails_missing_command_script(self) -> None:
        command_group = _package_file_group("command_scripts")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_group_files(root, command_group, skip={"scripts/install_local.sh"})

            result = check_package_file_coverage(
                root,
                groups=(command_group,),
                optional_groups=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("scripts/install_local.sh", result["missing_required_files"])
        self.assertEqual(result["missing_required_groups"][0]["name"], "command_scripts")
        self.assertIn("command_scripts", result["errors"][0])

    def test_package_file_coverage_gate_fails_missing_dashboard_server_asset(self) -> None:
        dashboard_group = _package_file_group("dashboard_server_package_files")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_group_files(root, dashboard_group, skip={"dashboard/public/static_dashboard.js"})

            result = check_package_file_coverage(
                root,
                groups=(dashboard_group,),
                optional_groups=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("dashboard/public/static_dashboard.js", result["missing_required_files"])
        self.assertEqual(
            result["missing_required_groups"][0]["name"],
            "dashboard_server_package_files",
        )
        self.assertIn("dashboard_server_package_files", result["errors"][0])

    def test_package_file_coverage_gate_fails_missing_schema_file(self) -> None:
        schema_group = _package_file_group("runtime_schema_files")
        missing_schema = "runtime/schemas/loopplane_home_workspaces.schema.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_group_files(root, schema_group, skip={missing_schema})

            result = check_package_file_coverage(
                root,
                groups=(schema_group,),
                optional_groups=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn(missing_schema, result["missing_required_files"])
        self.assertEqual(result["missing_required_groups"][0]["name"], "runtime_schema_files")
        self.assertIn("runtime_schema_files", result["errors"][0])

    def test_doctor_checks_required_command_handlers_are_not_stubbed(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        command_check = next(
            check for check in result["checks"] if check["name"] == "required_command_handlers_non_stub"
        )
        self.assertEqual(command_check["status"], "pass", command_check)
        self.assertIn("write-brief", command_check["required_commands"])
        self.assertIn("workspace register", command_check["required_commands"])
        self.assertIn("workspace unregister", command_check["required_commands"])
        self.assertIn("workspace scan", command_check["required_commands"])
        self.assertIn("workspace list", command_check["required_commands"])
        self.assertIn("workspace doctor", command_check["required_commands"])
        self.assertIn("workflow", command_check["required_commands"])
        self.assertIn("workflow list", command_check["required_commands"])
        self.assertIn("workflow current", command_check["required_commands"])
        self.assertIn("workflow show", command_check["required_commands"])
        self.assertIn("workflow switch", command_check["required_commands"])
        self.assertIn("workflow create", command_check["required_commands"])
        self.assertIn("workflow archive", command_check["required_commands"])
        self.assertIn("workflow restore", command_check["required_commands"])
        self.assertIn("workflow fork", command_check["required_commands"])
        self.assertIn("dashboard list", command_check["required_commands"])
        deferred_commands = {entry["command"] for entry in command_check["deferred_commands"]}
        self.assertNotIn("workflow fork", deferred_commands)
        self.assertNotIn("dashboard list", deferred_commands)
        self.assertIn("vc checkpoint", command_check["required_commands"])
        self.assertIn("vc import", command_check["required_commands"])
        self.assertNotIn("vc import", deferred_commands)
        self.assertFalse(command_check["stubbed_required_commands"])

    def test_doctor_checks_required_adapters_do_not_raise_notimplemented(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        adapter_check = next(
            check for check in result["checks"] if check["name"] == "required_adapters_no_notimplemented"
        )
        self.assertEqual(adapter_check["status"], "pass", adapter_check)
        checked = {
            (entry["adapter"], entry["method"]): entry
            for entry in adapter_check["checked"]
        }
        for adapter in ("noop", "shell", "codex_cli", "claude_code_cli"):
            for method in ("run", "doctor"):
                self.assertEqual(checked[(adapter, method)]["status"], "pass")
        self.assertFalse(adapter_check["failed_required_methods"])
        self.assertTrue(adapter_check["ignored_abstract_contracts"])

    def test_doctor_checks_recommended_cli_fixture_flows_execute_tasks(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        flow_check = next(
            check for check in result["checks"] if check["name"] == "recommended_cli_fixture_flows"
        )
        self.assertEqual(flow_check["status"], "pass", flow_check)
        self.assertFalse(flow_check["failed_flows"])
        checked = {entry["name"]: entry for entry in flow_check["checked"]}
        self.assertEqual(
            set(checked),
            {
                "codex_cli_worker_recommended_fixture",
                "claude_code_cli_worker_recommended_fixture",
            },
        )
        for entry in checked.values():
            self.assertEqual(entry["doctor_status"], "ok", entry)
            self.assertEqual(entry["exit_code"], 0, entry)
            self.assertFalse(entry["timed_out"], entry)
            self.assertEqual(entry["agent_status_status"], "completed", entry)
            self.assertFalse(entry["missing_contract_or_task_artifacts"], entry)
            self.assertFalse(entry["missing_produced_paths"], entry)

    def test_doctor_checks_dashboard_history_switching_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        history_check = next(
            check for check in result["checks"] if check["name"] == "dashboard_history_switching_release_gate"
        )
        self.assertEqual(history_check["status"], "pass", history_check)
        self.assertFalse(history_check["problems"], history_check)
        checked = history_check["checked"][0]
        self.assertEqual(checked["status"], "pass", checked)
        self.assertNotEqual(checked["current_workflow_id"], checked["selected_workflow_id"])
        self.assertEqual(checked["embedded_selected_workflow_id"], checked["selected_workflow_id"])
        self.assertEqual(
            set(checked["embedded_workflow_ids"]),
            {checked["current_workflow_id"], checked["selected_workflow_id"]},
        )

    def test_dashboard_history_gate_reads_compressed_embedded_payload(self) -> None:
        payload = {
            "workflow_id": "wf_20260613_00000001",
            "workspace": {"selected_workflow_id": "wf_20260613_00000001"},
        }
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=9, mtime=0)
        wrapper = {
            "payload_encoding": "gzip+base64",
            "payload_compressed": base64.b64encode(compressed).decode("ascii"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            index_file = Path(tmp) / "index.html"
            index_file.write_text(
                '<script id="loopplane-read-models" type="application/json">'
                + json.dumps(wrapper, sort_keys=True)
                + "</script>",
                encoding="utf-8",
            )

            self.assertEqual(_dashboard_history_gate_embedded_payload(index_file), payload)

    def test_dashboard_history_gate_fails_when_renderer_ignores_selected_workflow(self) -> None:
        from runtime.dashboard import render_static_dashboard

        def render_current_workflow(
            project: Path,
            *,
            output_dir: Path,
            rebuild_read_models_first: bool,
            workflow_id: str,
        ) -> dict[str, object]:
            return render_static_dashboard(
                project,
                output_dir=output_dir,
                rebuild_read_models_first=rebuild_read_models_first,
            )

        result = check_dashboard_history_switching_release_gate(
            REPO_ROOT,
            render_dashboard=render_current_workflow,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("selected_workflow_not_returned", result["problems"])
        self.assertIn("selected_workflow_read_models_not_used", result["problems"])
        self.assertIn("selected_workflow_status_not_loaded", result["problems"])

    def test_dashboard_history_gate_fails_when_renderer_mutates_current_pointer(self) -> None:
        from runtime.dashboard import render_static_dashboard

        def render_and_mutate_pointer(
            project: Path,
            *,
            output_dir: Path,
            rebuild_read_models_first: bool,
            workflow_id: str,
        ) -> dict[str, object]:
            result = render_static_dashboard(
                project,
                output_dir=output_dir,
                rebuild_read_models_first=rebuild_read_models_first,
                workflow_id=workflow_id,
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["current_workflow_id"] = workflow_id
            current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return result

        result = check_dashboard_history_switching_release_gate(
            REPO_ROOT,
            render_dashboard=render_and_mutate_pointer,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("workspace_metadata_mutated", result["problems"])

    def test_doctor_checks_archived_read_only_mutation_rejection_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        gate = next(
            check
            for check in result["checks"]
            if check["name"] == "archived_read_only_mutation_rejection_release_gate"
        )
        self.assertEqual(gate["status"], "pass", gate)
        self.assertFalse(gate["problems"], gate)
        checked = {entry["name"]: entry for entry in gate["checked"]}
        self.assertEqual(
            set(checked),
            {
                "dashboard_api_archived_read_only_mutation_matrix",
                "protected_workflow_history_mutation_snapshot",
                "workflow_control_archived_read_only_mutation_matrix",
                "explicit_restore_or_fork_escape_paths",
            },
        )
        api = checked["dashboard_api_archived_read_only_mutation_matrix"]
        self.assertEqual(api["status"], "pass", api)
        self.assertEqual(len(api["protected_results"]), 2, api)
        for protected in api["protected_results"]:
            for endpoint in protected["endpoint_results"]:
                self.assertEqual(endpoint["status_code"], 409, endpoint)
                self.assertEqual(endpoint["response_status"], "read_only_workflow", endpoint)
        control = checked["workflow_control_archived_read_only_mutation_matrix"]
        self.assertEqual(control["status"], "pass", control)
        statuses = {case["name"]: case["statuses"] for case in control["cases"]}
        self.assertEqual(statuses["mutable_control_requests"], ["pending", "pending", "pending", "pending"])
        self.assertEqual(
            statuses["archived_control_requests"],
            ["archived_workflow", "archived_workflow", "archived_workflow", "archived_workflow"],
        )
        self.assertEqual(
            statuses["read_only_control_requests"],
            ["read_only_workflow", "read_only_workflow", "read_only_workflow", "read_only_workflow"],
        )
        escape = checked["explicit_restore_or_fork_escape_paths"]
        self.assertEqual(escape["status"], "pass", escape)
        self.assertEqual(escape["restored_record_status"], "active")
        self.assertEqual(escape["forked_record_status"], "forked")
        self.assertTrue(escape["source_preserved"], escape)

    def test_archived_read_only_gate_fails_when_dashboard_api_mutates_protected_history(self) -> None:
        def mutating_api_smoke(project_info: dict[str, object], *, root: Path) -> dict[str, object]:
            project = Path(project_info["project"])
            archived_root = project / str(project_info["archived_workflow_root"])
            target = archived_root / "requests" / "dashboard_requests.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "request_id": "bad_archived_mutation",
                        "type": "plan",
                        "workflow_id": project_info["archived_workflow_id"],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return {
                "name": "mutating_dashboard_api_negative_control",
                "status": "pass",
                "project_root": project.relative_to(root).as_posix() if project.is_relative_to(root) else project.as_posix(),
                "problems": [],
            }

        result = check_archived_read_only_mutation_rejection_release_gate(
            REPO_ROOT,
            api_smoke=mutating_api_smoke,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("dashboard_api_mutated_protected_workflow_history", result["problems"])

    def test_archived_read_only_gate_fails_when_mutable_api_is_blanket_rejected(self) -> None:
        def blanket_reject_api_smoke(project_info: dict[str, object], *, root: Path) -> dict[str, object]:
            return {
                "name": "blanket_reject_dashboard_api_negative_control",
                "status": "fail",
                "project_root": Path(project_info["project"]).as_posix(),
                "problems": ["mutable_dashboard_api_request_records_not_written"],
            }

        result = check_archived_read_only_mutation_rejection_release_gate(
            REPO_ROOT,
            api_smoke=blanket_reject_api_smoke,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("mutable_dashboard_api_request_records_not_written", result["problems"])

    def test_doctor_checks_workspace_registry_current_pointer_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        pointer_check = next(
            check
            for check in result["checks"]
            if check["name"] == "workspace_registry_current_pointer_release_gate"
        )
        self.assertEqual(pointer_check["status"], "pass", pointer_check)
        self.assertFalse(pointer_check["problems"], pointer_check)
        checked = {entry["name"]: entry for entry in pointer_check["checked"]}
        self.assertEqual(
            set(checked),
            {
                "valid_pointer_registry_resolution",
                "missing_workflow_registry",
                "missing_current_workflow_pointer",
                "malformed_current_workflow_pointer",
                "dangling_current_workflow_pointer",
                "unregistered_workflow_directory_scan",
            },
        )
        self.assertEqual(checked["valid_pointer_registry_resolution"]["resolution_source"], "v1.6_metadata")
        self.assertEqual(checked["missing_workflow_registry"]["status"], "pass")
        self.assertEqual(checked["missing_current_workflow_pointer"]["status"], "pass")
        self.assertEqual(checked["dangling_current_workflow_pointer"]["status"], "pass")
        self.assertEqual(checked["unregistered_workflow_directory_scan"]["status"], "pass")

    def test_workspace_registry_current_pointer_gate_fails_when_missing_metadata_is_accepted(self) -> None:
        def accepts_flat_config(project: Path, *, workflow_id: str | None = None) -> tuple[object, dict[str, object]]:
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            return (
                SimpleNamespace(
                    workflow_id=workflow.get("workflow_id"),
                    workflow_root_value=workflow.get("workflow_root"),
                    workflow_config_file_value=".loopplane/config/workflow.json",
                    source="compatibility_config",
                ),
                workflow,
            )

        result = check_workspace_registry_current_pointer_release_gate(
            REPO_ROOT,
            resolver=accepts_flat_config,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("missing_workflow_registry_accepted", result["problems"])
        self.assertIn("missing_current_workflow_pointer_accepted", result["problems"])

    def test_workspace_registry_current_pointer_gate_fails_when_directory_scan_is_used(self) -> None:
        def scans_unregistered_workflow_dir(
            project: Path,
            *,
            workflow_id: str | None = None,
        ) -> tuple[object, dict[str, object]]:
            if workflow_id:
                workflow_path = project / ".loopplane" / "workflows" / workflow_id / "config" / "workflow.json"
            else:
                workflow_path = next((project / ".loopplane" / "workflows").glob("*/config/workflow.json"))
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            return (
                SimpleNamespace(
                    workflow_id=workflow.get("workflow_id"),
                    workflow_root_value=workflow.get("workflow_root"),
                    workflow_config_file_value=workflow.get("workflow_config_file"),
                    source="directory_scan_fallback",
                ),
                workflow,
            )

        result = check_workspace_registry_current_pointer_release_gate(
            REPO_ROOT,
            resolver=scans_unregistered_workflow_dir,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("valid_pointer_registry_resolution_ignored_current_pointer", result["problems"])
        self.assertIn("unregistered_workflow_directory_scan_accepted", result["problems"])

    def test_doctor_checks_v15_flat_compatibility_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        flat_check = next(
            check for check in result["checks"] if check["name"] == "v15_flat_compatibility_release_gate"
        )
        self.assertEqual(flat_check["status"], "pass", flat_check)
        self.assertFalse(flat_check["problems"], flat_check)
        checked = flat_check["checked"][0]
        self.assertEqual(checked["status"], "pass", checked)
        self.assertIn(checked["registry_workflow_root"], {".loopplane", ".loopplane/"})
        self.assertEqual(checked["resolved_workflow_root"], ".loopplane")
        self.assertEqual(checked["resolution_source"], "v1.6_metadata")
        self.assertEqual(checked["path_values"]["runtime_dir"], ".loopplane/runtime")
        self.assertEqual(checked["path_values"]["read_models_dir"], ".loopplane/read_models")
        self.assertEqual(checked["path_values"]["results_dir"], ".loopplane/results")
        self.assertEqual(checked["surface_result"]["schema_status"], "pass")
        self.assertEqual(
            checked["surface_result"]["preview_expected_prompt_path"],
            ".loopplane/runtime/runs/<run_id>/prompt.md",
        )
        self.assertEqual(checked["surface_result"]["dashboard_read_models_dir"], ".loopplane/read_models")
        self.assertFalse(checked["canonical_workflow_dirs"], checked)

    def test_doctor_checks_v16_canonical_workflow_root_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        canonical_check = next(
            check
            for check in result["checks"]
            if check["name"] == "v16_canonical_workflow_root_release_gate"
        )
        self.assertEqual(canonical_check["status"], "pass", canonical_check)
        self.assertFalse(canonical_check["problems"], canonical_check)
        checked = canonical_check["checked"][0]
        self.assertEqual(checked["status"], "pass", checked)
        workflow_id = checked["current_workflow_id"]
        workflow_root = f".loopplane/workflows/{workflow_id}"
        self.assertEqual(checked["registry_workflow_root"], workflow_root)
        self.assertEqual(checked["resolved_workflow_root"], workflow_root)
        self.assertEqual(checked["resolution_source"], "v1.6_metadata")
        self.assertEqual(checked["path_values"]["brief_file"], f"{workflow_root}/PROJECT_BRIEF.md")
        self.assertEqual(checked["path_values"]["plan_file"], f"{workflow_root}/PLAN.md")
        self.assertEqual(checked["path_values"]["shared_context_file"], f"{workflow_root}/SHARED_CONTEXT.md")
        self.assertEqual(checked["path_values"]["runtime_dir"], f"{workflow_root}/runtime")
        self.assertEqual(checked["path_values"]["read_models_dir"], f"{workflow_root}/read_models")
        self.assertEqual(checked["path_values"]["results_dir"], f"{workflow_root}/results")
        self.assertEqual(
            checked["surface_result"]["preview_expected_prompt_path"],
            f"{workflow_root}/runtime/runs/<run_id>/prompt.md",
        )
        self.assertEqual(checked["surface_result"]["read_models_dir"], f"{workflow_root}/read_models")
        self.assertEqual(checked["surface_result"]["dashboard_read_models_dir"], f"{workflow_root}/read_models")
        self.assertEqual(checked["surface_result"]["read_model_workflow_id"], workflow_id)
        self.assertEqual(checked["surface_result"]["dashboard_workflow_id"], workflow_id)
        self.assertEqual(
            checked["surface_result"]["read_model_task_title"],
            "Exercise canonical v1.6 workflow-root mode",
        )
        self.assertEqual(checked["projection_hashes_after"], checked["projection_hashes_before"])
        self.assertFalse(checked["root_flat_runtime_paths_created"], checked)

    def test_v16_canonical_workflow_root_gate_fails_when_resolver_uses_flat_root(self) -> None:
        def ignores_registry_current_pointer(project: Path) -> tuple[object, dict[str, object]]:
            workflow_id = json.loads(
                (project / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8")
            )["current_workflow_id"]
            flat = {
                "schema_version": "1.6",
                "workflow_id": workflow_id,
                "workspace_root": ".loopplane",
                "workflow_root": ".loopplane",
                "workflow_config_file": ".loopplane/config/workflow.json",
                "brief_file": "PROJECT_BRIEF.md",
                "plan_file": "PLAN.md",
                "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
                "runtime_dir": ".loopplane/runtime",
                "results_dir": ".loopplane/results",
                "read_models_dir": ".loopplane/read_models",
                "requests_dir": ".loopplane/requests",
                "planning_dir": ".loopplane/planning",
                "version_control_config_file": ".loopplane/config/version_control.json",
            }
            return (
                SimpleNamespace(
                    workflow_id=workflow_id,
                    workflow_root_value=".loopplane",
                    workflow_config_file_value=".loopplane/config/workflow.json",
                    source="compatibility_config",
                ),
                flat,
            )

        result = check_v16_canonical_workflow_root_release_gate(
            REPO_ROOT,
            resolver=ignores_registry_current_pointer,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("registry_current_pointer_metadata_not_used", result["problems"])
        self.assertIn("resolved_workflow_root_not_canonical", result["problems"])
        self.assertIn("workflow_paths_root_not_canonical", result["problems"])
        self.assertIn("brief_file_not_canonical_workflow_root", result["problems"])
        self.assertIn("runtime_dir_not_canonical_workflow_root", result["problems"])

    def test_v16_canonical_workflow_root_gate_fails_when_surface_uses_flat_paths(self) -> None:
        def hardcoded_flat_surface(project: Path) -> dict[str, object]:
            workflow_id = json.loads(
                (project / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8")
            )["current_workflow_id"]
            flat_runtime = project / ".loopplane" / "runtime"
            flat_read_models = project / ".loopplane" / "read_models"
            flat_runtime.mkdir(parents=True)
            flat_read_models.mkdir(parents=True)
            (project / "PLAN.md").write_text(
                "# Flat fallback plan\n\nThis mutation proves root projections were treated as truth.\n",
                encoding="utf-8",
            )
            return {
                "schema_status": "pass",
                "preview_expected_prompt_path": ".loopplane/runtime/runs/<run_id>/prompt.md",
                "read_models_ok": True,
                "read_models_dir": ".loopplane/read_models",
                "read_model_workflow_id": workflow_id,
                "read_model_task_title": "Flat fallback task",
                "dashboard_ok": True,
                "dashboard_read_models_dir": ".loopplane/read_models",
                "dashboard_workflow_id": workflow_id,
            }

        result = check_v16_canonical_workflow_root_release_gate(
            REPO_ROOT,
            surface_smoke=hardcoded_flat_surface,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("root_projection_files_mutated_as_canonical_truth", result["problems"])
        self.assertIn("root_flat_runtime_paths_created_for_canonical_instance", result["problems"])
        self.assertIn("canonical_preview_prompt_path_not_used", result["problems"])
        self.assertIn("canonical_read_models_dir_not_used", result["problems"])
        self.assertIn("canonical_dashboard_read_models_dir_not_used", result["problems"])
        self.assertIn("canonical_plan_read_model_not_loaded", result["problems"])

    def test_v15_flat_compat_gate_fails_when_resolver_forces_canonical_root(self) -> None:
        def forces_canonical_workflow_root(project: Path) -> tuple[object, dict[str, object]]:
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = str(workflow["workflow_id"])
            workflow_root = f".loopplane/workflows/{workflow_id}"
            canonical = {
                **workflow,
                "workflow_root": workflow_root,
                "workflow_config_file": f"{workflow_root}/config/workflow.json",
                "brief_file": f"{workflow_root}/PROJECT_BRIEF.md",
                "plan_file": f"{workflow_root}/PLAN.md",
                "shared_context_file": f"{workflow_root}/SHARED_CONTEXT.md",
                "runtime_dir": f"{workflow_root}/runtime",
                "results_dir": f"{workflow_root}/results",
                "read_models_dir": f"{workflow_root}/read_models",
                "requests_dir": f"{workflow_root}/requests",
                "planning_dir": f"{workflow_root}/planning",
                "version_control_config_file": f"{workflow_root}/config/version_control.json",
            }
            return (
                SimpleNamespace(
                    workflow_id=workflow_id,
                    workflow_root_value=workflow_root,
                    workflow_config_file_value=f"{workflow_root}/config/workflow.json",
                    source="hardcoded_canonical_layout",
                ),
                canonical,
            )

        result = check_v15_flat_compatibility_release_gate(
            REPO_ROOT,
            resolver=forces_canonical_workflow_root,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("registry_current_pointer_metadata_not_used", result["problems"])
        self.assertIn("resolved_workflow_root_not_flat", result["problems"])
        self.assertIn("workflow_paths_root_not_flat", result["problems"])
        self.assertIn("runtime_dir_not_flat_compatible", result["problems"])
        self.assertIn("read_models_dir_not_flat_compatible", result["problems"])

    def test_v15_flat_compat_gate_fails_when_surface_uses_canonical_dirs(self) -> None:
        def hardcoded_canonical_surface(project: Path) -> dict[str, object]:
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = str(workflow["workflow_id"])
            workflow_root = project / ".loopplane" / "workflows" / workflow_id
            (workflow_root / "runtime").mkdir(parents=True)
            return {
                "schema_status": "pass",
                "preview_expected_prompt_path": f".loopplane/workflows/{workflow_id}/runtime/runs/<run_id>/prompt.md",
                "read_models_ok": True,
                "read_models_dir": f".loopplane/workflows/{workflow_id}/read_models",
                "dashboard_ok": True,
                "dashboard_read_models_dir": f".loopplane/workflows/{workflow_id}/read_models",
            }

        result = check_v15_flat_compatibility_release_gate(
            REPO_ROOT,
            surface_smoke=hardcoded_canonical_surface,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("canonical_workflow_directory_created_for_flat_instance", result["problems"])
        self.assertIn("flat_preview_prompt_path_not_used", result["problems"])
        self.assertIn("flat_read_models_dir_not_used", result["problems"])
        self.assertIn("flat_dashboard_read_models_dir_not_used", result["problems"])

    def test_doctor_checks_loopplane_home_authority_separation_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        authority_check = next(
            check
            for check in result["checks"]
            if check["name"] == "loopplane_home_authority_separation_release_gate"
        )
        self.assertEqual(authority_check["status"], "pass", authority_check)
        self.assertFalse(authority_check["problems"], authority_check)
        checked = authority_check["checked"][0]
        workflow_id = checked["expected_workflow_id"]
        workflow_root = f".loopplane/workflows/{workflow_id}"
        self.assertEqual(checked["status"], "pass", checked)
        self.assertEqual(checked["resolved_workflow_id"], workflow_id)
        self.assertEqual(checked["resolved_workflow_root"], workflow_root)
        self.assertEqual(checked["resolution_source"], "v1.6_metadata")
        self.assertNotEqual(checked["poison_workflow_id"], workflow_id)
        self.assertEqual(checked["path_values"]["runtime_dir"], f"{workflow_root}/runtime")
        self.assertEqual(
            checked["surface_result"]["preview_expected_prompt_path"],
            f"{workflow_root}/runtime/runs/<run_id>/prompt.md",
        )
        self.assertEqual(checked["surface_result"]["read_model_workflow_id"], workflow_id)
        self.assertEqual(checked["surface_result"]["dashboard_workflow_id"], workflow_id)
        self.assertEqual(checked["workspace_current"]["current_workflow_id"], workflow_id)
        self.assertEqual(checked["workspace_current"]["workflow_root"], workflow_root)
        matching_record = checked["workspace_list"]["matching_project_record"]
        self.assertEqual(
            matching_record["health"]["project_local_current_workflow_id"],
            workflow_id,
        )
        self.assertEqual(
            matching_record["health"]["project_local_workflow_root"],
            workflow_root,
        )
        self.assertIn(
            "global_registry_current_workflow_mismatch",
            checked["workspace_doctor"]["issue_codes"],
        )
        self.assertEqual(checked["project_hashes_after"], checked["project_hashes_before"])
        self.assertEqual(checked["loopplane_home_hashes_after"], checked["loopplane_home_hashes_before"])

    def test_loopplane_home_authority_gate_fails_when_global_registry_is_authoritative(self) -> None:
        def reads_loopplane_home_registry(project: Path) -> tuple[object, dict[str, object]]:
            from runtime.path_resolution import default_workflow_path_values

            registry_path = Path(os.environ["LOOPPLANE_HOME"]) / "registry" / "workspaces.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            entry = next(
                item
                for item in registry["workspaces"]
                if item["project_root"] == project.resolve().as_posix()
            )
            workflow_id = str(entry["current_workflow_id"])
            workflow_root = str(entry["workflow_root"])
            config = {
                "schema_version": "1.5",
                "workflow_id": workflow_id,
                "workspace_root": ".loopplane",
                "workflow_root": workflow_root,
                "workflow_config_file": f"{workflow_root}/config/workflow.json",
                **default_workflow_path_values(workflow_root=workflow_root),
            }
            return (
                SimpleNamespace(
                    workflow_id=workflow_id,
                    workflow_root_value=workflow_root,
                    workflow_config_file_value=f"{workflow_root}/config/workflow.json",
                    source="loopplane_home_registry",
                ),
                config,
            )

        result = check_loopplane_home_authority_separation_release_gate(
            REPO_ROOT,
            resolver=reads_loopplane_home_registry,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("loopplane_home_registry_overrode_project_workflow", result["problems"])
        self.assertIn("project_local_metadata_not_used", result["problems"])
        self.assertIn("project_local_workflow_root_not_used", result["problems"])

    def test_doctor_checks_migration_stale_state_exclusion_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        gate = next(
            check
            for check in result["checks"]
            if check["name"] == "migration_stale_state_exclusion_release_gate"
        )
        self.assertEqual(gate["status"], "pass", gate)
        self.assertFalse(gate["problems"], gate)
        checked = {entry["name"]: entry for entry in gate["checked"]}
        self.assertEqual(
            set(checked),
            {
                "source_export_stale_state_exclusion",
                "stateful_export_stale_state_exclusion",
                "archive_export_stale_state_exclusion",
                "stateful_import_excludes_stale_state",
                "archive_read_only_import_excludes_stale_state",
            },
        )
        for entry in checked.values():
            self.assertEqual(entry["status"], "pass", entry)
            self.assertFalse(entry["content_findings"], entry)
        for name in (
            "source_export_stale_state_exclusion",
            "stateful_export_stale_state_exclusion",
            "archive_export_stale_state_exclusion",
        ):
            runner = checked[name]["sanitized_agent_runner"]
            self.assertFalse(str(runner["command"]).startswith("/"), runner)
            self.assertNotIn("loopplane-release-gate-runner-secret", json.dumps(runner, sort_keys=True))
            self.assertEqual(runner["cwd"], "{{project_root}}")
            self.assertEqual(runner["env"], {})
        self.assertEqual(
            checked["stateful_import_excludes_stale_state"]["runtime_state_status"],
            "waiting_config",
        )
        self.assertEqual(
            checked["archive_read_only_import_excludes_stale_state"]["runtime_state_status"],
            "read_only_imported",
        )

    def test_migration_stale_state_gate_fails_when_archive_preserves_stale_payload(self) -> None:
        def leak_stale_state(archive_path: Path, *, profile: str, fixture: dict[str, object]) -> None:
            payload = json.dumps(
                {
                    "pid": 12345,
                    "dashboard_token": "loopplane-release-gate-dashboard-token",
                    "runner_secret": "loopplane-release-gate-runner-secret",
                    "command": "/opt/loopplane-local/bin/codex --token loopplane-release-gate-runner-secret",
                },
                sort_keys=True,
            ).encode("utf-8")
            info = tarfile.TarInfo(f"leaked_{profile}_machine_state.json")
            info.size = len(payload)
            info.mtime = 0
            with tarfile.open(archive_path, "a") as archive:
                archive.addfile(info, io.BytesIO(payload))

        result = check_migration_stale_state_exclusion_release_gate(
            REPO_ROOT,
            archive_mutator=leak_stale_state,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("source_export_preserved_stale_payload", result["problems"])
        self.assertIn("stateful_export_preserved_stale_payload", result["problems"])
        self.assertIn("archive_export_preserved_stale_payload", result["problems"])

    def test_doctor_checks_v16_json_examples_parseable(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        json_check = next(
            check for check in result["checks"] if check["name"] == "v16_json_examples_parseable"
        )
        self.assertEqual(json_check["status"], "pass", json_check)
        self.assertEqual(json_check["counts"]["loopplane_md_json_fences"], 50)
        self.assertGreaterEqual(json_check["counts"]["schema_validated_examples"], 5)
        schema_files = {entry["schema_file"] for entry in json_check["schema_validated_examples"]}
        self.assertIn("workspace.schema.json", schema_files)
        self.assertIn("workflow_registry.schema.json", schema_files)
        self.assertIn("current_workflow.schema.json", schema_files)
        self.assertIn("loopplane_home_workspaces.schema.json", schema_files)
        self.assertIn("dashboard_server.schema.json", schema_files)

    def test_v16_json_examples_gate_fails_invalid_json_without_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_doc = root / "bad.md"
            bad_doc.write_text(
                "```json\n{\"schema_version\": \n```\n",
                encoding="utf-8",
            )

            result = check_v16_json_examples_parseable(
                root,
                markdown_files=("bad.md",),
                json_files=(),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("invalid JSON", result["errors"][0])

    def test_v16_json_examples_gate_allows_marked_invalid_pseudocode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "pseudocode.md"
            doc.write_text(
                "<!-- loopplane-json-example-exclude: intentionally incomplete pseudocode fixture -->\n"
                "```json\n{\"schema_version\": \n```\n",
                encoding="utf-8",
            )

            result = check_v16_json_examples_parseable(
                root,
                markdown_files=("pseudocode.md",),
                json_files=(),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["counts"]["excluded_markdown_json_fences"], 1)

    def test_v16_json_examples_gate_fails_schema_invalid_registry_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema_dir = root / "runtime" / "schemas"
            schema_dir.mkdir(parents=True)
            shutil.copy2(
                REPO_ROOT / "runtime" / "schemas" / "workflow_registry.schema.json",
                schema_dir / "workflow_registry.schema.json",
            )
            doc = root / "LoopPlane.md"
            doc.write_text(
                "### 31.4 Workflow registry\n\n"
                "```json\n"
                "{\n"
                "  \"schema_version\": \"1.6\",\n"
                "  \"workspace_id\": \"ws_01JZ8Q3X6R9K7J2N4M5P6Q7R8S\",\n"
                "  \"generated_at\": \"2026-06-10T12:00:00Z\",\n"
                "  \"workflows\": [\n"
                "    {\n"
                "      \"workflow_id\": \"wf_20260610_000001\",\n"
                "      \"name\": \"invalid id fixture\",\n"
                "      \"status\": \"completed\",\n"
                "      \"workflow_root\": \".loopplane/workflows/wf_20260610_000001\",\n"
                "      \"created_at\": \"2026-06-10T12:00:00Z\",\n"
                "      \"last_seen_at\": \"2026-06-10T12:00:00Z\",\n"
                "      \"plan_file\": \".loopplane/workflows/wf_20260610_000001/PLAN.md\",\n"
                "      \"read_models_dir\": \".loopplane/workflows/wf_20260610_000001/read_models\",\n"
                "      \"read_only\": false,\n"
                "      \"archived\": false,\n"
                "      \"summary\": {\n"
                "        \"one_line\": \"fixture\",\n"
                "        \"tasks_total\": 1,\n"
                "        \"tasks_completed\": 1,\n"
                "        \"tasks_blocked\": 0\n"
                "      }\n"
                "    }\n"
                "  ]\n"
                "}\n"
                "```\n",
                encoding="utf-8",
            )

            result = check_v16_json_examples_parseable(
                root,
                markdown_files=("LoopPlane.md",),
                json_files=(),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertTrue(any("workflow_registry.schema.json" in error for error in result["errors"]))

    def test_doctor_checks_v16_jsonl_examples_parseable(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        jsonl_check = next(
            check for check in result["checks"] if check["name"] == "v16_jsonl_examples_parseable"
        )
        self.assertEqual(jsonl_check["status"], "pass", jsonl_check)
        self.assertEqual(jsonl_check["counts"]["loopplane_md_jsonl_fences"], 5)
        self.assertEqual(jsonl_check["counts"]["markdown_jsonl_records"], 8)
        self.assertGreaterEqual(jsonl_check["counts"]["jsonl_files_checked"], 1)

    def test_v16_jsonl_examples_gate_fails_invalid_jsonl_without_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_doc = root / "bad.md"
            bad_doc.write_text(
                "```jsonl\n{\"ok\": true}\n{\"schema_version\": \n```\n",
                encoding="utf-8",
            )

            result = check_v16_jsonl_examples_parseable(
                root,
                markdown_files=("bad.md",),
                jsonl_files=(),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("invalid JSONL record", result["errors"][0])

    def test_v16_jsonl_examples_gate_allows_marked_invalid_pseudocode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "pseudocode.md"
            doc.write_text(
                "<!-- loopplane-jsonl-example-exclude: intentionally incomplete pseudocode fixture -->\n"
                "```jsonl\n{\"schema_version\": \n```\n",
                encoding="utf-8",
            )

            result = check_v16_jsonl_examples_parseable(
                root,
                markdown_files=("pseudocode.md",),
                jsonl_files=(),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["counts"]["excluded_markdown_jsonl_records"], 1)

    def test_v16_jsonl_examples_gate_fails_invalid_jsonl_file_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "records.jsonl"
            data.write_text(
                "{\"ok\": true}\n{\"not\": \n",
                encoding="utf-8",
            )

            result = check_v16_jsonl_examples_parseable(
                root,
                markdown_files=(),
                jsonl_files=("records.jsonl",),
                required_loopplane_md_fence_count=0,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("records.jsonl:2: invalid JSONL record", result["errors"][0])

    def test_doctor_checks_v16_runtime_schema_version_release_gate(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        schema_check = next(
            check for check in result["checks"] if check["name"] == "v16_runtime_schema_version_release_gate"
        )
        self.assertEqual(schema_check["status"], "pass", schema_check)
        checked = {entry["name"]: entry for entry in schema_check["checked"]}
        self.assertEqual(set(checked), {"canonical_v16_runtime_files", "v15_flat_compatibility_files"})
        canonical = checked["canonical_v16_runtime_files"]
        flat = checked["v15_flat_compatibility_files"]
        self.assertNotEqual(canonical["workflow_root"], ".loopplane")
        self.assertGreater(canonical["compatibility_tag_count"], 0)
        self.assertFalse(canonical["untagged_schema_version_15_findings"], canonical)
        self.assertEqual(flat["workflow_root"], ".loopplane")
        self.assertTrue(flat["allowed_schema_version_15_findings"], flat)
        self.assertFalse(schema_check["problems"], schema_check)

    def test_v16_runtime_schema_gate_fails_untagged_canonical_legacy_file(self) -> None:
        def remove_canonical_compatibility_tags(project: Path) -> None:
            schema_path = next((project / ".loopplane" / "workflows").glob("*/config/schema_version.json"))
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
            payload.pop("compatibility", None)
            schema_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        result = check_v16_runtime_schema_version_release_gate(
            REPO_ROOT,
            canonical_project_mutator=remove_canonical_compatibility_tags,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertTrue(
            any(problem.startswith("untagged_stale_schema_version:") for problem in result["problems"]),
            result,
        )
        self.assertTrue(
            any(problem.endswith("/runtime/state.json") for problem in result["problems"]),
            result,
        )

    def test_recommended_cli_fixture_flow_gate_fails_when_codex_cannot_execute_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture_bin = _copy_cli_fixture_bin(Path(tmp))
            _write_task_failing_cli(fixture_bin / "codex", label="codex", exit_code=31)

            result = check_recommended_cli_fixture_flows(
                REPO_ROOT,
                fixture_bin_dir=fixture_bin,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("codex_cli_worker_recommended_fixture", result["failed_flows"])
        codex = next(
            entry
            for entry in result["checked"]
            if entry["name"] == "codex_cli_worker_recommended_fixture"
        )
        self.assertIn("adapter_exit_nonzero", codex["problems"])
        self.assertIn("missing_contract_or_task_artifacts", codex["problems"])
        claude = next(
            entry
            for entry in result["checked"]
            if entry["name"] == "claude_code_cli_worker_recommended_fixture"
        )
        self.assertEqual(claude["status"], "pass", claude)

    def test_recommended_cli_fixture_flow_gate_fails_when_claude_cannot_execute_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture_bin = _copy_cli_fixture_bin(Path(tmp))
            _write_task_failing_cli(fixture_bin / "claude", label="claude", exit_code=32)

            result = check_recommended_cli_fixture_flows(
                REPO_ROOT,
                fixture_bin_dir=fixture_bin,
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn("claude_code_cli_worker_recommended_fixture", result["failed_flows"])
        claude = next(
            entry
            for entry in result["checked"]
            if entry["name"] == "claude_code_cli_worker_recommended_fixture"
        )
        self.assertIn("adapter_exit_nonzero", claude["problems"])
        self.assertIn("missing_contract_or_task_artifacts", claude["problems"])
        codex = next(
            entry
            for entry in result["checked"]
            if entry["name"] == "codex_cli_worker_recommended_fixture"
        )
        self.assertEqual(codex["status"], "pass", codex)

    def test_doctor_reports_required_deferred_release_classification(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        release_check = next(
            check for check in result["checks"] if check["name"] == "required_deferred_release_classification"
        )
        self.assertEqual(release_check["status"], "pass", release_check)
        self.assertFalse(release_check["unresolved_classification_problems"])
        required_ids = {entry["id"] for entry in release_check["required_items"]}
        self.assertIn("workspace_registry_current_workflow_pointer", required_ids)
        self.assertIn("basic_tests", required_ids)
        self.assertTrue(release_check["accepted_deferrals"])
        for entry in release_check["accepted_deferrals"]:
            self.assertEqual(entry["spec_reference"], "LoopPlane.md 26.2")
            self.assertTrue(entry["reason"])

    def test_doctor_checks_docs_do_not_claim_completed_requirements_are_stubbed(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        docs_check = next(
            check for check in result["checks"] if check["name"] == "docs_completed_requirements_not_stubbed"
        )
        self.assertEqual(docs_check["status"], "pass", docs_check)
        self.assertFalse(docs_check["stale_completed_requirement_claims"])
        self.assertIn("LoopPlane.md", docs_check["docs_checked"])
        self.assertIn("README.md", docs_check["docs_checked"])
        self.assertIn("dashboard/README.md", docs_check["docs_checked"])

    def test_docs_gate_fails_stale_completed_requirement_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readme = root / "README.md"
            readme.write_text(
                "The loopplane write-brief command is reserved until implemented.\n",
                encoding="utf-8",
            )

            result = check_docs_completed_requirements_not_stubbed(
                root,
                doc_files=("README.md",),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["stale_completed_requirement_claims"]), 1)
        claim = result["stale_completed_requirement_claims"][0]
        self.assertEqual(claim["problem"], "stale_completed_requirement_claim")
        self.assertEqual(claim["matched_completed_surfaces"][0]["id"], "write_brief")

    def test_docs_gate_accepts_explicit_deferred_v16_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readme = root / "README.md"
            readme.write_text(
                "Global cross-workspace dashboard discovery is reserved for the future "
                "because LoopPlane.md 26.2 says the MVP may defer it.\n",
                encoding="utf-8",
            )

            result = check_docs_completed_requirements_not_stubbed(
                root,
                doc_files=("README.md",),
            )

        self.assertEqual(result["status"], "pass", result)
        self.assertFalse(result["stale_completed_requirement_claims"])
        self.assertEqual(len(result["accepted_future_or_deferred_mentions"]), 1)

    def test_docs_gate_fails_cli_agent_adapter_skeleton_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loopplane_spec = root / "LoopPlane.md"
            loopplane_spec.write_text(
                "shell/noop adapter and at least one CLI agent adapter skeleton;\n",
                encoding="utf-8",
            )

            result = check_docs_completed_requirements_not_stubbed(
                root,
                doc_files=("LoopPlane.md",),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["stale_completed_requirement_claims"]), 1)
        claim = result["stale_completed_requirement_claims"][0]
        self.assertEqual(claim["problem"], "stale_completed_requirement_claim")
        self.assertEqual(claim["matched_completed_surfaces"][0]["id"], "cli_agent_adapters")

    def test_doctor_checks_smoke_examples_are_not_substitutes(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        smoke_docs_check = next(
            check for check in result["checks"] if check["name"] == "docs_smoke_examples_not_substitutes"
        )
        self.assertEqual(smoke_docs_check["status"], "pass", smoke_docs_check)
        self.assertFalse(smoke_docs_check["risky_substitute_claims"])
        self.assertFalse(smoke_docs_check["missing_required_clarifications"])
        self.assertIn("runtime/README.md", smoke_docs_check["docs_checked"])

    def test_smoke_docs_gate_fails_noop_provider_cli_substitute_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readme = root / "README.md"
            readme.write_text(
                "The noop adapter replaces provider CLI execution for production workflows.\n",
                encoding="utf-8",
            )

            result = check_docs_smoke_examples_are_not_substitutes(
                root,
                doc_files=("README.md",),
                required_clarifications=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["risky_substitute_claims"]), 1)
        claim = result["risky_substitute_claims"][0]
        self.assertEqual(claim["problem"], "smoke_example_substitute_claim")
        self.assertIn("noop_replaces_cli", claim["matched_patterns"])

    def test_smoke_docs_gate_fails_static_dashboard_request_entry_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard_readme = root / "dashboard.md"
            dashboard_readme.write_text(
                "Static dashboard supports request-entry controls without server mode.\n",
                encoding="utf-8",
            )

            result = check_docs_smoke_examples_are_not_substitutes(
                root,
                doc_files=("dashboard.md",),
                required_clarifications=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["risky_substitute_claims"]), 1)
        claim = result["risky_substitute_claims"][0]
        self.assertEqual(claim["problem"], "smoke_example_substitute_claim")
        self.assertIn("static_dashboard_request_entry", claim["matched_patterns"])

    def test_doctor_checks_docs_status_classification_language(self) -> None:
        result = doctor_skill_package(REPO_ROOT)

        status_docs_check = next(
            check for check in result["checks"] if check["name"] == "docs_status_classification_language"
        )
        self.assertEqual(status_docs_check["status"], "pass", status_docs_check)
        self.assertFalse(status_docs_check["future_overclaim_claims"])
        self.assertFalse(status_docs_check["missing_required_clarifications"])
        clarification_ids = {
            entry["id"] for entry in status_docs_check["required_clarifications"]
        }
        self.assertIn("readme_completed_standalone_mvp", clarification_ids)
        self.assertIn("readme_v16_support_status", clarification_ids)
        self.assertIn("scripts_v16_support_status", clarification_ids)
        self.assertIn("references_v16_support_status", clarification_ids)

    def test_status_docs_gate_fails_missing_v16_support_status_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readme = root / "README.md"
            readme.write_text(
                "Completed standalone/MVP functionality includes codex_cli and claude_code_cli.\n",
                encoding="utf-8",
            )

            result = check_docs_status_classification_language(
                root,
                doc_files=("README.md",),
                required_clarifications=(
                    {
                        "id": "v16_support_status_anchor",
                        "file": "README.md",
                        "required_terms": ("v1.6 Support Status", "migration export/import profiles"),
                    },
                ),
                overclaim_patterns=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["missing_required_clarifications"]), 1)
        missing = result["missing_required_clarifications"][0]
        self.assertEqual(missing["id"], "v16_support_status_anchor")
        self.assertIn("v1.6 Support Status", missing["missing_terms"])

    def test_status_docs_gate_fails_future_v16_overclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readme = root / "README.md"
            readme.write_text(
                "LOOPPLANE_HOME discovery and migration export/import are fully implemented.\n",
                encoding="utf-8",
            )

            result = check_docs_status_classification_language(
                root,
                doc_files=("README.md",),
                required_clarifications=(),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(len(result["future_overclaim_claims"]), 1)
        claim = result["future_overclaim_claims"][0]
        self.assertEqual(claim["problem"], "future_surface_overclaim")
        self.assertIn("v16_global_migration_complete_claim", claim["matched_patterns"])

    def test_required_deferred_gate_fails_required_item_marked_deferred(self) -> None:
        bad_deferrals = (
            *ACCEPTED_MVP_DEFERRED_RELEASE_ITEMS,
            {
                "id": "final_verifier",
                "label": "final verifier",
                "spec_reference": "LoopPlane.md 26.1",
                "reason": "negative control: required items cannot be deferred",
            },
        )

        result = check_required_deferred_release_classification(
            REPO_ROOT,
            deferred_items=bad_deferrals,
        )

        self.assertEqual(result["status"], "fail", result)
        problems = {
            (entry["id"], entry["problem"])
            for entry in result["unresolved_classification_problems"]
        }
        self.assertIn(("final_verifier", "required_item_marked_deferred"), problems)

    def test_required_deferred_gate_fails_missing_required_item_without_spec_deferral(self) -> None:
        required_ids = [
            str(entry["id"])
            for entry in MVP_REQUIRED_RELEASE_ITEMS
            if entry["id"] != "failure_registry"
        ]

        result = check_required_deferred_release_classification(
            REPO_ROOT,
            required_item_ids=required_ids,
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertIn(
            ("failure_registry", "missing_required_classification"),
            {
                (entry["id"], entry["problem"])
                for entry in result["unresolved_classification_problems"]
            },
        )

    def test_required_command_handler_gate_fails_stubbed_required_command(self) -> None:
        def real_handler(args: argparse.Namespace) -> int:
            return 0

        def not_implemented(args: argparse.Namespace) -> int:
            return 1

        def build_parser() -> argparse.ArgumentParser:
            parser = argparse.ArgumentParser(prog="loopplane")
            subparsers = parser.add_subparsers(dest="command")
            plan = subparsers.add_parser("plan")
            plan.set_defaults(handler=not_implemented, command_path=("plan",))
            run = subparsers.add_parser("run")
            run.set_defaults(handler=real_handler, command_path=("run",))
            return parser

        result = check_required_command_handlers(
            REPO_ROOT,
            required_commands=(("plan",), ("run",)),
            deferred_commands=(),
            cli_module=SimpleNamespace(build_parser=build_parser, not_implemented=not_implemented),
        )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(result["stubbed_required_commands"], ["plan"])
        self.assertFalse(result["missing_required_commands"])

    def test_required_adapter_gate_fails_notimplemented_required_method(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter_path = root / "runtime" / "adapters" / "fake_adapter.py"
            adapter_path.parent.mkdir(parents=True)
            adapter_path.write_text(
                "\n".join(
                    (
                        "class FakeAdapter:",
                        "    adapter_name = 'fake_cli'",
                        "    def run(self, adapter_input):",
                        "        raise NotImplementedError",
                        "    def doctor(self, adapter_input):",
                        "        return object()",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            result = check_required_adapters_no_notimplemented(
                root,
                required_adapters=(
                    {
                        "adapter": "fake_cli",
                        "module": "runtime/adapters/fake_adapter.py",
                        "class": "FakeAdapter",
                        "required_methods": ("run", "doctor"),
                    },
                ),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(result["failed_required_methods"], ["fake_cli.run"])
        run_check = next(
            entry
            for entry in result["checked"]
            if entry["adapter"] == "fake_cli" and entry["method"] == "run"
        )
        self.assertIn("raises_NotImplementedError", run_check["problems"])

    def test_required_adapter_gate_fails_base_contract_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter_dir = root / "runtime" / "adapters"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "base.py").write_text(
                "\n".join(
                    (
                        "class AgentAdapter:",
                        "    def run(self, adapter_input):",
                        "        raise NotImplementedError",
                        "    def doctor(self, adapter_input):",
                        "        return 'waiting_config'",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            (adapter_dir / "fake_adapter.py").write_text(
                "\n".join(
                    (
                        "class FakeAdapter(AgentAdapter):",
                        "    adapter_name = 'fake_cli'",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            result = check_required_adapters_no_notimplemented(
                root,
                required_adapters=(
                    {
                        "adapter": "fake_cli",
                        "module": "runtime/adapters/fake_adapter.py",
                        "class": "FakeAdapter",
                        "required_methods": ("run", "doctor"),
                    },
                ),
            )

        self.assertEqual(result["status"], "fail", result)
        self.assertEqual(result["failed_required_methods"], ["fake_cli.run", "fake_cli.doctor"])
        checks = {
            entry["method"]: entry
            for entry in result["checked"]
            if entry["adapter"] == "fake_cli"
        }
        self.assertIn("inherits_abstract_base_contract", checks["run"]["problems"])
        self.assertIn("inherits_default_waiting_config_doctor", checks["doctor"]["problems"])

    def test_cli_skill_doctor_reports_invalid_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_loopplane("skill", "doctor", "--package-root", tmp, "--json")

        self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")
        self.assertIn("SKILL.md", payload["missing_files"])


class SkillPackageInstallTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_empty_target_creates_runnable_instance_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "empty-project"
            env = _env_with_cli_fixtures(root)

            result = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["status"], "installed")
            self.assertEqual(payload["layout"], "compatibility_flat")
            self.assertEqual(payload["workflow_root"], ".loopplane")
            self.assertGreater(payload["created"]["count"], 0)
            self.assertIsInstance(payload["created"]["sample"], list)
            self.assertEqual(payload["runner_readiness"]["status"], "ok")
            self.assertIn("worker", payload["runner_readiness"]["configured_runner_ids"])
            self.assertIn("worker_fallback", payload["runner_readiness"]["configured_runner_ids"])
            self.assertNotIn("inspector", payload["runner_readiness"]["configured_runner_ids"])
            self.assertTrue((project / ".loopplane" / "config" / "local" / "agent_runners.local.json").is_file())
            local_runners = json.loads(
                (project / ".loopplane" / "config" / "local" / "agent_runners.local.json").read_text(encoding="utf-8")
            )["runners"]
            self.assertTrue(Path(local_runners["worker_fallback"]["command"].split()[0]).is_absolute())
            self.assertTrue(local_runners["worker_fallback"]["enabled"])
            self.assertNotIn("summary_fallback", local_runners)
            self.assertIn("worker_fallback", payload["runner_readiness"]["required_runner_ids"])
            installations = {entry["agent_style"]: entry for entry in payload["agent_skill_installations"]}
            self.assertEqual(set(installations), {"codex", "claude_code"})
            self.assertEqual(installations["codex"]["skill_root"], f".codex/skills/{SKILL_NAME}")
            self.assertEqual(installations["claude_code"]["skill_root"], f".claude/skills/{SKILL_NAME}")
            self.assertEqual(installations["codex"]["status"], "created")
            self.assertEqual(installations["claude_code"]["status"], "created")

            required_paths = (
                "PROJECT_BRIEF.md",
                "PLAN.md",
                ".loopplane/SHARED_CONTEXT.md",
                ".loopplane/config",
                ".loopplane/planning",
                ".loopplane/runtime",
                ".loopplane/read_models",
                ".loopplane/requests",
                ".loopplane/results",
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            )
            for relative in required_paths:
                self.assertTrue((project / relative).exists(), relative)
            _assert_project_agent_skill_projection(self, project)

            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["workflows"][0]["workflow_id"], payload["workflow_id"])
            self.assertEqual(registry["workflows"][0]["workflow_root"], ".loopplane/")
            self.assertEqual(registry["workflows"][0]["status"], "draft")

            status = run_loopplane("status", "--project", str(project), "--json")
            self.assertEqual(status.returncode, EXIT_SUCCESS, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["runtime_state"]["status"], "initialized")

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_best_effort_when_claude_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "codex-only-project"
            env = _env_with_selected_cli_fixtures(root, ("codex",))

            result = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            readiness = payload["runner_readiness"]
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["status"], "installed")
            self.assertEqual(readiness["status"], "ok")
            self.assertIn("worker", readiness["configured_runner_ids"])
            self.assertNotIn("worker_fallback", readiness["configured_runner_ids"])
            self.assertTrue(
                any(item["program"] == "codex" and item["status"] == "found" for item in readiness["discovery"]),
                readiness,
            )
            self.assertTrue(
                any(
                    item["program"] == "claude"
                    and item["status"] == "missing"
                    and item["required"] is False
                    for item in readiness["discovery"]
                ),
                readiness,
            )
            self.assertTrue(any("Claude Code CLI" in step for step in readiness["next_steps"]), readiness)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_uses_claude_when_codex_cli_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "claude-only-project"
            env = _env_with_selected_cli_fixtures(root, ("claude",))

            result = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            readiness = payload["runner_readiness"]
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(readiness["status"], "ok")
            self.assertIn("worker", readiness["configured_runner_ids"])
            self.assertNotIn("worker_fallback", readiness["configured_runner_ids"])
            self.assertTrue(
                any(
                    item["program"] == "codex"
                    and item["status"] == "missing"
                    and item["required"] is False
                    for item in readiness["discovery"]
                ),
                readiness,
            )
            self.assertTrue(
                any(
                    item["program"] == "claude"
                    and item["status"] == "found"
                    and item["required"] is True
                    for item in readiness["discovery"]
                ),
                readiness,
            )
            local_runners = json.loads(
                (project / ".loopplane" / "config" / "local" / "agent_runners.local.json").read_text(encoding="utf-8")
            )["runners"]
            self.assertEqual(local_runners["worker"]["adapter"], "claude_code_cli")
            self.assertEqual(local_runners["worker"]["prompt_delivery"]["mode"], "stdin_or_prompt_flag")

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_can_limit_project_agent_skill_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "codex-only-project"
            env = _env_with_cli_fixtures(root)

            result = run_loopplane("skill", "install", "--target", str(project), "--agent-style", "codex", "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["agent_skill_projection_policy"]["requested_agent_styles"], ["codex"])
            self.assertEqual(payload["agent_skill_projection_policy"]["installed_agent_styles"], ["codex"])
            installations = {entry["agent_style"]: entry for entry in payload["agent_skill_installations"]}
            self.assertEqual(set(installations), {"codex"})
            self.assertTrue((project / ".codex" / "skills" / SKILL_NAME / "SKILL.md").is_file())
            self.assertFalse((project / ".claude" / "skills" / SKILL_NAME / "SKILL.md").exists())

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_can_skip_project_agent_skill_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "no-projection-project"
            env = _env_with_cli_fixtures(root)

            result = run_loopplane("skill", "install", "--target", str(project), "--no-agent-skill-projection", "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], payload)
            self.assertFalse(payload["agent_skill_projection_policy"]["enabled"])
            self.assertEqual(payload["agent_skill_projection_policy"]["installed_agent_styles"], [])
            self.assertEqual(payload["agent_skill_installations"], [])
            self.assertFalse((project / ".codex").exists())
            self.assertFalse((project / ".claude").exists())

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_text_does_real_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "text-project"
            env = _env_with_cli_fixtures(root)

            result = run_loopplane("skill", "install", "--target", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertIn("loopplane skill install: installed", result.stdout)
            self.assertIn("layout: compatibility_flat", result.stdout)
            self.assertIn("runner_readiness: ok", result.stdout)
            self.assertIn("agent_skill_installations:", result.stdout)
            self.assertIn(f".codex/skills/{SKILL_NAME}", result.stdout)
            self.assertIn(f".claude/skills/{SKILL_NAME}", result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())

    def test_install_cli_discovery_finds_editor_extension_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            codex = home / ".vscode-server" / "extensions" / "openai.chatgpt-1.2.3" / "bin" / "linux-x64" / "codex"
            claude = home / ".vscode-server" / "extensions" / "anthropic.claude-code-2.1.175-linux-x64" / "resources" / "native-binary" / "claude"
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex.chmod(codex.stat().st_mode | 0o111)
            claude.chmod(claude.stat().st_mode | 0o111)

            with patch("runtime.skill_package.shutil.which", return_value=None), patch("runtime.skill_package.Path.home", return_value=home):
                discovered_codex = _discover_install_cli_program("codex")
                discovered_claude = _discover_install_cli_program("claude")

            self.assertEqual(discovered_codex, codex.resolve())
            self.assertEqual(discovered_claude, claude.resolve())

    def test_cli_skill_install_refuses_existing_project_file_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "existing-project"
            project.mkdir()
            brief = project / "PROJECT_BRIEF.md"
            brief.write_text("Existing human-authored brief\n", encoding="utf-8")

            result = run_loopplane("skill", "install", "--target", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "refused")
            self.assertTrue(any("PROJECT_BRIEF.md" in conflict for conflict in payload["conflicts"]))
            self.assertEqual(brief.read_text(encoding="utf-8"), "Existing human-authored brief\n")
            self.assertFalse((project / ".loopplane").exists())

    def test_cli_skill_install_refuses_metadata_conflict_before_partial_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "metadata-conflict-project"
            conflict = project / ".loopplane" / "config" / "local"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("not a directory\n", encoding="utf-8")

            result = run_loopplane("skill", "install", "--target", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "refused")
            self.assertTrue(any(".loopplane/config/local" in conflict for conflict in payload["conflicts"]))
            self.assertFalse((project / ".loopplane" / "config" / "workflow.json").exists())
            self.assertFalse((project / "PROJECT_BRIEF.md").exists())

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_attaches_existing_flat_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "existing-flat-project"
            env = _env_with_cli_fixtures(root)
            init_project(project, "Keep this existing brief.")
            brief_before = (project / "PROJECT_BRIEF.md").read_text(encoding="utf-8")

            install = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)

            self.assertEqual(install.returncode, EXIT_SUCCESS, install.stderr + install.stdout)
            payload = json.loads(install.stdout)
            self.assertEqual(payload["status"], "attached")
            self.assertEqual(payload["runner_readiness"]["status"], "ok")
            self.assertEqual((project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"), brief_before)
            self.assertTrue((project / ".loopplane" / "workspace.json").is_file())
            self.assertTrue((project / ".loopplane" / "workflow_registry.json").is_file())
            self.assertTrue((project / ".loopplane" / "current_workflow.json").is_file())
            _assert_project_agent_skill_projection(self, project)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_is_idempotent_for_installed_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "installed-project"
            env = _env_with_cli_fixtures(root)

            first = run_loopplane("skill", "install", "--target", str(project), "--json", "--verbose-json", env=env)
            second = run_loopplane("skill", "install", "--target", str(project), "--json", "--verbose-json", env=env)

            self.assertEqual(first.returncode, EXIT_SUCCESS, first.stderr + first.stdout)
            self.assertEqual(second.returncode, EXIT_SUCCESS, second.stderr + second.stdout)
            first_payload = json.loads(first.stdout)
            second_payload = json.loads(second.stdout)
            self.assertEqual(second_payload["status"], "attached")
            self.assertEqual(second_payload["runner_readiness"]["status"], "ok")
            self.assertEqual(second_payload["workflow_id"], first_payload["workflow_id"])
            self.assertIn(".loopplane/workflow_registry.json", second_payload["preserved"])
            self.assertIn(f".codex/skills/{SKILL_NAME}/SKILL.md", second_payload["preserved"])
            self.assertIn(f".claude/skills/{SKILL_NAME}/SKILL.md", second_payload["preserved"])
            self.assertEqual(
                {entry["status"] for entry in second_payload["agent_skill_installations"]},
                {"preserved"},
            )

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_install_requires_agent_cli_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "missing-cli-project"
            env = _env_without_agent_cli(root)

            result = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "installed_waiting_config")
            self.assertEqual(payload["runner_readiness"]["status"], "waiting_config")
            self.assertTrue(
                any(item["program"] == "codex" and item["status"] == "missing" for item in payload["runner_readiness"]["discovery"]),
                payload["runner_readiness"],
            )
            self.assertTrue(any("Codex CLI" in step for step in payload["runner_readiness"]["next_steps"]))


class SkillPackageUpdateTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_update_installed_project_creates_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "installed-project"
            env = _env_with_cli_fixtures(root)
            install = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)
            self.assertEqual(install.returncode, EXIT_SUCCESS, install.stderr + install.stdout)

            result = run_loopplane("skill", "update", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["status"], "updated")
            self.assertEqual(payload["runner_readiness"]["status"], "ok")
            self.assertEqual(payload["layout"], "compatibility_flat")
            self.assertEqual(payload["workflow_root"], ".loopplane")
            self.assertIn(".loopplane/config/package_manifest.json", payload["created"])
            self.assertIn("PROJECT_BRIEF.md", payload["protected_paths"])
            self.assertTrue((project / ".loopplane" / "config" / "package_manifest.json").is_file())
            _assert_project_agent_skill_projection(self, project)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_update_text_does_real_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "text-project"
            env = _env_with_cli_fixtures(root)
            install = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)
            self.assertEqual(install.returncode, EXIT_SUCCESS, install.stderr + install.stdout)

            result = run_loopplane("skill", "update", "--target", str(project), env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertIn("loopplane skill update: updated", result.stdout)
            self.assertIn("protected_paths:", result.stdout)
            self.assertIn("runner_readiness: ok", result.stdout)
            self.assertIn("agent_skill_installations:", result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_update_existing_flat_project_materializes_v16_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "flat-project"
            env = _env_with_cli_fixtures(root)
            init_project(project, "Keep this flat workflow brief.")
            brief_before = (project / "PROJECT_BRIEF.md").read_text(encoding="utf-8")
            workspace_before = (project / ".loopplane" / "workspace.json").read_text(encoding="utf-8")

            result = run_loopplane("skill", "update", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "updated")
            self.assertEqual(payload["runner_readiness"]["status"], "ok")
            self.assertIn(".loopplane/workspace.json", payload["preserved"])
            self.assertIn(".loopplane/workflow_registry.json", payload["preserved"])
            self.assertIn(".loopplane/current_workflow.json", payload["preserved"])
            self.assertEqual((project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"), brief_before)
            self.assertEqual((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"), workspace_before)
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["workflows"][0]["workflow_root"], ".loopplane/")

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_update_is_idempotent_and_preserves_workflow_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "stateful-project"
            env = _env_with_cli_fixtures(root)
            install = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)
            self.assertEqual(install.returncode, EXIT_SUCCESS, install.stderr + install.stdout)

            protected_files = [
                project / "PROJECT_BRIEF.md",
                project / "PLAN.md",
                project / ".loopplane" / "SHARED_CONTEXT.md",
                project / ".loopplane" / "runtime" / "state.json",
                project / ".loopplane" / "runtime" / "git_checkpoints.jsonl",
                project / ".loopplane" / "requests" / "chat_requests.jsonl",
                project / ".loopplane" / "read_models" / "workflow_status.json",
            ]
            (project / "PROJECT_BRIEF.md").write_text("Human-authored brief\n", encoding="utf-8")
            (project / "PLAN.md").write_text("Human-authored plan\n", encoding="utf-8")
            (project / ".loopplane" / "SHARED_CONTEXT.md").write_text("Human shared context\n", encoding="utf-8")
            (project / ".loopplane" / "runtime" / "git_checkpoints.jsonl").write_text(
                "{\"checkpoint_id\":\"user\"}\n",
                encoding="utf-8",
            )
            (project / ".loopplane" / "requests" / "chat_requests.jsonl").write_text(
                "{\"request_id\":\"user\"}\n",
                encoding="utf-8",
            )
            local_config = project / ".loopplane" / "config" / "local" / "user.json"
            local_config.write_text("{\"user\": true}\n", encoding="utf-8")
            local_gitignore = project / ".loopplane" / "config" / "local" / ".gitignore"
            local_gitignore.write_text("# user local ignore rules\n", encoding="utf-8")
            result_file = project / ".loopplane" / "results" / "T001" / "runs" / "run_user" / "report.md"
            result_file.parent.mkdir(parents=True)
            result_file.write_text("runtime result\n", encoding="utf-8")
            protected_files.extend([local_config, local_gitignore, result_file])
            before = {path.as_posix(): file_sha256(path) for path in protected_files}

            first = run_loopplane("skill", "update", "--target", str(project), "--json", env=env)
            manifest_after_first = file_sha256(project / ".loopplane" / "config" / "package_manifest.json")
            second = run_loopplane("skill", "update", "--target", str(project), "--json", env=env)

            self.assertEqual(first.returncode, EXIT_SUCCESS, first.stderr + first.stdout)
            self.assertEqual(second.returncode, EXIT_SUCCESS, second.stderr + second.stdout)
            first_payload = json.loads(first.stdout)
            second_payload = json.loads(second.stdout)
            self.assertEqual(first_payload["status"], "updated")
            self.assertEqual(second_payload["status"], "current")
            self.assertEqual(second_payload["runner_readiness"]["status"], "ok")
            self.assertIn(".loopplane/config/package_manifest.json", first_payload["created"])
            self.assertIn(".loopplane/config/package_manifest.json", second_payload["preserved"])
            after = {path.as_posix(): file_sha256(path) for path in protected_files}
            self.assertEqual(after, before)
            self.assertEqual(
                file_sha256(project / ".loopplane" / "config" / "package_manifest.json"),
                manifest_after_first,
            )

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_cli_skill_update_refuses_locally_modified_agent_skill_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Path(tmp) / "modified-projection-project"
            env = _env_with_cli_fixtures(root)
            install = run_loopplane("skill", "install", "--target", str(project), "--json", env=env)
            self.assertEqual(install.returncode, EXIT_SUCCESS, install.stderr + install.stdout)
            skill_file = project / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8") + "\n# local edit\n",
                encoding="utf-8",
            )

            result = run_loopplane("skill", "update", "--target", str(project), "--json", env=env)

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "refused")
            self.assertTrue(any(".claude/skills" in conflict for conflict in payload["conflicts"]))

    def test_cli_skill_update_refuses_missing_workflow_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "not-installed"
            project.mkdir()

            result = run_loopplane("skill", "update", "--target", str(project), "--json")

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "invalid_config")
            self.assertTrue(any("workflow config is missing" in error for error in payload["errors"]))


class SkillPackagePackTest(unittest.TestCase):
    def test_cli_skill_pack_json_creates_portable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "loopplane-skill.zip"

            result = run_loopplane("skill", "pack", "--output", str(artifact), "--json")

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["status"], "packed")
            self.assertEqual(payload["validation_status"], "pass")
            self.assertEqual(payload["artifact_path"], artifact.as_posix())
            self.assertEqual(payload["artifact_sha256"], f"sha256:{file_sha256(artifact)}")
            self.assertGreater(payload["content_counts"]["files"], 30)
            self.assertIn("scripts/loopplane", payload["included_files"])
            self.assertIn("runtime/scheduler.py", payload["included_files"])

            with zipfile.ZipFile(artifact) as archive:
                names = set(archive.namelist())

            archive_root = payload["archive_root"]
            expected = {
                f"{archive_root}/SKILL.md",
                f"{archive_root}/README.md",
                f"{archive_root}/skill.json",
                f"{archive_root}/agents/openai.yaml",
                f"{archive_root}/references/PROTOCOL.md",
                f"{archive_root}/templates/worker_prompt.template.md",
                f"{archive_root}/scripts/loopplane",
                f"{archive_root}/scripts/install_local.sh",
                f"{archive_root}/scripts/doctor.sh",
                f"{archive_root}/runtime/scheduler.py",
                f"{archive_root}/runtime/adapters/base.py",
                f"{archive_root}/dashboard/README.md",
                f"{archive_root}/examples/minimal_project/README.md",
            }
            self.assertTrue(expected.issubset(names), sorted(expected.difference(names)))
            forbidden_fragments = (
                "/.git/",
                "__pycache__",
            )
            for name in names:
                self.assertFalse(any(fragment in name for fragment in forbidden_fragments), name)

    def test_cli_skill_pack_text_does_real_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "loopplane-skill.zip"

            result = run_loopplane("skill", "pack", "--output", str(artifact))

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            self.assertIn("loopplane skill pack: packed", result.stdout)
            self.assertIn("validation: pass", result.stdout)
            self.assertIn(f"artifact: {artifact}", result.stdout)
            self.assertNotIn("not implemented", result.stdout.lower())
            self.assertTrue(artifact.is_file())

    def test_cli_skill_pack_refuses_invalid_package_root_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-package"
            root.mkdir()
            artifact = Path(tmp) / "invalid.zip"

            result = run_loopplane(
                "skill",
                "pack",
                "--package-root",
                str(root),
                "--output",
                str(artifact),
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_INVALID_CONFIG, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "invalid_package")
            self.assertEqual(payload["validation_status"], "fail")
            self.assertIn("SKILL.md", payload["validation"]["missing_files"])
            self.assertFalse(artifact.exists())

    def test_pack_skill_package_validates_before_writing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-package"
            root.mkdir()
            artifact = Path(tmp) / "invalid.zip"

            payload = pack_skill_package(root, output=artifact)

            self.assertFalse(payload["ok"], payload)
            self.assertEqual(payload["status"], "invalid_package")
            self.assertFalse(artifact.exists())


class SkillPackageCommandNonStubTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_skill_commands_do_real_work_and_do_not_route_to_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            artifact = Path(tmp) / "loopplane-skill.zip"
            commands = [
                ("doctor", run_loopplane("skill", "doctor", "--json")),
                ("install", run_loopplane("skill", "install", "--target", str(project), "--json")),
                ("update", run_loopplane("skill", "update", "--target", str(project), "--json")),
                ("pack", run_loopplane("skill", "pack", "--output", str(artifact), "--json")),
            ]

            for name, result in commands:
                with self.subTest(name=name):
                    self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
                    self.assertNotIn("not implemented", result.stdout.lower())
                    payload = json.loads(result.stdout)
                    self.assertTrue(payload["ok"], payload)

            self.assertTrue(artifact.is_file())


if __name__ == "__main__":
    unittest.main()
