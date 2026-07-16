from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from runtime.exit_codes import EXIT_HEALTH_FAILURE
from runtime.health import health_exit_code, run_health_probe
from runtime.init_workflow import LAYOUT_CANONICAL_V16, init_project
from runtime.scheduler import append_event, load_scheduler_context


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def timestamp(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_by_name(result: dict[str, object], name: str) -> dict[str, object]:
    for check in result["checks"]:
        if isinstance(check, dict) and check.get("name") == name:
            return check
    raise AssertionError(f"missing check {name!r}: {json.dumps(result, indent=2, sort_keys=True)}")


class HealthProbeTest(unittest.TestCase):
    def test_initialized_project_health_is_healthy_and_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should inspect runtime truth.")

            result = run_health_probe(project, write=True)

            self.assertEqual(result["status"], "healthy", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(health_exit_code(result), 0)
            expected_checks = {
                "schema_validation",
                "scheduler_lock",
                "active_run_leases",
                "runner_liveness",
                "machine_runner_locks",
                "background_jobs",
                "agent_status_files",
                "validations",
                "completion_marker_freshness",
                "failure_registry",
                "expansion_registry",
                "git_checkpoints",
                "event_segments",
                "read_models",
            }
            actual_checks = {check["name"] for check in result["checks"]}
            self.assertEqual(actual_checks, expected_checks)
            report_path = project / ".loopplane" / "runtime" / "health_report.json"
            self.assertTrue(report_path.is_file())
            written = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "healthy")

    def test_health_can_target_registered_workflow_without_switching_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Health workflow selection.", layout=LAYOUT_CANONICAL_V16)
            current_before = (project / ".loopplane" / "current_workflow.json").read_bytes()

            result = run_health_probe(project, workflow_id=initialized.workflow_id)

            self.assertEqual(result["workflow_id"], initialized.workflow_id)
            self.assertEqual(result["status"], "healthy", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual((project / ".loopplane" / "current_workflow.json").read_bytes(), current_before)

    def test_health_cli_all_reports_workspace_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Health all workflow selection.", layout=LAYOUT_CANONICAL_V16)

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "health", "--project", str(project), "--all", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["workflow_count"], 1)
            self.assertEqual(payload["workflow_results"][0]["workflow_id"], initialized.workflow_id)

    def test_satisfied_agent_status_is_health_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should accept satisfied worker status.")
            write_json(
                project / ".loopplane" / "results" / "T001" / "runs" / "run_satisfied" / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_satisfied",
                    "status": "satisfied",
                    "next_prompt_ready": True,
                },
            )

            result = run_health_probe(project)

            self.assertEqual(result["status"], "healthy", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(check_by_name(result, "agent_status_files")["status"], "pass")

    def test_complete_agent_status_alias_is_health_warning_not_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should accept complete as a status alias.")
            write_json(
                project / ".loopplane" / "results" / "T001" / "runs" / "run_complete" / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_complete",
                    "status": "complete",
                    "next_prompt_ready": True,
                },
            )

            result = run_health_probe(project)

            self.assertEqual(result["status"], "healthy_with_warnings", json.dumps(result, indent=2, sort_keys=True))
            check = check_by_name(result, "agent_status_files")
            self.assertEqual(check["status"], "warn")
            self.assertIn("accepted as alias", "\n".join(check["details"]["warnings"]))  # type: ignore[index]

    def test_compatible_old_agent_status_schema_is_health_warning_not_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should accept compatible old worker schema.")
            write_json(
                project / ".loopplane" / "results" / "T001" / "runs" / "run_old_schema" / "agent_status.json",
                {
                    "schema_version": "1.0",
                    "run_id": "run_old_schema",
                    "status": "completed",
                    "next_prompt_ready": True,
                },
            )

            result = run_health_probe(project)

            self.assertEqual(result["status"], "healthy_with_warnings", json.dumps(result, indent=2, sort_keys=True))
            check = check_by_name(result, "agent_status_files")
            self.assertEqual(check["status"], "warn")
            self.assertIn("accepted as compatible", "\n".join(check["details"]["warnings"]))  # type: ignore[index]

    def test_recovered_historical_malformed_agent_status_is_health_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Recovered historical worker status should not degrade workflow health.")
            rel_status = ".loopplane/results/T001/runs/run_bad/agent_status.json"
            write_json(
                project / rel_status,
                {
                    "schema_version": "1.5",
                    "run_id": "run_bad",
                    "status": "almost_completed",
                    "next_prompt_ready": True,
                },
            )
            registry_path = project / ".loopplane" / "runtime" / "failure_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["failures"] = [
                {
                    "failure_id": "fail_recovered",
                    "task_id": "T001",
                    "run_id": "run_bad",
                    "status": "recovered",
                    "failure_class": "worker_failed",
                    "failure_signature": "worker_agent_status:almost_completed",
                    "agent_status_path": rel_status,
                }
            ]
            write_json(registry_path, registry)

            result = run_health_probe(project)

            self.assertEqual(result["status"], "healthy_with_warnings", json.dumps(result, indent=2, sort_keys=True))
            check = check_by_name(result, "agent_status_files")
            self.assertEqual(check["status"], "warn")
            self.assertIn("historical recovered run has status", "\n".join(check["details"]["warnings"]))  # type: ignore[index]

    def test_cli_json_strict_and_write_for_stale_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health CLI should expose JSON and strict mode.")
            write_json(
                project / ".loopplane" / "runtime" / "plan_loop_complete.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_stale",
                    "status": "completed",
                    "plan_sha256": "sha256:not-current",
                },
            )

            normal = subprocess.run(
                [sys.executable, str(LoopPlane), "health", "--project", str(project), "--json", "--write"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            strict = subprocess.run(
                [sys.executable, str(LoopPlane), "health", "--project", str(project), "--json", "--strict"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(normal.returncode, 0, normal.stderr + normal.stdout)
            self.assertEqual(strict.returncode, EXIT_HEALTH_FAILURE, strict.stderr + strict.stdout)
            payload = json.loads(normal.stdout)
            self.assertEqual(payload["status"], "healthy_with_warnings")
            self.assertEqual(check_by_name(payload, "completion_marker_freshness")["status"], "warn")
            self.assertTrue((project / ".loopplane" / "runtime" / "health_report.json").is_file())

    def test_health_warns_when_only_derived_read_model_schema_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should not fail on stale read-model schema.")
            vc_status = project / ".loopplane" / "read_models" / "version_control_status.json"
            payload = json.loads(vc_status.read_text(encoding="utf-8"))
            payload.pop("source_hashes", None)
            write_json(vc_status, payload)

            result = run_health_probe(project)

            schema_check = check_by_name(result, "schema_validation")
            self.assertEqual(result["status"], "healthy_with_warnings", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(schema_check["status"], "warn")
            self.assertIn("rebuild-read-models", schema_check["message"])

    def test_health_reports_stale_machine_runner_lock_with_recovery_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "home"
            init_project(project, "Health should surface stale machine runner locks.")
            self._configure_machine_runner_lock(project, "worker", "shared_codex")
            lock_path = home / "locks" / "runner_locks" / "shared_codex.lock"
            write_json(
                lock_path,
                {
                    "schema_version": "1.6",
                    "lock_type": "runner_resource",
                    "lock_scope": "machine",
                    "lock_key": "shared_codex",
                    "lock_path": lock_path.as_posix(),
                    "global_concurrency_limit": 1,
                    "queue_when_busy": True,
                    "run_id": "run_dead",
                    "workflow_id": "wf_dead",
                    "runner_id": "worker",
                    "role": "worker",
                    "pid": 99999999,
                    "acquired_at": "2000-01-01T00:00:00Z",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                },
            )
            truth_before = self._project_truth_hashes(project)

            with patch.dict(os.environ, {"LOOPPLANE_HOME": home.as_posix()}):
                result = run_health_probe(project)

            check = check_by_name(result, "machine_runner_locks")
            self.assertEqual(result["status"], "degraded", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(check["status"], "fail")
            self.assertIn("Remove stale lock", check["message"])
            self.assertEqual(check["details"]["stale"][0]["lock_key"], "shared_codex")  # type: ignore[index]
            self.assertEqual(check["details"]["stale"][0]["state"], "stale")  # type: ignore[index]
            self.assertTrue(lock_path.is_file())
            self.assertEqual(self._project_truth_hashes(project), truth_before)

    def test_health_reports_malformed_machine_runner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "home"
            init_project(project, "Health should surface malformed machine runner locks.")
            self._configure_machine_runner_lock(project, "worker", "shared_bad")
            lock_path = home / "locks" / "runner_locks" / "shared_bad.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("{not-json\n", encoding="utf-8")

            with patch.dict(os.environ, {"LOOPPLANE_HOME": home.as_posix()}):
                result = run_health_probe(project)

            check = check_by_name(result, "machine_runner_locks")
            self.assertEqual(result["status"], "degraded", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(check["status"], "fail")
            self.assertIn("Malformed", check["message"])
            self.assertEqual(check["details"]["malformed"][0]["lock_key"], "shared_bad")  # type: ignore[index]
            self.assertEqual(check["details"]["malformed"][0]["state"], "malformed")  # type: ignore[index]

    def test_health_does_not_fail_live_machine_runner_lock_with_old_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "home"
            init_project(project, "Health should not flag live machine runner locks as stale.")
            self._configure_machine_runner_lock(project, "worker", "shared_live")
            lock_path = home / "locks" / "runner_locks" / "shared_live.lock"
            write_json(
                lock_path,
                {
                    "schema_version": "1.6",
                    "lock_type": "runner_resource",
                    "lock_scope": "machine",
                    "lock_key": "shared_live",
                    "lock_path": lock_path.as_posix(),
                    "global_concurrency_limit": 1,
                    "queue_when_busy": True,
                    "run_id": "run_live",
                    "workflow_id": "wf_live",
                    "runner_id": "worker",
                    "role": "worker",
                    "pid": os.getpid(),
                    "acquired_at": "2000-01-01T00:00:00Z",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                },
            )

            with patch.dict(os.environ, {"LOOPPLANE_HOME": home.as_posix()}):
                result = run_health_probe(project)

            check = check_by_name(result, "machine_runner_locks")
            self.assertEqual(result["status"], "healthy", json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(check["status"], "pass")
            self.assertEqual(check["details"]["active"][0]["lock_key"], "shared_live")  # type: ignore[index]
            self.assertEqual(check["details"]["active"][0]["state"], "active")  # type: ignore[index]

    def test_health_fixture_matrix_classifies_runtime_records(self) -> None:
        fixtures = [
            ("stale_scheduler_lock", self._stale_scheduler_lock, "scheduler_lock", "fail", "degraded"),
            ("stale_active_lease", self._stale_active_lease, "active_run_leases", "fail", "degraded"),
            ("dead_runner_pid", self._dead_runner_pid, "runner_liveness", "fail", "degraded"),
            ("stale_background_job", self._stale_background_job, "background_jobs", "fail", "degraded"),
            ("malformed_background_status", self._malformed_background_status, "background_jobs", "fail", "degraded"),
            ("malformed_agent_status", self._malformed_agent_status, "agent_status_files", "fail", "degraded"),
            ("malformed_validation", self._malformed_validation, "validations", "fail", "degraded"),
            ("malformed_failure_registry", self._malformed_failure_registry, "failure_registry", "fail", "unhealthy"),
            ("missing_git_checkpoint_ref", self._missing_git_checkpoint_ref, "git_checkpoints", "fail", "degraded"),
            ("nonmonotonic_events", self._nonmonotonic_events, "event_segments", "fail", "unhealthy"),
            ("tampered_event_hash_chain", self._tampered_event_hash_chain, "event_segments", "fail", "unhealthy"),
            ("missing_read_model", self._missing_read_model, "read_models", "warn", "healthy_with_warnings"),
        ]
        for name, setup, check_name, check_status, overall_status in fixtures:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    init_project(project, f"Health matrix fixture {name}.")
                    setup(project)

                    result = run_health_probe(project)

                    self.assertEqual(result["status"], overall_status, json.dumps(result, indent=2, sort_keys=True))
                    self.assertEqual(check_by_name(result, check_name)["status"], check_status)
                    self.assertTrue(result["requires_attention"])

    def test_nonblocking_inspector_lease_does_not_degrade_workflow_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Health should ignore external inspector lease.")
            write_json(
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_inspector.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_inspector",
                    "task_id": None,
                    "role": "inspector",
                    "runner_id": "inspector",
                    "status": "running",
                    "blocks_scheduler": False,
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                },
            )

            result = run_health_probe(project)

            self.assertEqual(result["status"], "healthy", json.dumps(result, indent=2, sort_keys=True))
            self.assertFalse(result["requires_attention"])
            active_check = check_by_name(result, "active_run_leases")
            self.assertEqual(active_check["status"], "pass")
            self.assertTrue(active_check["details"]["external_nonblocking"])
            self.assertEqual(check_by_name(result, "runner_liveness")["status"], "pass")

    def test_fresh_owner_lease_covers_stale_scheduler_lock_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Fresh active lease covers a blocking scheduler tick.")
            now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            write_json(
                project / ".loopplane" / "runtime" / "lock" / "scheduler_instance_lock" / "owner.json",
                {
                    "owner": f"test:{os.getpid()}:owner",
                    "pid": os.getpid(),
                    "started_at": "2000-01-01T00:00:00Z",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "ttl_seconds": 1,
                },
            )
            write_json(
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_live.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_live",
                    "task_id": "T001",
                    "role": "worker",
                    "runner_id": "worker",
                    "status": "running",
                    "blocks_scheduler": True,
                    "heartbeat_at": now,
                    "lease_ttl_seconds": 120,
                    "adapter_pid": os.getpid(),
                    "adapter_child_pid": os.getpid(),
                    "scheduler_pid": os.getpid(),
                },
            )

            result = run_health_probe(project)

            check = check_by_name(result, "scheduler_lock")
            self.assertEqual(check["status"], "pass", json.dumps(result, indent=2, sort_keys=True))
            self.assertTrue(check["details"]["heartbeat_covered_by_active_run_lease"])
            self.assertEqual(check["details"]["active_run_lease"]["run_id"], "run_live")

    def _stale_scheduler_lock(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "runtime" / "lock" / "scheduler_instance_lock" / "owner.json",
            {
                "owner": "test-owner",
                "started_at": "2000-01-01T00:00:00Z",
                "heartbeat_at": "2000-01-01T00:00:00Z",
                "ttl_seconds": 1,
            },
        )

    def _stale_active_lease(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "runtime" / "active_run_leases" / "run_stale.json",
            {
                "schema_version": "1.5",
                "run_id": "run_stale",
                "task_id": "T001",
                "role": "worker",
                "status": "running",
                "heartbeat_at": "2000-01-01T00:00:00Z",
                "lease_expires_at": "2000-01-01T00:00:01Z",
            },
        )

    def _dead_runner_pid(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "runtime" / "active_run_leases" / "run_dead_pid.json",
            {
                "schema_version": "1.5",
                "run_id": "run_dead_pid",
                "task_id": "T001",
                "role": "worker",
                "status": "running",
                "heartbeat_at": timestamp(),
                "lease_expires_at": timestamp(timedelta(minutes=5)),
                "adapter_pid": 99999999,
            },
        )

    def _stale_background_job(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "runtime" / "background_jobs.json",
            {
                "schema_version": "1.5",
                "jobs": [
                    {
                        "job_id": "bg_stale",
                        "task_id": "T001",
                        "run_id": "run_bg",
                        "status": "running",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                    }
                ],
            },
        )

    def _malformed_background_status(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "runtime" / "background_jobs.json",
            {
                "schema_version": "1.5",
                "jobs": [
                    {
                        "job_id": "bg_bad_status",
                        "task_id": "T001",
                        "run_id": "run_bg_bad",
                        "status": "almost_done",
                        "heartbeat_at": timestamp(),
                    }
                ],
            },
        )

    def _malformed_agent_status(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "results" / "T001" / "runs" / "run_bad" / "agent_status.json",
            {
                "schema_version": "1.5",
                "run_id": "run_bad",
                "status": "not-a-worker-status",
                "next_prompt_ready": "yes",
            },
        )

    def _malformed_validation(self, project: Path) -> None:
        write_json(
            project / ".loopplane" / "results" / "T001" / "runs" / "run_bad" / "validation.json",
            {
                "schema_version": "1.5",
                "run_id": "run_bad",
                "status": "historical",
            },
        )

    def _malformed_failure_registry(self, project: Path) -> None:
        (project / ".loopplane" / "runtime" / "failure_registry.json").write_text("{not json\n", encoding="utf-8")

    def _missing_git_checkpoint_ref(self, project: Path) -> None:
        checkpoint_log = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
        checkpoint_log.write_text(
            json.dumps(
                {
                    "schema_version": "1.5",
                    "checkpoint_id": "gitcp_missing",
                    "status": "created",
                    "ref": "refs/loopplane/wf_missing/checkpoints/gitcp_missing",
                    "commit": "abc123",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _nonmonotonic_events(self, project: Path) -> None:
        events = project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl"
        events.write_text(
            json.dumps({"schema_version": "1.5", "sequence": 2, "timestamp": timestamp(), "event_type": "second"})
            + "\n"
            + json.dumps({"schema_version": "1.5", "sequence": 1, "timestamp": timestamp(), "event_type": "first"})
            + "\n",
            encoding="utf-8",
        )

    def _tampered_event_hash_chain(self, project: Path) -> None:
        context_result = load_scheduler_context(project)
        if not context_result["ok"]:
            raise AssertionError(context_result)
        context = context_result["context"]
        append_event(context.paths, workflow_id=context.workflow_id, event_type="first", data={}, snapshot_interval=None)
        append_event(context.paths, workflow_id=context.workflow_id, event_type="second", data={}, snapshot_interval=None)
        events = project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl"
        records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line.strip()]
        records[1]["payload"]["tampered"] = True
        events.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")

    def _missing_read_model(self, project: Path) -> None:
        (project / ".loopplane" / "read_models" / "workflow_status.json").unlink()

    def _configure_machine_runner_lock(self, project: Path, runner_id: str, lock_key: str) -> None:
        config_path = project / ".loopplane" / "config" / "agent_runners.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["runners"][runner_id]["resource_policy"] = {
            "global_concurrency_limit": 1,
            "lock_scope": "machine",
            "lock_key": lock_key,
            "queue_when_busy": True,
        }
        config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _project_truth_hashes(self, project: Path) -> dict[str, bytes]:
        relative_paths = (
            ".loopplane/workspace.json",
            ".loopplane/workflow_registry.json",
            ".loopplane/current_workflow.json",
            ".loopplane/config/workflow.json",
            ".loopplane/config/agent_runners.json",
        )
        return {relative: (project / relative).read_bytes() for relative in relative_paths if (project / relative).exists()}


if __name__ == "__main__":
    unittest.main()
