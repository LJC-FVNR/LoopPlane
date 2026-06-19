from __future__ import annotations

import base64
import gzip
import json
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from runtime.init_workflow import LAYOUT_CANONICAL_V16, init_project
from runtime.migration_export import (
    EXPORT_MANIFEST_NAME,
    export_project,
    list_export_archive_members,
    read_export_archive_manifest,
)
from runtime.migration_import import import_project_archive
from runtime.path_resolution import WorkflowPaths, load_workflow_config


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


def dashboard_script_payload(html: str) -> dict[str, object]:
    match = re.search(r'<script id="loopplane-read-models" type="application/json">(.+?)</script>', html)
    assert match is not None
    script_payload = json.loads(match.group(1))
    if script_payload.get("payload_encoding") != "gzip+base64":
        return script_payload
    compressed = base64.b64decode(str(script_payload["payload_compressed"]))
    return json.loads(gzip.decompress(compressed).decode("utf-8"))


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def archive_text(path: Path, member: str) -> str:
    with tarfile.open(path, "r:*") as archive:
        extracted = archive.extractfile(member)
        if extracted is None:
            raise AssertionError(f"{member} missing from archive")
        return extracted.read().decode("utf-8")


def archive_json(path: Path, member: str) -> object:
    return json.loads(archive_text(path, member))


def assert_no_process_keys(test: unittest.TestCase, value: object) -> None:
    forbidden = {
        "pid",
        "pids",
        "adapter_pid",
        "background",
        "background_jobs",
        "background_pids",
        "process_handle",
        "supervisor_pid",
        "wake_next_agent_when",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            test.assertNotIn(str(key), forbidden)
            test.assertFalse(str(key).endswith("_pid"), key)
            assert_no_process_keys(test, child)
    elif isinstance(value, list):
        for child in value:
            assert_no_process_keys(test, child)


def prepare_source_export_project(root: Path) -> tuple[Path, WorkflowPaths]:
    project = root / "service-a"
    init_project(project, "Source migration export fixture.", layout=LAYOUT_CANONICAL_V16)
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    workflow_id = str(paths.workflow_id)

    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / ".codex" / "skills" / "loopplane").mkdir(parents=True)
    (project / ".codex" / "skills" / "loopplane" / "SKILL.md").write_text("local codex projection\n", encoding="utf-8")
    (project / ".claude" / "skills" / "loopplane").mkdir(parents=True)
    (project / ".claude" / "skills" / "loopplane" / "SKILL.md").write_text("local claude projection\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=do-not-export\n", encoding="utf-8")
    write_json(project / ".loopplane_home" / "runners" / "agent_runners.local.json", {"secret": "workspace-home"})
    (root / "service-b").mkdir()
    (root / "service-b" / "sibling.txt").write_text("outside workspace\n", encoding="utf-8")

    agent_config_path = paths.config_file("agent_runners.json")
    agent_config = json.loads(agent_config_path.read_text(encoding="utf-8"))
    agent_config["runners"]["worker"]["command"] = "/opt/local/bin/codex --danger"
    agent_config["runners"]["worker"]["cwd"] = "/tmp/local-project"
    agent_config["runners"]["worker"]["env"] = {
        "OPENAI_API_KEY": "secret-value",
        "PATH": "/machine/local/bin",
    }
    agent_config["runners"]["worker"]["doctor"]["check_command"] = "/opt/local/bin/codex --version"
    agent_config_path.write_text(json.dumps(agent_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    write_json(project / ".loopplane" / "config" / "local" / "agent_runners.local.json", {"secret": "local"})
    write_json(paths.runtime_dir / "lock" / "scheduler_instance_lock" / "owner.json", {"pid": 12345})
    write_json(paths.runtime_dir / "active_run_leases" / "run_001.json", {"pid": 12345})
    (paths.runtime_dir / "dashboard_token").write_text("dashboard-secret\n", encoding="utf-8")
    write_json(paths.runtime_dir / "dashboard_server.json", {"pid": 12345, "token": "dashboard-secret"})
    write_json(paths.runtime_dir / "background_jobs.json", {"jobs": [{"pid": 45678, "command": "/tmp/local/run.sh"}]})
    write_json(paths.runtime_dir / "supervisor.json", {"pid": 45678, "process_handle": {"pid": 45678}})
    (paths.runtime_dir / "supervisor").mkdir()
    (paths.runtime_dir / "supervisor" / "supervisor_stdout.log").write_text("local supervisor log\n", encoding="utf-8")
    write_json(paths.runtime_dir / "runs" / "run_001" / "run_metadata.json", {"adapter_pid": 45678})
    write_json(paths.read_models_dir / "workflow_status.json", {"derived": True})

    (paths.planning_dir / "runs" / "plan_001").mkdir(parents=True)
    write_json(paths.planning_dir / "runs" / "plan_001" / "plan_result.json", {"status": "planned"})
    (paths.requests_dir / "control_requests.jsonl").write_text(
        json.dumps({"request_id": "req_001", "action": "pause"}) + "\n",
        encoding="utf-8",
    )
    events_file = paths.runtime_dir / "events" / "events_000001.jsonl"
    events_file.write_text(
        json.dumps(
            event_record_with_hash(
                {
                    "schema_version": "1.5",
                    "sequence": 1,
                    "event_id": "evt_source",
                    "event_type": "task_completed",
                    "timestamp": "2026-06-12T00:00:01Z",
                    "data": {"adapter_pid": 45678, "token": "dashboard-secret"},
                }
            ),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        paths.runtime_dir / "snapshots" / "snapshot_000001.json",
        {
            "schema_version": "1.5",
            "snapshot_id": "snapshot_000001",
            "workflow_id": workflow_id,
            "created_at": "2026-06-12T00:00:00Z",
            "events_through_sequence": 1,
            "state": {"status": "running", "pid": 45678},
        },
    )
    write_json(
        paths.runtime_dir / "failure_registry.json",
        {"schema_version": "1.5", "workflow_id": workflow_id, "failures": [{"failure_id": "F001", "pid": 45678}]},
    )
    (paths.runtime_dir / "git_checkpoints.jsonl").write_text(
        json.dumps({"checkpoint_id": "gitcp_source", "ref": "refs/loopplane/example"}) + "\n",
        encoding="utf-8",
    )
    write_json(
        paths.runtime_dir / "evidence_manifest.json",
        {"schema_version": "1.5", "workflow_id": workflow_id, "tasks": {"T001": {"status": "pass"}}},
    )
    write_json(paths.runtime_dir / "final_verification_report.json", {"status": "pass"})

    run_dir = paths.results_dir / "T001" / "runs" / "run_001"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
    write_json(
        run_dir / "validation.json",
        {
            "schema_version": "1.5",
            "run_id": "run_001",
            "primary_task_id": "T001",
            "status": "pass",
            "verdict": "accepted",
        },
    )
    write_json(run_dir / "node_summary.json", {"task_id": "T001"})
    write_json(paths.results_dir / "T001" / "latest.json", {"run_id": "run_001"})
    write_json(
        run_dir / "agent_status.json",
        {
            "schema_version": "1.5",
            "run_id": "run_001",
            "task_id": "T001",
            "primary_task_id": "T001",
            "status": "completed",
            "next_prompt_ready": True,
            "machine": "/tmp/local",
            "background": {
                "pids": [45678],
                "commands": ["/opt/local/bin/worker --token dashboard-secret"],
                "logs": ["/tmp/local/stdout.log"],
                "wake_next_agent_when": "pid exits",
            },
            "background_pids": [45678],
        },
    )
    write_json(
        run_dir / "adapter_result.json",
        {
            "command": "/opt/local/bin/codex exec /tmp/local/prompt.md",
            "cwd": "/tmp/local-project",
            "stdout_path": str(run_dir / "logs" / "stdout.log"),
            "stderr_path": str(run_dir / "logs" / "stderr.log"),
            "adapter_metadata": {
                "process_handle": {"pid": 45678},
                "runner_resource_lock": {"pid": 45678, "lock_path": "/home/user/.loopplane/locks/runner.lock"},
                "api_token": "secret-token",
            },
        },
    )
    (run_dir / "commands.sh").write_text("/opt/local/bin/codex exec /tmp/local/prompt.md\n", encoding="utf-8")
    (run_dir / "logs" / "stdout.log").write_text("local log\n", encoding="utf-8")
    (run_dir / "logs" / "stderr.log").write_text("local error log\n", encoding="utf-8")
    (run_dir / "artifacts" / "result.txt").write_text("result artifact\n", encoding="utf-8")

    return project, paths


class SourceMigrationExportTest(unittest.TestCase):
    def test_cli_source_export_writes_manifest_and_filters_non_portable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, paths = prepare_source_export_project(root)
            loopplane_home = root / "loopplane-home"
            write_json(loopplane_home / "runners" / "agent_runners.local.json", {"secret": "home-secret"})
            write_json(loopplane_home / "dashboard" / "servers.json", {"servers": [{"token": "home-token"}]})
            output = root / "loopplane_source.tar"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "source",
                "--output",
                str(output),
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["profile"], "source")
            self.assertGreater(payload["manifest"]["files"]["count"], 0)
            self.assertGreater(payload["manifest"]["excluded_paths"]["count"], 0)
            members = set(list_export_archive_members(output))
            manifest = read_export_archive_manifest(output)

            workflow_root = paths.workflow_root_value.rstrip("/")
            self.assertIn(EXPORT_MANIFEST_NAME, members)
            self.assertIn("PROJECT_BRIEF.md", members)
            self.assertIn("PLAN.md", members)
            self.assertIn("src/app.py", members)
            self.assertIn(".loopplane/workspace.json", members)
            self.assertIn(".loopplane/workflow_registry.json", members)
            self.assertIn(".loopplane/current_workflow.json", members)
            self.assertIn(f"{workflow_root}/SHARED_CONTEXT.md", members)
            self.assertIn(f"{workflow_root}/config/workflow.json", members)
            self.assertIn(f"{workflow_root}/config/agent_runners.json", members)
            self.assertIn(f"{workflow_root}/runtime/events/events_000001.jsonl", members)
            self.assertIn(f"{workflow_root}/runtime/git_checkpoints.jsonl", members)
            self.assertIn(f"{workflow_root}/runtime/evidence_manifest.json", members)
            self.assertIn(f"{workflow_root}/runtime/final_verification_report.json", members)
            self.assertIn(f"{workflow_root}/results/T001/runs/run_001/report.md", members)
            self.assertIn(f"{workflow_root}/results/T001/runs/run_001/validation.json", members)
            self.assertIn(f"{workflow_root}/results/T001/runs/run_001/node_summary.json", members)
            self.assertIn(f"{workflow_root}/results/T001/latest.json", members)

            forbidden = {
                ".env",
                ".codex/skills/loopplane/SKILL.md",
                ".claude/skills/loopplane/SKILL.md",
                ".loopplane_home/runners/agent_runners.local.json",
                f"{workflow_root}/runtime/lock/scheduler_instance_lock/owner.json",
                f"{workflow_root}/runtime/active_run_leases/run_001.json",
                f"{workflow_root}/runtime/dashboard_token",
                f"{workflow_root}/runtime/dashboard_server.json",
                f"{workflow_root}/read_models/workflow_status.json",
                ".loopplane/config/local/agent_runners.local.json",
                f"{workflow_root}/results/T001/runs/run_001/agent_status.json",
                f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log",
                "../service-b/sibling.txt",
                str(loopplane_home / "runners" / "agent_runners.local.json"),
            }
            self.assertFalse(forbidden & members)

            exported_agent_config = json.loads(archive_text(output, f"{workflow_root}/config/agent_runners.json"))
            runner = exported_agent_config["runners"]["worker"]
            self.assertEqual(runner["command"], "codex --danger")
            self.assertEqual(runner["cwd"], "{{project_root}}")
            self.assertEqual(runner["env"], {})
            self.assertEqual(runner["doctor"]["check_command"], "codex --version")
            self.assertIn("migration_notes", exported_agent_config)

            event = json.loads(archive_text(output, f"{workflow_root}/runtime/events/events_000001.jsonl"))
            assert_no_process_keys(self, event)
            self.assertEqual(event["data"]["token"], "<redacted-for-source-migration>")

            manifest_paths = {record["path"] for record in manifest["files"]}
            excluded = {record["path"]: record["reason"] for record in manifest["excluded_paths"]}
            self.assertIn("src/app.py", manifest_paths)
            self.assertEqual(excluded[f"{workflow_root}/runtime/dashboard_token"], "runtime_process_state")
            self.assertEqual(excluded[".codex/skills/loopplane/SKILL.md"], "agent_skill_projection")
            self.assertEqual(excluded[".claude/skills/loopplane/SKILL.md"], "agent_skill_projection")
            self.assertEqual(excluded[f"{workflow_root}/read_models/workflow_status.json"], "derived_read_model")
            self.assertEqual(excluded[".loopplane/config/local/agent_runners.local.json"], "machine_local_config")
            self.assertEqual(excluded[".loopplane_home/runners/agent_runners.local.json"], "loopplane_home_files")
            self.assertIn("project-local Codex/Claude skill projections", manifest["source_profile"]["excludes"])

    def test_tar_zst_output_falls_back_to_uncompressed_tar_when_zstd_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _paths = prepare_source_export_project(root)
            output = root / "loopplane_source.tar.zst"

            with patch("runtime.migration_export.shutil.which", return_value=None):
                result = export_project(project, profile="source", output=output)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["archive"]["requested_compression"], "zstd")
            self.assertEqual(result["archive"]["compression"], "tar")
            self.assertTrue(result["archive"]["fallback"])
            self.assertEqual(result["archive"]["fallback_reason"], "zstd_unavailable")
            self.assertIn(EXPORT_MANIFEST_NAME, list_export_archive_members(output))

    def test_cli_stateful_export_includes_stateful_files_and_excludes_stale_machine_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, paths = prepare_source_export_project(root)
            loopplane_home = root / "loopplane-home"
            write_json(loopplane_home / "runners" / "agent_runners.local.json", {"secret": "home-secret"})
            write_json(loopplane_home / "dashboard" / "servers.json", {"servers": [{"token": "home-token"}]})
            output = root / "loopplane_stateful.tar"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "stateful",
                "--output",
                str(output),
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["profile"], "stateful")
            members = set(list_export_archive_members(output))
            manifest = read_export_archive_manifest(output)
            workflow_root = paths.workflow_root_value.rstrip("/")

            required = {
                EXPORT_MANIFEST_NAME,
                "PROJECT_BRIEF.md",
                "PLAN.md",
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
                f"{workflow_root}/SHARED_CONTEXT.md",
                f"{workflow_root}/config/workflow.json",
                f"{workflow_root}/config/agent_runners.json",
                f"{workflow_root}/planning/runs/plan_001/plan_result.json",
                f"{workflow_root}/requests/control_requests.jsonl",
                f"{workflow_root}/results/T001/latest.json",
                f"{workflow_root}/results/T001/runs/run_001/report.md",
                f"{workflow_root}/results/T001/runs/run_001/validation.json",
                f"{workflow_root}/results/T001/runs/run_001/node_summary.json",
                f"{workflow_root}/results/T001/runs/run_001/agent_status.json",
                f"{workflow_root}/results/T001/runs/run_001/adapter_result.json",
                f"{workflow_root}/results/T001/runs/run_001/commands.sh",
                f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log",
                f"{workflow_root}/results/T001/runs/run_001/logs/stderr.log",
                f"{workflow_root}/results/T001/runs/run_001/artifacts/result.txt",
                f"{workflow_root}/runtime/events/events_000001.jsonl",
                f"{workflow_root}/runtime/snapshots/snapshot_000001.json",
                f"{workflow_root}/runtime/failure_registry.json",
                f"{workflow_root}/runtime/git_checkpoints.jsonl",
                f"{workflow_root}/runtime/evidence_manifest.json",
                f"{workflow_root}/runtime/final_verification_report.json",
            }
            self.assertFalse(required - members)

            forbidden = {
                ".env",
                ".loopplane_home/runners/agent_runners.local.json",
                ".loopplane/config/local/agent_runners.local.json",
                f"{workflow_root}/runtime/lock/scheduler_instance_lock/owner.json",
                f"{workflow_root}/runtime/active_run_leases/run_001.json",
                f"{workflow_root}/runtime/supervisor.json",
                f"{workflow_root}/runtime/supervisor/supervisor_stdout.log",
                f"{workflow_root}/runtime/runs/run_001/run_metadata.json",
                f"{workflow_root}/runtime/dashboard_token",
                f"{workflow_root}/runtime/dashboard_server.json",
                f"{workflow_root}/read_models/workflow_status.json",
                "../service-b/sibling.txt",
                str(loopplane_home / "runners" / "agent_runners.local.json"),
            }
            self.assertFalse(forbidden & members)

            exported_agent_config = json.loads(archive_text(output, f"{workflow_root}/config/agent_runners.json"))
            runner = exported_agent_config["runners"]["worker"]
            self.assertEqual(runner["command"], "codex --danger")
            self.assertEqual(runner["cwd"], "{{project_root}}")
            self.assertEqual(runner["env"], {})
            self.assertEqual(runner["doctor"]["check_command"], "codex --version")

            agent_status = json.loads(archive_text(output, f"{workflow_root}/results/T001/runs/run_001/agent_status.json"))
            adapter_result = json.loads(archive_text(output, f"{workflow_root}/results/T001/runs/run_001/adapter_result.json"))
            snapshot = json.loads(archive_text(output, f"{workflow_root}/runtime/snapshots/snapshot_000001.json"))
            failure_registry = json.loads(archive_text(output, f"{workflow_root}/runtime/failure_registry.json"))
            assert_no_process_keys(self, agent_status)
            assert_no_process_keys(self, adapter_result)
            assert_no_process_keys(self, snapshot)
            assert_no_process_keys(self, failure_registry)
            self.assertEqual(adapter_result["command"], "codex exec '<redacted-local-path>'")
            self.assertEqual(adapter_result["cwd"], "<redacted-local-path>")
            self.assertEqual(adapter_result["stdout_path"], f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log")
            self.assertEqual(adapter_result["adapter_metadata"]["api_token"], "<redacted-for-stateful-migration>")
            self.assertEqual(
                archive_text(output, f"{workflow_root}/results/T001/runs/run_001/commands.sh"),
                "codex exec '<redacted-local-path>'\n",
            )

            event = json.loads(archive_text(output, f"{workflow_root}/runtime/events/events_000001.jsonl"))
            assert_no_process_keys(self, event)
            self.assertEqual(event["data"]["token"], "<redacted-for-stateful-migration>")

            manifest_paths = {record["path"] for record in manifest["files"]}
            excluded = {record["path"]: record["reason"] for record in manifest["excluded_paths"]}
            self.assertIn(f"{workflow_root}/runtime/snapshots/snapshot_000001.json", manifest_paths)
            self.assertIn(f"{workflow_root}/runtime/failure_registry.json", manifest_paths)
            self.assertEqual(excluded[f"{workflow_root}/runtime/background_jobs.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/runtime/supervisor.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/runtime/runs/run_001/run_metadata.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/read_models/workflow_status.json"], "derived_read_model")
            self.assertEqual(excluded[".loopplane_home/runners/agent_runners.local.json"], "loopplane_home_files")
            self.assertIn("stateful_profile", manifest)

    def test_cli_archive_export_includes_read_only_visualization_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, paths = prepare_source_export_project(root)
            loopplane_home = root / "loopplane-home"
            write_json(loopplane_home / "runners" / "agent_runners.local.json", {"secret": "home-secret"})
            write_json(loopplane_home / "dashboard" / "servers.json", {"servers": [{"token": "home-token"}]})
            output = root / "loopplane_archive.tar"
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            result = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "archive",
                "--output",
                str(output),
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["profile"], "archive")
            members = set(list_export_archive_members(output))
            manifest = read_export_archive_manifest(output)
            workflow_root = paths.workflow_root_value.rstrip("/")

            required = {
                EXPORT_MANIFEST_NAME,
                "PROJECT_BRIEF.md",
                "PLAN.md",
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
                f"{workflow_root}/SHARED_CONTEXT.md",
                f"{workflow_root}/config/workflow.json",
                f"{workflow_root}/config/agent_runners.json",
                f"{workflow_root}/planning/runs/plan_001/plan_result.json",
                f"{workflow_root}/requests/control_requests.jsonl",
                f"{workflow_root}/results/T001/latest.json",
                f"{workflow_root}/results/T001/runs/run_001/report.md",
                f"{workflow_root}/results/T001/runs/run_001/validation.json",
                f"{workflow_root}/results/T001/runs/run_001/node_summary.json",
                f"{workflow_root}/results/T001/runs/run_001/agent_status.json",
                f"{workflow_root}/results/T001/runs/run_001/adapter_result.json",
                f"{workflow_root}/results/T001/runs/run_001/commands.sh",
                f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log",
                f"{workflow_root}/results/T001/runs/run_001/logs/stderr.log",
                f"{workflow_root}/results/T001/runs/run_001/artifacts/result.txt",
                f"{workflow_root}/runtime/events/events_000001.jsonl",
                f"{workflow_root}/runtime/snapshots/snapshot_000001.json",
                f"{workflow_root}/runtime/failure_registry.json",
                f"{workflow_root}/runtime/git_checkpoints.jsonl",
                f"{workflow_root}/runtime/evidence_manifest.json",
                f"{workflow_root}/runtime/final_verification_report.json",
            }
            self.assertFalse(required - members)

            forbidden = {
                ".env",
                ".loopplane_home/runners/agent_runners.local.json",
                ".loopplane/config/local/agent_runners.local.json",
                f"{workflow_root}/runtime/lock/scheduler_instance_lock/owner.json",
                f"{workflow_root}/runtime/active_run_leases/run_001.json",
                f"{workflow_root}/runtime/supervisor.json",
                f"{workflow_root}/runtime/supervisor/supervisor_stdout.log",
                f"{workflow_root}/runtime/runs/run_001/run_metadata.json",
                f"{workflow_root}/runtime/dashboard_token",
                f"{workflow_root}/runtime/dashboard_server.json",
                f"{workflow_root}/read_models/workflow_status.json",
                "../service-b/sibling.txt",
                str(loopplane_home / "runners" / "agent_runners.local.json"),
            }
            self.assertFalse(forbidden & members)

            self.assertIn("archive_profile", manifest)
            self.assertNotIn("stateful_profile", manifest)
            self.assertEqual(manifest["migration_intent"]["mode"], "read_only_archive")
            self.assertTrue(manifest["migration_intent"]["import_requires_read_only"])
            self.assertEqual(manifest["migration_intent"]["workflow_status_on_import"], "read_only_imported")
            self.assertFalse(manifest["migration_intent"]["resume_allowed_after_import"])
            self.assertEqual(manifest["archive_profile"]["intended_dashboard_mode"], "read_only")

            exported_agent_config = json.loads(archive_text(output, f"{workflow_root}/config/agent_runners.json"))
            runner = exported_agent_config["runners"]["worker"]
            self.assertEqual(runner["command"], "codex --danger")
            self.assertEqual(runner["cwd"], "{{project_root}}")
            self.assertEqual(runner["env"], {})
            self.assertEqual(runner["doctor"]["check_command"], "codex --version")

            agent_status = json.loads(archive_text(output, f"{workflow_root}/results/T001/runs/run_001/agent_status.json"))
            adapter_result = json.loads(archive_text(output, f"{workflow_root}/results/T001/runs/run_001/adapter_result.json"))
            snapshot = json.loads(archive_text(output, f"{workflow_root}/runtime/snapshots/snapshot_000001.json"))
            failure_registry = json.loads(archive_text(output, f"{workflow_root}/runtime/failure_registry.json"))
            assert_no_process_keys(self, agent_status)
            assert_no_process_keys(self, adapter_result)
            assert_no_process_keys(self, snapshot)
            assert_no_process_keys(self, failure_registry)
            self.assertEqual(adapter_result["command"], "codex exec '<redacted-local-path>'")
            self.assertEqual(adapter_result["cwd"], "<redacted-local-path>")
            self.assertEqual(adapter_result["stdout_path"], f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log")
            self.assertEqual(adapter_result["adapter_metadata"]["api_token"], "<redacted-for-archive-migration>")
            self.assertEqual(
                archive_text(output, f"{workflow_root}/results/T001/runs/run_001/commands.sh"),
                "codex exec '<redacted-local-path>'\n",
            )

            event = json.loads(archive_text(output, f"{workflow_root}/runtime/events/events_000001.jsonl"))
            assert_no_process_keys(self, event)
            self.assertEqual(event["data"]["token"], "<redacted-for-archive-migration>")

            manifest_paths = {record["path"] for record in manifest["files"]}
            manifest_sources = {record["path"]: record["source"] for record in manifest["files"]}
            excluded = {record["path"]: record["reason"] for record in manifest["excluded_paths"]}
            self.assertIn(f"{workflow_root}/runtime/events/events_000001.jsonl", manifest_paths)
            self.assertEqual(
                manifest_sources[f"{workflow_root}/runtime/events/events_000001.jsonl"],
                "sanitized_archive_metadata",
            )
            self.assertEqual(excluded[f"{workflow_root}/runtime/background_jobs.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/runtime/supervisor.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/runtime/runs/run_001/run_metadata.json"], "runtime_process_state")
            self.assertEqual(excluded[f"{workflow_root}/read_models/workflow_status.json"], "derived_read_model")


class StatefulMigrationImportTest(unittest.TestCase):
    def test_cli_stateful_import_restores_project_truth_and_excludes_nonportable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, paths = prepare_source_export_project(root)
            output = root / "loopplane_stateful.tar"
            target = root / "imported-service"
            loopplane_home = root / "loopplane-home"
            write_json(loopplane_home / "runners" / "agent_runners.local.json", {"secret": "home-secret"})
            env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}

            export_result = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "stateful",
                "--output",
                str(output),
                "--json",
                env=env,
            )
            self.assertEqual(export_result.returncode, 0, export_result.stderr + export_result.stdout)

            import_result = run_loopplane(
                "import",
                str(output),
                "--target",
                str(target),
                "--json",
                env=env,
            )

            self.assertEqual(import_result.returncode, 0, import_result.stderr + import_result.stdout)
            payload = json.loads(import_result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "imported")
            self.assertEqual(payload["profile"], "stateful")
            self.assertEqual(payload["workflow_id"], paths.workflow_id)
            self.assertTrue(payload["read_models"]["rebuild_required"])
            self.assertFalse(payload["read_models"]["directory_exists"])
            self.assertIn("doctor-agent", "\n".join(payload["post_import_actions"]))
            self.assertIn("rebuild-read-models", "\n".join(payload["post_import_actions"]))

            workflow_root = paths.workflow_root_value.rstrip("/")
            required = {
                "PROJECT_BRIEF.md",
                "PLAN.md",
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
                f"{workflow_root}/SHARED_CONTEXT.md",
                f"{workflow_root}/config/workflow.json",
                f"{workflow_root}/config/agent_runners.json",
                f"{workflow_root}/planning/runs/plan_001/plan_result.json",
                f"{workflow_root}/requests/control_requests.jsonl",
                f"{workflow_root}/results/T001/latest.json",
                f"{workflow_root}/results/T001/runs/run_001/report.md",
                f"{workflow_root}/results/T001/runs/run_001/validation.json",
                f"{workflow_root}/results/T001/runs/run_001/node_summary.json",
                f"{workflow_root}/results/T001/runs/run_001/agent_status.json",
                f"{workflow_root}/results/T001/runs/run_001/adapter_result.json",
                f"{workflow_root}/results/T001/runs/run_001/commands.sh",
                f"{workflow_root}/results/T001/runs/run_001/logs/stdout.log",
                f"{workflow_root}/results/T001/runs/run_001/logs/stderr.log",
                f"{workflow_root}/results/T001/runs/run_001/artifacts/result.txt",
                f"{workflow_root}/runtime/events/events_000001.jsonl",
                f"{workflow_root}/runtime/snapshots/snapshot_000001.json",
                f"{workflow_root}/runtime/failure_registry.json",
                f"{workflow_root}/runtime/git_checkpoints.jsonl",
                f"{workflow_root}/runtime/evidence_manifest.json",
                f"{workflow_root}/runtime/final_verification_report.json",
            }
            for relative in required:
                self.assertTrue((target / relative).is_file(), relative)

            forbidden = {
                ".env",
                ".loopplane/config/local/agent_runners.local.json",
                f"{workflow_root}/runtime/lock/scheduler_instance_lock/owner.json",
                f"{workflow_root}/runtime/active_run_leases/run_001.json",
                f"{workflow_root}/runtime/supervisor.json",
                f"{workflow_root}/runtime/supervisor/supervisor_stdout.log",
                f"{workflow_root}/runtime/runs/run_001/run_metadata.json",
                f"{workflow_root}/runtime/dashboard_token",
                f"{workflow_root}/runtime/dashboard_server.json",
                f"{workflow_root}/read_models/workflow_status.json",
            }
            for relative in forbidden:
                self.assertFalse((target / relative).exists(), relative)

            state = json.loads((target / workflow_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            background_jobs = json.loads(
                (target / workflow_root / "runtime" / "background_jobs.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["workflow_id"], paths.workflow_id)
            self.assertEqual(state["status"], "waiting_config")
            self.assertEqual(background_jobs["workflow_id"], paths.workflow_id)
            self.assertEqual(background_jobs["jobs"], [])

            source_workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            target_workspace = json.loads((target / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(target_workspace["workspace_id"], source_workspace["workspace_id"])
            self.assertEqual(target_workspace["project_root"], ".")
            self.assertEqual(target_workspace["loopplane_dir"], ".loopplane")
            self.assertEqual(target_workspace["repo_root"], ".")
            registry = json.loads((target / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            current = json.loads((target / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["workspace_id"], target_workspace["workspace_id"])
            self.assertEqual(current["workspace_id"], target_workspace["workspace_id"])
            self.assertEqual(current["current_workflow_id"], paths.workflow_id)
            self.assertIn(paths.workflow_id, {record["workflow_id"] for record in registry["workflows"]})

            exported_agent_config = archive_json(output, f"{workflow_root}/config/agent_runners.json")
            imported_agent_config = json.loads(
                (target / workflow_root / "config" / "agent_runners.json").read_text(encoding="utf-8")
            )
            self.assertEqual(imported_agent_config, exported_agent_config)
            runner = imported_agent_config["runners"]["worker"]
            self.assertEqual(runner["command"], "codex --danger")
            self.assertEqual(runner["cwd"], "{{project_root}}")
            self.assertEqual(runner["env"], {})
            self.assertEqual(runner["doctor"]["check_command"], "codex --version")

            agent_status = json.loads(
                (target / workflow_root / "results" / "T001" / "runs" / "run_001" / "agent_status.json").read_text(
                    encoding="utf-8"
                )
            )
            adapter_result = json.loads(
                (target / workflow_root / "results" / "T001" / "runs" / "run_001" / "adapter_result.json").read_text(
                    encoding="utf-8"
                )
            )
            event = json.loads(
                (target / workflow_root / "runtime" / "events" / "events_000001.jsonl").read_text(encoding="utf-8")
            )
            assert_no_process_keys(self, agent_status)
            assert_no_process_keys(self, adapter_result)
            assert_no_process_keys(self, event)
            self.assertEqual(adapter_result["command"], "codex exec '<redacted-local-path>'")
            self.assertEqual(adapter_result["cwd"], "<redacted-local-path>")
            self.assertEqual(event["data"]["token"], "<redacted-for-stateful-migration>")

            status = run_loopplane("status", "--project", str(target), "--json", env=env)
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["workflow_id"], paths.workflow_id)

            rebuild = run_loopplane("rebuild-read-models", "--project", str(target), "--json", env=env)
            self.assertEqual(rebuild.returncode, 0, rebuild.stderr + rebuild.stdout)
            self.assertTrue((target / workflow_root / "read_models" / "workflow_status.json").is_file())

    def test_stateful_import_refuses_non_empty_target_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _paths = prepare_source_export_project(root)
            output = root / "loopplane_stateful.tar"
            target = root / "existing-target"
            target.mkdir()
            (target / "keep.txt").write_text("do not overwrite\n", encoding="utf-8")

            export_result = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "stateful",
                "--output",
                str(output),
                "--json",
            )
            self.assertEqual(export_result.returncode, 0, export_result.stderr + export_result.stdout)

            import_result = run_loopplane("import", str(output), "--target", str(target), "--json")

            self.assertEqual(import_result.returncode, 2, import_result.stderr + import_result.stdout)
            payload = json.loads(import_result.stdout)
            self.assertEqual(payload["status"], "target_not_empty")
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "do not overwrite\n")
            self.assertFalse((target / ".loopplane").exists())

    def test_archive_read_only_import_marks_workflow_and_blocks_accidental_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, paths = prepare_source_export_project(root)
            source_archive = root / "loopplane_source.tar"
            archive_archive = root / "loopplane_archive.tar"

            source_export = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "source",
                "--output",
                str(source_archive),
                "--json",
            )
            self.assertEqual(source_export.returncode, 0, source_export.stderr + source_export.stdout)
            archive_export = run_loopplane(
                "export",
                "--project",
                str(project),
                "--profile",
                "archive",
                "--output",
                str(archive_archive),
                "--json",
            )
            self.assertEqual(archive_export.returncode, 0, archive_export.stderr + archive_export.stdout)

            source_import = run_loopplane("import", str(source_archive), "--target", str(root / "source-target"), "--json")
            self.assertEqual(source_import.returncode, 2, source_import.stderr + source_import.stdout)
            self.assertEqual(json.loads(source_import.stdout)["status"], "unsupported_profile")

            archive_import = run_loopplane("import", str(archive_archive), "--target", str(root / "archive-target"), "--json")
            self.assertEqual(archive_import.returncode, 2, archive_import.stderr + archive_import.stdout)
            self.assertEqual(json.loads(archive_import.stdout)["status"], "archive_import_requires_read_only")

            read_only_import = run_loopplane(
                "import",
                str(archive_archive),
                "--target",
                str(root / "read-only-target"),
                "--read-only",
                "--json",
            )
            self.assertEqual(read_only_import.returncode, 0, read_only_import.stderr + read_only_import.stdout)
            payload = json.loads(read_only_import.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "imported")
            self.assertEqual(payload["profile"], "archive")
            self.assertEqual(payload["workflow_id"], paths.workflow_id)
            self.assertIn("dashboard", "\n".join(payload["post_import_actions"]))
            self.assertNotIn("resume --project", "\n".join(payload["post_import_actions"]))

            target = root / "read-only-target"
            workflow_root = paths.workflow_root_value.rstrip("/")
            registry = json.loads((target / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            records = registry["workflows"]
            self.assertTrue(records)
            self.assertEqual({record["status"] for record in records}, {"read_only_imported"})
            self.assertTrue(all(record["read_only"] for record in records))
            self.assertFalse(any(record["archived"] for record in records))
            current = json.loads((target / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8"))
            self.assertTrue(current["read_only"])
            self.assertEqual(current["selection_reason"], "read_only_archive_import")

            state = json.loads((target / workflow_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "read_only_imported")
            self.assertTrue(state["read_only"])
            self.assertFalse(state["resume_allowed"])
            self.assertFalse(state["scheduler"]["resume_allowed"])

            resume = run_loopplane("resume", "--project", str(target), "--json")
            self.assertEqual(resume.returncode, 1, resume.stderr + resume.stdout)
            resume_payload = json.loads(resume.stdout)
            self.assertEqual(resume_payload["status"], "read_only_workflow")
            self.assertEqual(resume_payload["request_type"], "resume")
            self.assertFalse((target / workflow_root / "runtime" / "control_requests.jsonl").exists())

            rebuild = run_loopplane("rebuild-read-models", "--project", str(target), "--json")
            self.assertEqual(rebuild.returncode, 0, rebuild.stderr + rebuild.stdout)
            dashboard_dir = root / "read-only-dashboard"
            dashboard = run_loopplane(
                "dashboard",
                "--project",
                str(target),
                "--output",
                str(dashboard_dir),
                "--json",
            )
            self.assertEqual(dashboard.returncode, 0, dashboard.stderr + dashboard.stdout)
            dashboard_payload = json.loads(dashboard.stdout)
            self.assertTrue(dashboard_payload["ok"], dashboard.stdout)
            html = (dashboard_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("read-only", html)
            dashboard_data = dashboard_script_payload(html)
            self.assertEqual(dashboard_data["workflow_id"], paths.workflow_id)
            self.assertEqual(dashboard_data["workspace"]["selected_workflow_id"], paths.workflow_id)
            self.assertEqual(dashboard_data["read_models"]["workflow_status.json"]["status"], "read_only_imported")
            self.assertFalse(dashboard_data["execution_controls"]["mutation_allowed"])
            self.assertIn("read_only", dashboard_data["execution_controls"]["mutation_blockers"])
            self.assertFalse(dashboard_data["planning_controls"]["mutation_allowed"])
            self.assertIn("read_only", dashboard_data["planning_controls"]["mutation_blockers"])

    def test_import_rejects_path_traversal_and_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            traversal_archive = root / "traversal.tar"
            symlink_archive = root / "symlink.tar"

            write_minimal_bad_archive(
                traversal_archive,
                bad_member="../evil.txt",
                bad_data=b"escape\n",
            )
            traversal = import_project_archive(traversal_archive, target=root / "traversal-target")
            self.assertFalse(traversal["ok"])
            self.assertEqual(traversal["status"], "unsafe_archive_member")
            self.assertFalse((root / "evil.txt").exists())

            write_symlink_bad_archive(symlink_archive)
            symlink = import_project_archive(symlink_archive, target=root / "symlink-target")
            self.assertFalse(symlink["ok"])
            self.assertEqual(symlink["status"], "unsafe_archive_member")
            self.assertFalse((root / "symlink-target").exists())


def write_minimal_bad_archive(path: Path, *, bad_member: str, bad_data: bytes) -> None:
    manifest = {
        "schema_version": "loopplane-migration-export-1",
        "profile": "stateful",
        "files": [
            {
                "path": bad_member,
                "category": "project_source",
                "source": "filesystem",
                "size": len(bad_data),
                "sha256": sha256_bytes(bad_data),
            }
        ],
    }
    with tarfile.open(path, "w") as archive:
        add_bytes_to_tar(archive, EXPORT_MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))
        add_bytes_to_tar(archive, bad_member, bad_data)


def write_symlink_bad_archive(path: Path) -> None:
    data = b"brief\n"
    manifest = {
        "schema_version": "loopplane-migration-export-1",
        "profile": "stateful",
        "files": [
            {
                "path": "PROJECT_BRIEF.md",
                "category": "root_project_file",
                "source": "filesystem",
                "size": len(data),
                "sha256": sha256_bytes(data),
            }
        ],
    }
    with tarfile.open(path, "w") as archive:
        add_bytes_to_tar(archive, EXPORT_MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))
        info = tarfile.TarInfo("PROJECT_BRIEF.md")
        info.type = tarfile.SYMTYPE
        info.linkname = "/tmp/not-portable"
        archive.addfile(info)


def add_bytes_to_tar(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, fileobj=io.BytesIO(data))


def sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def event_record_with_hash(record: dict[str, object]) -> dict[str, object]:
    payload = dict(record)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["event_hash"] = "sha256:" + sha256(encoded).hexdigest()
    return payload


if __name__ == "__main__":
    unittest.main()
