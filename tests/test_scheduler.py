from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from runtime.exit_codes import (
    EXIT_GENERIC_FAILURE,
    EXIT_PLAN_MALFORMED,
    EXIT_RUNNER_UNAVAILABLE,
    EXIT_SECURITY_POLICY_VIOLATION,
    EXIT_WAITING_BACKGROUND_JOB,
)
from runtime.init_workflow import init_project
from runtime.prompt_builder import PromptBuildError
from runtime.scheduler import (
    EXIT_DUPLICATE_SCHEDULER,
    AtomicOwnerLock,
    SchedulerLockError,
    append_event,
    load_event_log_projection,
    load_latest_event_snapshot,
    load_scheduler_context,
    load_scheduler_snapshot,
    prepare_run,
    preview_scheduler,
    format_scheduler_text,
    replay_events_after_snapshot,
    run_scheduler,
    select_next_action,
    _upsert_failure,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
CLI_ADAPTER_FIXTURE_BIN = REPO_ROOT / "tests" / "fixtures" / "cli_adapters" / "bin"


def write_active_plan(
    project: Path,
    task_statuses: dict[str, str],
    *,
    approval: str = "not_required",
    max_attempts: int = 3,
) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    first_status = task_statuses.get("P0.T001", " ")
    second_status = task_statuses.get("P1.T001", " ")
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Scheduler Fixture

- [{first_status}] P0.T001: First task
  - acceptance: First task acceptance.
  - evidence: .loopplane/results/P0.T001/
  - latest: .loopplane/results/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md; command_exit_code: 0
  - max_attempts: {max_attempts}
  - approval: {approval}
  - deliverables: First task output.

- [{second_status}] P1.T001: Second task
  - acceptance: Second task acceptance.
  - evidence: .loopplane/results/P1.T001/
  - latest: .loopplane/results/P1.T001/latest.json
  - depends_on: [P0.T001]
  - risk: low
  - validation: file_exists: report.md; command_exit_code: 0
  - max_attempts: {max_attempts}
  - approval: not_required
  - deliverables: Second task output.
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def record_accepted_plan_hash(project: Path) -> str:
    plan_sha = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
    state_path = project / ".loopplane" / "runtime" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_plan_sha256"] = plan_sha
    state["configuration_problems"] = [
        problem
        for problem in state.get("configuration_problems", [])
        if isinstance(problem, dict) and problem.get("code") != "manual_plan_change_detected"
    ]
    state.pop("manual_plan_change", None)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan_sha


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_approval_enabled(project: Path, enabled: bool) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["approval"]["enabled"] = enabled
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def event_hash(record: dict[str, object]) -> str:
    payload = dict(record)
    payload.pop("event_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def configure_shell_worker(
    project: Path,
    script_path: Path,
    *,
    timeout_seconds: int = 10,
    resource_policy: dict[str, object] | None = None,
) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    runner = config["runners"]["worker"]
    runner["adapter"] = "shell"
    runner["command"] = sys.executable
    runner["args"] = [script_path.as_posix()]
    runner["cwd"] = "{{project_root}}"
    runner["prompt_delivery"] = {"mode": "stdin"}
    runner["timeout_seconds"] = timeout_seconds
    runner["stream_logs"] = True
    runner["doctor"] = {"check_command": f"{sys.executable} --version", "requires_auth": False}
    if resource_policy is not None:
        runner["resource_policy"] = resource_policy
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_codex_failure_to_claude_recovery(project: Path) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    codex = config["runners"]["worker"]
    codex["adapter"] = "codex_cli"
    codex["command"] = "codex"
    codex["args"] = ["--fail"]
    codex["cwd"] = "{{project_root}}"
    codex["prompt_delivery"] = {"mode": "file_argument", "argument_template": "{{prompt_path}}"}
    codex["timeout_seconds"] = 10
    codex["stream_logs"] = True
    codex["doctor"] = {"check_command": "codex --version", "requires_auth": False}

    claude = config["runners"]["worker_fallback"]
    claude["enabled"] = True
    claude["role"] = "worker"
    claude["adapter"] = "claude_code_cli"
    claude["command"] = "claude"
    claude["args"] = []
    claude["cwd"] = "{{project_root}}"
    claude["prompt_delivery"] = {"mode": "stdin_or_prompt_flag", "prompt_file": "{{prompt_path}}"}
    claude["timeout_seconds"] = 10
    claude["stream_logs"] = True
    claude["doctor"] = {"check_command": "claude --version", "requires_auth": False}
    config["runner_failover"] = {
        "worker": {
            "strategy": "ordered",
            "runners": ["worker", "worker_fallback"],
            "mark_unhealthy_after": 4,
            "failure_window_seconds": 900,
        }
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_codex_usage_limit_worker(project: Path) -> None:
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    codex = config["runners"]["worker"]
    codex["adapter"] = "codex_cli"
    codex["command"] = "codex"
    codex["args"] = ["--usage-limit"]
    codex["cwd"] = "{{project_root}}"
    codex["prompt_delivery"] = {"mode": "file_argument", "argument_template": "{{prompt_path}}"}
    codex["timeout_seconds"] = 10
    codex["stream_logs"] = True
    codex["doctor"] = {"check_command": "codex --version", "requires_auth": False}
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def timestamp(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_worker_script(project: Path, name: str, body: str) -> Path:
    script = project / name
    script.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return script


def install_cli_adapter_fixture_bin(root: Path) -> Path:
    bin_dir = root / "fixture-bin"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        target = bin_dir / name
        shutil.copy2(CLI_ADAPTER_FIXTURE_BIN / name, target)
        target.chmod(target.stat().st_mode | 0o111)
    return bin_dir


def authoritative_file_hashes(project: Path) -> dict[str, str]:
    roots = [
        project / ".loopplane" / "runtime",
        project / ".loopplane" / "read_models",
        project / ".loopplane" / "results",
        project / ".git",
    ]
    hashes: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[path.relative_to(project).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return hashes


class SchedulerSelectionTest(unittest.TestCase):
    def test_scheduler_text_includes_worker_evidence_paths(self) -> None:
        text = format_scheduler_text(
            {
                "status": "failed",
                "message": "Worker adapter exited with code 1.",
                "selected_action": {
                    "action": "run_worker",
                    "selected": {"task_id": "P0.T001", "runner_id": "worker", "role": "worker"},
                    "execution_result": {
                        "run_id": "run_fixture",
                        "run_dir": ".loopplane/results/P0.T001/runs/run_fixture",
                        "task_evidence_run_dir": ".loopplane/results/P0.T001/runs/run_fixture",
                        "scheduler_run_dir": ".loopplane/runtime/runs/run_fixture",
                        "role_output_dir": ".loopplane/results/P0.T001/runs/run_fixture",
                        "agent_status_path": ".loopplane/results/P0.T001/runs/run_fixture/agent_status.json",
                        "adapter_result_path": ".loopplane/runtime/runs/run_fixture/adapter_result.json",
                    },
                },
            }
        )

        self.assertIn("run_dir: .loopplane/results/P0.T001/runs/run_fixture", text)
        self.assertIn("scheduler_run_dir: .loopplane/runtime/runs/run_fixture", text)
        self.assertIn("adapter_result_path: .loopplane/runtime/runs/run_fixture/adapter_result.json", text)

    def test_control_request_is_selected_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Schedule control first.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            append_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl", {"request_id": "ctrl1", "action": "pause"})

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "handle_control_request")
            self.assertEqual(action["selected"]["request_id"], "ctrl1")

    def test_control_request_without_id_is_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Consume malformed control request once.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            append_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl", {"action": "pause"})

            selected = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(selected["action"], "handle_control_request")
            self.assertTrue(str(selected["selected"]["request_id"]).startswith("control_missing_id_"))

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            responses = read_jsonl(project / ".loopplane" / "runtime" / "control_responses.jsonl")
            synthetic_id = responses[-1]["request_id"]
            self.assertTrue(str(synthetic_id).startswith("control_missing_id_"))
            self.assertEqual(result["selected_action"]["execution_result"]["request_id"], synthetic_id)

            next_action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(next_action["action"], "wait_paused")
            self.assertEqual(next_action["selected"]["last_control_request_id"], synthetic_id)

    def test_waiting_approval_is_selected_before_background_or_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Wait for approval.")
            set_approval_enabled(project, True)
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            append_jsonl(
                project / ".loopplane" / "runtime" / "human_approval_requests.jsonl",
                {"approval_id": "appr1", "status": "pending", "task_id": "P0.T001"},
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "wait_approval")
            self.assertTrue(action["would_wait"])
            self.assertEqual(action["selected"]["approval_id"], "appr1")

    def test_approval_disabled_required_task_requires_attention_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval disabled blocks approval-required task.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "}, approval="required")

            snapshot = load_scheduler_snapshot(project)
            action = select_next_action(snapshot)
            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(action["action"], "requires_attention")
            self.assertTrue(action["would_wait"])
            self.assertEqual(action["selected"]["task_id"], "P0.T001")
            self.assertEqual(action["selected"]["type"], "approval_disabled")
            self.assertEqual(action["selected"]["run_kind"], "approval_disabled")
            self.assertIn("interactive approvals are disabled", action["reason"])
            self.assertEqual(read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl"), [])
            self.assertEqual(result["selected_action"]["action"], "requires_attention")
            self.assertEqual(result["exit_code"], EXIT_SECURITY_POLICY_VIOLATION)
            self.assertEqual(result["stopped_reason"], "requires_attention")
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001" / "runs").exists())

            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "requires_attention")
            self.assertEqual(state["requires_attention"][0]["type"], "approval_disabled")

    def test_approval_disabled_gate_precedes_stale_background_and_unrecovered_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval disabled remains visible after a bad worker attempt.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "}, approval="required")
            write_json(
                project / ".loopplane" / "runtime" / "failure_registry.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "failures": [
                        {
                            "failure_id": "fail_human_blocked",
                            "task_id": "P0.T001",
                            "run_id": "run_human_blocked",
                            "status": "unrecovered",
                            "failure_class": "worker_failed",
                            "failure_signature": "worker_agent_status:blocked_needs_human:exit_0:timed_out_false",
                        }
                    ],
                },
            )
            write_json(
                project / ".loopplane" / "runtime" / "background_jobs.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "jobs": [
                        {
                            "job_id": "bg_human_blocked",
                            "task_id": "P0.T001",
                            "run_id": "run_human_blocked",
                            "status": "stale",
                            "next_prompt_ready": False,
                        }
                    ],
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "requires_attention")
            self.assertEqual(action["selected"]["type"], "approval_disabled")
            self.assertEqual(action["selected"]["task_id"], "P0.T001")
            self.assertIn("approval_required", action["blocking_conditions"])

    def test_approval_enabled_required_task_creates_request_and_approval_allows_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval enabled creates request.")
            set_approval_enabled(project, True)
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "}, approval="required")

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["selected_action"]["action"], "wait_approval")
            requests = read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl")
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertTrue(str(request["approval_id"]).startswith("approval_"))
            self.assertEqual(request["status"], "pending")
            self.assertEqual(request["workflow_id"], json.loads((project / ".loopplane" / "config" / "workflow.json").read_text())["workflow_id"])
            self.assertEqual(request["task_id"], "P0.T001")
            self.assertEqual(request["type"], "task_execution")
            self.assertEqual(request["scope"], "P0.T001 only")
            self.assertIn("expires_at", request)

            append_jsonl(
                project / ".loopplane" / "runtime" / "human_approval_responses.jsonl",
                {
                    "schema_version": "1.5",
                    "approval_id": request["approval_id"],
                    "responded_at": timestamp(),
                    "decision": "approved",
                    "approved_by": "tester",
                    "scope": "P0.T001 only",
                    "task_id": "P0.T001",
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P0.T001")

    def test_closed_approval_decisions_require_attention_instead_of_running_or_waiting(self) -> None:
        for decision in ("rejected", "expired", "superseded"):
            with self.subTest(decision=decision):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    init_project(project, f"Approval {decision} fixture.")
                    set_approval_enabled(project, True)
                    write_active_plan(project, {"P0.T001": " ", "P1.T001": " "}, approval="required")
                    append_jsonl(
                        project / ".loopplane" / "runtime" / "human_approval_requests.jsonl",
                        {
                            "schema_version": "1.5",
                            "approval_id": "approval_closed",
                            "created_at": timestamp(),
                            "workflow_id": "wf_test",
                            "task_id": "P0.T001",
                            "run_id": "run_test",
                            "type": "task_execution",
                            "message": "Approve closed fixture.",
                            "scope": "P0.T001 only",
                            "expires_at": timestamp(timedelta(hours=1)),
                            "status": "pending",
                        },
                    )
                    append_jsonl(
                        project / ".loopplane" / "runtime" / "human_approval_responses.jsonl",
                        {
                            "schema_version": "1.5",
                            "approval_id": "approval_closed",
                            "responded_at": timestamp(),
                            "decision": decision,
                            "approved_by": "tester",
                            "scope": "P0.T001 only",
                            "task_id": "P0.T001",
                        },
                    )

                    action = select_next_action(load_scheduler_snapshot(project))

                    self.assertEqual(action["action"], "requires_attention")
                    self.assertEqual(action["selected"]["approval_decision"], decision)
                    self.assertEqual(action["selected"]["task_id"], "P0.T001")

    def test_waiting_background_job_is_selected_before_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Wait for background job.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            (project / ".loopplane" / "runtime" / "background_jobs.json").write_text(
                json.dumps([{"job_id": "job1", "status": "running", "next_prompt_ready": False}]),
                encoding="utf-8",
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "wait_background_job")
            self.assertEqual(action["selected"]["job_id"], "job1")

    def test_malformed_background_status_waits_and_live_tick_persists_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed background status waits.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            registry_path = project / ".loopplane" / "runtime" / "background_jobs.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "jobs": [
                            {
                                "job_id": "bg_bad_status",
                                "task_id": "P0.T001",
                                "run_id": "run_bad_status",
                                "status": "definitely_not_allowed",
                                "next_prompt_ready": True,
                                "heartbeat_at": timestamp(),
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "wait_background_job")
            self.assertEqual(action["selected"]["job"]["status"], "needs_recovery")
            self.assertFalse(action["selected"]["job"]["next_prompt_ready"])

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], EXIT_WAITING_BACKGROUND_JOB, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["selected_action"]["action"], "wait_background_job")
            self.assertEqual(result["stopped_reason"], "wait_background_job")
            persisted = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["jobs"][0]["status"], "needs_recovery")
            self.assertEqual(persisted["jobs"][0]["status_problem"], "invalid_status:definitely_not_allowed")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertEqual(event_types, ["scheduler_wait_tick"])
            self.assertEqual(events[0]["payload"]["action"], "wait_background_job")
            self.assertEqual(events[0]["payload"]["status"], "waiting_background_job")

    def test_stale_background_heartbeat_waits_before_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Stale background heartbeat waits.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            (project / ".loopplane" / "runtime" / "background_jobs.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "jobs": [
                            {
                                "job_id": "bg_stale",
                                "task_id": "P0.T001",
                                "run_id": "run_bg_stale",
                                "status": "running",
                                "heartbeat_at": "2000-01-01T00:00:00Z",
                                "wake_next_agent_when": "Continue after stale job is recovered.",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "wait_background_job")
            self.assertEqual(action["selected"]["job"]["status"], "stale")
            self.assertEqual(action["selected"]["job"]["status_problem"], "stale_heartbeat")

    def test_inferred_completed_background_job_without_next_prompt_ready_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Completed inferred background jobs should not block.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": " "})
            run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_bg_completed"
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                run_dir / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_bg_completed",
                    "task_id": "P0.T001",
                    "primary_task_id": "P0.T001",
                    "status": "running_background",
                    "next_prompt_ready": False,
                    "started_at": timestamp(timedelta(minutes=-2)),
                    "ended_at": timestamp(timedelta(minutes=-1)),
                    "background_jobs": [
                        {
                            "job_id": "bg_completed_from_status",
                            "task_id": "P0.T001",
                            "run_id": "run_bg_completed",
                            "status": "completed",
                        }
                    ],
                },
            )

            snapshot = load_scheduler_snapshot(project)
            background_job = snapshot["background_jobs"][0]
            action = select_next_action(snapshot)

            self.assertEqual(background_job["status"], "completed")
            self.assertTrue(background_job["next_prompt_ready"])
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P1.T001")

    def test_stale_synthetic_background_job_reconciles_completed_source_agent_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Completed source agent status should release stale synthetic background jobs.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": " "})
            workflow_id = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                "workflow_id"
            ]
            run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_bg_reconciled"
            run_dir.mkdir(parents=True, exist_ok=True)
            status_path = run_dir / "agent_status.json"
            write_json(
                status_path,
                {
                    "schema_version": "1.5",
                    "run_id": "run_bg_reconciled",
                    "task_id": "P0.T001",
                    "primary_task_id": "P0.T001",
                    "status": "completed_with_warnings",
                    "background_state": {
                        "started_background_work": False,
                        "next_prompt_ready": True,
                    },
                    "started_at": timestamp(timedelta(minutes=-20)),
                    "ended_at": timestamp(timedelta(minutes=-10)),
                },
            )
            registry_path = project / ".loopplane" / "runtime" / "background_jobs.json"
            write_json(
                registry_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": workflow_id,
                    "jobs": [
                        {
                            "job_id": "bg_P0_T001_run_bg_reconciled",
                            "task_id": "P0.T001",
                            "run_id": "run_bg_reconciled",
                            "status": "running",
                            "next_prompt_ready": False,
                            "heartbeat_at": timestamp(timedelta(minutes=-20)),
                            "source_agent_status_path": status_path.relative_to(project).as_posix(),
                        }
                    ],
                },
            )

            snapshot = load_scheduler_snapshot(project)
            action = select_next_action(snapshot)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            job = snapshot["background_jobs"][0]

            self.assertEqual(job["status"], "completed")
            self.assertTrue(job["next_prompt_ready"])
            self.assertTrue(job["resolved_from_source_agent_status"])
            self.assertEqual(registry["jobs"][0]["status"], "completed")
            self.assertTrue(registry["jobs"][0]["next_prompt_ready"])
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P1.T001")

    def test_registry_background_job_id_suppresses_duplicate_inferred_status_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Registry background job should stay authoritative.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": " "})
            (project / ".loopplane" / "runtime" / "background_jobs.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": "wf_test",
                        "jobs": [
                            {
                                "job_id": "bg_shared",
                                "status": "completed",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_bg_duplicate"
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                run_dir / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_bg_duplicate",
                    "task_id": "P0.T001",
                    "status": "running_background",
                    "started_at": timestamp(timedelta(minutes=-1)),
                    "background_jobs": [
                        {
                            "job_id": "bg_shared",
                            "status": "running",
                            "heartbeat_at": timestamp(),
                        }
                    ],
                },
            )

            snapshot = load_scheduler_snapshot(project)
            jobs = [job for job in snapshot["background_jobs"] if job["job_id"] == "bg_shared"]
            action = select_next_action(snapshot)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "completed")
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P1.T001")

    def test_background_state_active_background_jobs_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Background state active jobs should be authoritative.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": " "})
            run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_bg_state"
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                run_dir / "agent_status.json",
                {
                    "schema_version": "1.5",
                    "run_id": "run_bg_state",
                    "task_id": "P0.T001",
                    "status": "running_background",
                    "next_prompt_ready": False,
                    "started_at": timestamp(timedelta(minutes=-2)),
                    "background_state": {
                        "active_background_jobs": [
                            {
                                "job_id": "bg_state_real",
                                "task_id": "P0.T001",
                                "run_id": "run_bg_state",
                                "status": "completed",
                            }
                        ]
                    },
                },
            )

            snapshot = load_scheduler_snapshot(project)
            job_ids = [job["job_id"] for job in snapshot["background_jobs"]]
            action = select_next_action(snapshot)

            self.assertEqual(job_ids, ["bg_state_real"])
            self.assertEqual(snapshot["background_jobs"][0]["status"], "completed")
            self.assertTrue(snapshot["background_jobs"][0]["next_prompt_ready"])
            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P1.T001")

    def test_existing_registry_job_for_run_suppresses_synthetic_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Existing registry job should suppress synthetic background status job.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_background_registry_only.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                project = run_dir.parents[4]
                workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
                registry = {
                    "schema_version": "1.5",
                    "workflow_id": workflow["workflow_id"],
                    "jobs": [
                        {
                            "job_id": "bg_registry_real",
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "run_id": os.environ["LOOPPLANE_RUN_ID"],
                            "status": "completed",
                            "next_prompt_ready": True,
                        }
                    ],
                }
                (project / ".loopplane" / "runtime" / "background_jobs.json").write_text(
                    json.dumps(registry, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "status": "running_background",
                    "next_prompt_ready": False,
                    "summary_candidate": "Background command was registered separately.",
                    "evidence_satisfies": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)
            registry = json.loads((project / ".loopplane" / "runtime" / "background_jobs.json").read_text(encoding="utf-8"))
            job_ids = [job["job_id"] for job in registry["jobs"]]
            next_action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(result["exit_code"], EXIT_WAITING_BACKGROUND_JOB, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(job_ids, ["bg_registry_real"])
            self.assertEqual(registry["jobs"][0]["status"], "completed")
            self.assertTrue(registry["jobs"][0]["next_prompt_ready"])
            self.assertEqual(next_action["action"], "run_worker")
            self.assertEqual(next_action["selected"]["task_id"], "P0.T001")

    def test_waiting_config_is_selected_before_recovery_or_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Wait for config.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            state_path = project / ".loopplane" / "runtime" / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["configuration_problems"] = [{"code": "agent_runner_missing", "message": "Runner config invalid."}]
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "wait_config")
            self.assertEqual(action["selected"]["problem"]["code"], "agent_runner_missing")

    def test_malformed_active_plan_waits_config_without_selecting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed active plan fixture.")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            (project / "PLAN.md").write_text(
                textwrap.dedent(
                    f"""\
                    # Project Plan

                    ## Metadata

                    - workflow_id: {workflow["workflow_id"]}
                    - plan_version: 1
                    - generated_from: PROJECT_BRIEF.md
                    - active: true

                    ## Phase P0: Malformed

                    - [ ] P0.T001: Missing required fields
                    """
                ),
                encoding="utf-8",
            )

            result = run_scheduler(project)

            self.assertEqual(result["exit_code"], EXIT_PLAN_MALFORMED, json.dumps(result, indent=2, sort_keys=True))
            selected = result["selected_action"]
            self.assertEqual(selected["action"], "wait_config")
            self.assertEqual(selected["selected"]["problem"]["code"], "plan_malformed")
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001" / "runs").exists())

    def test_manual_plan_change_records_event_and_requires_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Manual plan change fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            accepted_sha = record_accepted_plan_hash(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8").replace("First task", "Manually edited first task"),
                encoding="utf-8",
            )

            result = run_scheduler(project)

            selected = result["selected_action"]
            self.assertEqual(selected["action"], "wait_config")
            self.assertEqual(selected["selected"]["problem"]["code"], "manual_plan_change_detected")
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            manual = state["manual_plan_change"]
            self.assertTrue(manual["reconciliation_required"])
            self.assertEqual(manual["accepted_plan_sha256"], accepted_sha)
            self.assertNotEqual(manual["current_plan_sha256"], accepted_sha)
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertEqual([event["event_type"] for event in events].count("manual_plan_change_detected"), 1)

            second = run_scheduler(project)

            self.assertEqual(second["selected_action"]["action"], "wait_config")
            events_after_second_tick = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertEqual(
                [event["event_type"] for event in events_after_second_tick].count("manual_plan_change_detected"),
                1,
            )
            status = subprocess.run(
                [sys.executable, str(LoopPlane), "status", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertTrue(
                any("acknowledge-plan" in step for step in json.loads(status.stdout).get("next_steps", []))
            )

            acknowledged = subprocess.run(
                [sys.executable, str(LoopPlane), "acknowledge-plan", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr + acknowledged.stdout)
            ack_payload = json.loads(acknowledged.stdout)
            self.assertEqual(ack_payload["status"], "acknowledged")
            acknowledged_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("manual_plan_change", acknowledged_state)
            self.assertEqual(acknowledged_state["active_plan_sha256"], ack_payload["active_plan_sha256"])
            self.assertFalse(
                any(
                    problem.get("code") == "manual_plan_change_detected"
                    for problem in acknowledged_state.get("configuration_problems", [])
                    if isinstance(problem, dict)
                )
            )
            planning_events_after_ack: list[dict[str, object]] = []
            for events_path in sorted((project / ".loopplane").glob("**/planning/planning_events.jsonl")):
                planning_events_after_ack.extend(read_jsonl(events_path))
            self.assertIn(
                "manual_plan_change_acknowledged",
                [event["event_type"] for event in planning_events_after_ack],
            )

    def test_active_run_lease_blocks_duplicate_worker_and_reports_liveness_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Active lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            write_json(
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_alive_stale.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "run_id": "run_alive_stale",
                    "node_id": "node_worker_P0_T001_run_alive_stale",
                    "task_id": "P0.T001",
                    "role": "worker",
                    "runner_id": "worker",
                    "status": "running",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                    "adapter_pid": os.getpid(),
                    "adapter_result_path": ".loopplane/runtime/runs/run_alive_stale/adapter_result.json",
                    "stdout_path": ".loopplane/runtime/runs/run_alive_stale/stdout.log",
                    "stderr_path": ".loopplane/runtime/runs/run_alive_stale/stderr.log",
                    "final_output_path": ".loopplane/runtime/runs/run_alive_stale/final.md",
                },
            )

            result = run_scheduler(project)

            self.assertEqual(result["exit_code"], EXIT_WAITING_BACKGROUND_JOB, json.dumps(result, indent=2, sort_keys=True))
            selected = result["selected_action"]
            self.assertEqual(selected["action"], "wait_background_job")
            self.assertIn("active_run_lease_not_ready", selected["blocking_conditions"])
            lease = selected["selected"]["job"]
            self.assertEqual(lease["run_id"], "run_alive_stale")
            self.assertEqual(lease["runner_liveness"], "alive")
            self.assertEqual(lease["status_problem"], "stale_heartbeat_process_alive")
            self.assertFalse(lease["fresh"])
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001" / "runs").exists())

    def test_active_run_lease_uses_child_pid_liveness_when_adapter_pid_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Active child lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            write_json(
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_child_alive.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "run_id": "run_child_alive",
                    "node_id": "node_worker_P0_T001_run_child_alive",
                    "task_id": "P0.T001",
                    "role": "worker",
                    "runner_id": "worker",
                    "status": "running",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                    "adapter_pid": 99999999,
                    "adapter_child_pid": os.getpid(),
                    "adapter_result_path": ".loopplane/runtime/runs/run_child_alive/adapter_result.json",
                    "stdout_path": ".loopplane/runtime/runs/run_child_alive/stdout.log",
                    "stderr_path": ".loopplane/runtime/runs/run_child_alive/stderr.log",
                    "final_output_path": ".loopplane/runtime/runs/run_child_alive/final.md",
                },
            )

            result = run_scheduler(project)

            self.assertEqual(result["exit_code"], EXIT_WAITING_BACKGROUND_JOB, json.dumps(result, indent=2, sort_keys=True))
            lease = result["selected_action"]["selected"]["job"]
            self.assertEqual(lease["run_id"], "run_child_alive")
            self.assertEqual(lease["runner_liveness"], "alive")
            self.assertEqual(lease["status_problem"], "stale_heartbeat_process_alive")
            processes = lease["process_liveness"]["processes"]
            self.assertIn(
                {"field": "adapter_child_pid", "pid": os.getpid(), "liveness": "alive"},
                processes,
            )
            self.assertIn(
                {"field": "adapter_pid", "pid": 99999999, "liveness": "dead"},
                processes,
            )

    def test_stale_dead_active_run_lease_is_reclaimed_and_does_not_block(self) -> None:
        # A lease whose runner process is *definitively* dead and whose heartbeat
        # has expired is crash debris (SIGKILL / OOM / host restart): the runner
        # cannot still be writing, so the scheduler auto-reclaims it (mirroring the
        # instance-lock's _reclaim_stale_owner) and proceeds with normal work,
        # instead of wedging the workflow in requires_attention forever.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Dead active lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            lease_path = (
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_dead_stale.json"
            )
            write_json(
                lease_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "run_id": "run_dead_stale",
                    "node_id": "node_worker_P0_T001_run_dead_stale",
                    "task_id": "P0.T001",
                    "role": "worker",
                    "runner_id": "worker",
                    "status": "running",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                    "adapter_pid": 99999999,
                    "scheduler_pid": os.getpid(),
                    "adapter_result_path": ".loopplane/runtime/runs/run_dead_stale/adapter_result.json",
                    "stdout_path": ".loopplane/runtime/runs/run_dead_stale/stdout.log",
                    "stderr_path": ".loopplane/runtime/runs/run_dead_stale/stderr.log",
                    "final_output_path": ".loopplane/runtime/runs/run_dead_stale/final.md",
                },
            )

            result = run_scheduler(project)

            # The dead-stale lease must NOT force requires_attention; the scheduler
            # reclaims it and advances to dispatch the first executable task.
            selected = result["selected_action"]
            self.assertNotEqual(selected["action"], "requires_attention", json.dumps(result, indent=2, sort_keys=True))
            self.assertNotIn(
                "active_run_lease_stale_dead",
                selected.get("blocking_conditions", []),
            )
            # The lease file is persisted as a released (inactive) terminal status,
            # so the reclaim survives across ticks and is auditable.
            released = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertEqual(released["status"], "released")
            self.assertEqual(
                released["status_problem"], "stale_heartbeat_process_dead_reclaimed"
            )
            self.assertEqual(
                released["released_reason"], "runner_process_dead_and_heartbeat_stale"
            )
            self.assertTrue(released.get("released_at"))
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(state["status"], "requires_attention")

    def test_stale_active_run_lease_with_unknown_liveness_is_not_reclaimed(self) -> None:
        # Conservative boundary: when runner liveness cannot be determined
        # ("unavailable"/unknown, e.g. a PID we cannot probe) we must NOT reclaim,
        # to avoid falsely releasing a lease whose runner may still be alive. The
        # lease stays intact and the scheduler keeps waiting on it rather than
        # advancing past it or escalating to requires_attention.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Unknown-liveness active lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            lease_path = (
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_unknown_stale.json"
            )
            write_json(
                lease_path,
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "run_id": "run_unknown_stale",
                    "node_id": "node_worker_P0_T001_run_unknown_stale",
                    "task_id": "P0.T001",
                    "role": "worker",
                    "runner_id": "worker",
                    "status": "running",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                    "adapter_pid": 99999999,
                    "scheduler_pid": os.getpid(),
                    "adapter_result_path": ".loopplane/runtime/runs/run_unknown_stale/adapter_result.json",
                    "stdout_path": ".loopplane/runtime/runs/run_unknown_stale/stdout.log",
                    "stderr_path": ".loopplane/runtime/runs/run_unknown_stale/stderr.log",
                    "final_output_path": ".loopplane/runtime/runs/run_unknown_stale/final.md",
                },
            )

            # Force every PID probe to be inconclusive -> aggregate liveness is
            # "unavailable" (never "dead"), so the reclaim must not trigger.
            with patch("runtime.scheduler._pid_exists", return_value=None):
                result = run_scheduler(project)

            selected = result["selected_action"]
            self.assertNotEqual(
                selected["action"], "requires_attention", json.dumps(result, indent=2, sort_keys=True)
            )
            # The lease is left intact (not reclaimed) and still blocks as a wait.
            untouched = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertNotEqual(untouched.get("status"), "released")
            self.assertEqual(selected["action"], "wait_background_job")
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001" / "runs").exists())

    def test_malformed_active_run_lease_requires_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed active lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            lease_path = project / ".loopplane" / "runtime" / "active_run_leases" / "run_malformed.json"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text("{not json", encoding="utf-8")

            result = run_scheduler(project)

            self.assertEqual(result["exit_code"], EXIT_GENERIC_FAILURE, json.dumps(result, indent=2, sort_keys=True))
            selected = result["selected_action"]
            self.assertEqual(selected["action"], "requires_attention")
            self.assertIn("active_run_lease_malformed", selected["blocking_conditions"])
            lease = selected["selected"]["job"]
            self.assertEqual(lease["status"], "needs_recovery")
            self.assertEqual(lease["status_problem"], "malformed_lease")
            self.assertEqual(lease["path"], ".loopplane/runtime/active_run_leases/run_malformed.json")
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "requires_attention")
            self.assertEqual(state["requires_attention"][0]["type"], "active_run_lease_needs_recovery")
            self.assertFalse((project / ".loopplane" / "results" / "P0.T001" / "runs").exists())

    def test_inspector_active_run_lease_does_not_block_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector lease fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            write_json(
                project / ".loopplane" / "runtime" / "active_run_leases" / "run_inspector.json",
                {
                    "schema_version": "1.5",
                    "workflow_id": "wf_test",
                    "run_id": "run_inspector",
                    "node_id": "node_inspector_run_inspector",
                    "task_id": None,
                    "role": "inspector",
                    "runner_id": "inspector",
                    "status": "running",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "lease_expires_at": "2000-01-01T00:00:01Z",
                    "adapter_pid": os.getpid(),
                },
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_worker")
            self.assertFalse(action["would_wait"])
            self.assertEqual(action["selected"]["task_id"], "P0.T001")

    def test_recovery_is_selected_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Recover before new work.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            registry = {
                "failures": [
                    {
                        "failure_id": "fail1",
                        "task_id": "P0.T001",
                        "status": "unrecovered",
                        "recoverable": True,
                        "recovery_attempts": 0,
                        "max_recovery_attempts": 2,
                        "first_seen_at": "2026-06-10T00:00:00Z",
                    }
                ]
            }
            (project / ".loopplane" / "runtime" / "failure_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_recovery")
            self.assertEqual(action["selected"]["role"], "recovery_worker")
            self.assertEqual(action["selected"]["runner_role"], "worker")
            self.assertEqual(action["selected"]["failure_id"], "fail1")
            self.assertEqual(action["selected"]["task_id"], "P0.T001")

    def test_worker_selection_uses_first_executable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Select worker.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_worker")
            self.assertEqual(action["selected"]["task_id"], "P0.T001")
            self.assertEqual(action["selected"]["runner_id"], "worker")

    def test_final_verification_candidate_when_no_task_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Run final verification.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": "x"})

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_final_verification")
            self.assertEqual(action["selected"]["run_kind"], "final_verification")

    def test_fresh_completion_marker_selects_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Completion marker fixture.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": "x"})
            final_report = project / ".loopplane" / "runtime" / "final_verification_report.json"
            final_report.write_text(json.dumps({"pass": True}, sort_keys=True) + "\n", encoding="utf-8")
            append_jsonl(
                project / ".loopplane" / "runtime" / "git_checkpoints.jsonl",
                {"checkpoint_id": "gitcp_test", "status": "created"},
            )
            marker_path = project / ".loopplane" / "runtime" / "plan_loop_complete.json"
            marker = {
                "schema_version": "1.5",
                "workflow_id": json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                    "workflow_id"
                ],
                "completed_at": "2026-06-10T00:00:00Z",
                "status": "completed",
                "final_verification_report": ".loopplane/runtime/final_verification_report.json",
            }
            marker["plan_sha256"] = "sha256:" + sha256((project / "PLAN.md").read_bytes()).hexdigest()
            marker["evidence_manifest_sha256"] = (
                "sha256:" + sha256((project / ".loopplane" / "runtime" / "evidence_manifest.json").read_bytes()).hexdigest()
            )
            marker["event_log_head"] = None
            marker["final_verification_report_sha256"] = "sha256:" + sha256(final_report.read_bytes()).hexdigest()
            marker["final_git_checkpoint_id"] = "gitcp_test"
            state_values = {
                "event_log_head": marker["event_log_head"],
                "evidence_manifest_sha256": marker["evidence_manifest_sha256"],
                "final_git_checkpoint_id": marker["final_git_checkpoint_id"],
                "final_verification_report_sha256": marker["final_verification_report_sha256"],
                "plan_sha256": marker["plan_sha256"],
            }
            marker["state_fingerprint"] = "sha256:" + sha256(
                json.dumps(state_values, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "complete")
            self.assertTrue(action["would_wait"])


class SchedulerPreviewTest(unittest.TestCase):
    def test_preview_uses_scheduler_selection_without_mutating_authoritative_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Preview should be mutation-free.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            result_dir = project / ".loopplane" / "results" / "P0.T001"
            result_dir.mkdir(parents=True)
            (result_dir / "latest.json").write_text(json.dumps({"latest_run_id": "run_prev"}) + "\n", encoding="utf-8")
            (result_dir / "validation.json").write_text(json.dumps({"status": "historical"}) + "\n", encoding="utf-8")
            append_jsonl(
                project / ".loopplane" / "runtime" / "git_checkpoints.jsonl",
                {"checkpoint_id": "gitcp_existing", "status": "created"},
            )
            (project / ".loopplane" / "runtime" / "plan_loop_complete.json").write_text(
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
            before = authoritative_file_hashes(project)

            result = preview_scheduler(project)

            after = authoritative_file_hashes(project)
            self.assertEqual(after, before)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["mode"], "dry_run")
            self.assertFalse(result["would_mutate_state"])
            self.assertEqual(result["next_action"], "run_worker")
            self.assertEqual(result["selected"]["task_id"], "P0.T001")
            self.assertEqual(result["selected"]["runner_id"], "worker")
            self.assertEqual(result["selected"]["expected_prompt_path"], ".loopplane/runtime/runs/<run_id>/prompt.md")
            self.assertFalse(result["completion_marker"]["fresh"])
            self.assertIn("plan_sha256_mismatch", result["completion_marker"]["stale_reasons"])
            self.assertIn("control_request", {item["candidate"] for item in result["skipped_candidates"]})

    def test_preview_cli_json_and_run_dry_run_are_machine_checkable_and_mutation_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Preview CLI smoke.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            before = authoritative_file_hashes(project)

            preview = subprocess.run(
                [sys.executable, str(LoopPlane), "preview", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            dry_run = subprocess.run(
                [sys.executable, str(LoopPlane), "run", "--project", str(project), "--dry-run"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            after = authoritative_file_hashes(project)
            self.assertEqual(after, before)
            self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            preview_payload = json.loads(preview.stdout)
            dry_run_payload = json.loads(dry_run.stdout)
            self.assertEqual(preview_payload["next_action"], "run_worker")
            self.assertEqual(dry_run_payload["next_action"], "run_worker")
            self.assertEqual(dry_run_payload["mode"], "dry_run")
            self.assertFalse(dry_run_payload["would_mutate_state"])


class PrepareRunTest(unittest.TestCase):
    def test_prepare_run_allocates_worker_paths_and_active_lease_before_prompt_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Prepare a worker run.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})

            run = prepare_run(
                project,
                role="worker",
                task_id="P0.T001",
                runner_id="worker",
                scheduler_owner="test-scheduler",
            )

            self.assertTrue(run.run_id.startswith("run_"))
            self.assertEqual(run.role, "worker")
            self.assertEqual(run.task_id, "P0.T001")
            self.assertEqual(run.runner_id, "worker")
            self.assertIn("worker", run.node_id)
            self.assertIn("P0_T001", run.node_id)
            self.assertIn(run.run_id, run.node_id)

            self.assertEqual(run.scheduler_run_dir, project / ".loopplane" / "runtime" / "runs" / run.run_id)
            self.assertEqual(run.role_output_dir, project / ".loopplane" / "results" / "P0.T001" / "runs" / run.run_id)
            self.assertEqual(run.task_evidence_run_dir, run.role_output_dir)
            self.assertEqual(run.prompt_path, run.scheduler_run_dir / "prompt.md")
            self.assertEqual(run.stdout_path, run.scheduler_run_dir / "stdout.log")
            self.assertEqual(run.stderr_path, run.scheduler_run_dir / "stderr.log")
            self.assertEqual(run.final_output_path, run.scheduler_run_dir / "final.md")
            self.assertEqual(run.adapter_result_path, run.scheduler_run_dir / "adapter_result.json")
            self.assertEqual(
                run.active_run_lease_path,
                project / ".loopplane" / "runtime" / "active_run_leases" / f"{run.run_id}.json",
            )

            self.assertTrue(run.scheduler_run_dir.is_dir())
            self.assertTrue(run.role_output_dir.is_dir())
            self.assertTrue((run.role_output_dir / "logs").is_dir())
            self.assertTrue((run.role_output_dir / "artifacts").is_dir())
            self.assertTrue((run.role_output_dir / "raw").is_dir())
            self.assertTrue((run.scheduler_run_dir / "run_metadata.json").is_file())
            self.assertTrue((run.role_output_dir / "metadata.json").is_file())
            self.assertTrue((run.scheduler_run_dir / "task_id.txt").is_file())
            self.assertFalse(run.prompt_path.exists())
            self.assertFalse(run.stdout_path.exists())
            self.assertFalse(run.stderr_path.exists())
            self.assertFalse(run.final_output_path.exists())
            self.assertFalse(run.adapter_result_path.exists())

            lease = json.loads(run.active_run_lease_path.read_text(encoding="utf-8"))
            self.assertEqual(lease["schema_version"], "1.5")
            self.assertEqual(lease["run_id"], run.run_id)
            self.assertEqual(lease["workflow_id"], run.workflow_id)
            self.assertEqual(lease["task_id"], "P0.T001")
            self.assertEqual(lease["role"], "worker")
            self.assertEqual(lease["runner_id"], "worker")
            self.assertEqual(lease["node_id"], run.node_id)
            self.assertEqual(lease["status"], "starting")
            self.assertEqual(lease["scheduler_owner"], "test-scheduler")
            self.assertTrue(lease["heartbeat_at"])
            self.assertTrue(lease["lease_expires_at"])
            self.assertEqual(lease["prompt_path"], ".loopplane/runtime/runs/" + run.run_id + "/prompt.md")
            self.assertEqual(lease["adapter_result_path"], ".loopplane/runtime/runs/" + run.run_id + "/adapter_result.json")

            metadata = json.loads((run.scheduler_run_dir / "run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["run_id"], run.run_id)
            self.assertEqual(metadata["node_id"], run.node_id)
            self.assertEqual(metadata["active_run_lease_path"], ".loopplane/runtime/active_run_leases/" + run.run_id + ".json")

    def test_prompt_build_failure_marks_prepared_active_lease_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Prompt build failure closes active lease.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            record_accepted_plan_hash(project)
            worker_script = write_worker_script(
                project,
                "unused_worker.py",
                "import sys\nsys.exit(0)\n",
            )
            configure_shell_worker(project, worker_script)

            with patch(
                "runtime.scheduler.build_prompt_for_prepared_run",
                side_effect=PromptBuildError("synthetic prompt failure"),
            ):
                result = run_scheduler(project)

            self.assertEqual(result["exit_code"], EXIT_GENERIC_FAILURE, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["selected_action"]["action"], "run_worker")
            execution = result["selected_action"]["execution_result"]
            self.assertEqual(execution["status"], "failed_system")
            self.assertEqual(execution["classification"], "worker_prepare_or_prompt_failed")
            leases = sorted((project / ".loopplane" / "runtime" / "active_run_leases").glob("*.json"))
            self.assertEqual(len(leases), 1)
            lease = json.loads(leases[0].read_text(encoding="utf-8"))
            self.assertEqual(lease["status"], "failed")
            self.assertEqual(lease["role"], "worker")
            self.assertEqual(load_scheduler_snapshot(project)["active_run_leases"], [])


class WorkerRunExecutionTest(unittest.TestCase):
    def test_loopplane_run_cli_executes_worker_fixture_without_noop_or_waiting_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            init_project(project, "Run a worker task through the Codex CLI adapter.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            record_accepted_plan_hash(project)
            fixture_bin = install_cli_adapter_fixture_bin(root)
            env = dict(os.environ)
            env["PATH"] = fixture_bin.as_posix() + os.pathsep + env.get("PATH", "")

            configured = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "configure-agent",
                    "--project",
                    str(project),
                    "--role",
                    "worker",
                    "--adapter",
                    "codex_cli",
                    "--command",
                    "codex",
                    "--json",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "run", "--project", str(project), "--json"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["selected_action"]["action"], "run_worker")
            execution = payload["selected_action"]["execution_result"]
            self.assertEqual(execution["adapter"], "codex_cli")
            self.assertEqual(execution["runner_id"], "worker")
            self.assertEqual(execution["task_id"], "P0.T001")
            self.assertEqual(execution["status"], "completed")
            self.assertEqual(execution["classification"], "worker_agent_status")
            self.assertNotEqual(execution["status"], "waiting_config")

            run_id = execution["run_id"]
            scheduler_run_dir = project / ".loopplane" / "runtime" / "runs" / run_id
            evidence_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / run_id
            adapter_input = json.loads((scheduler_run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            adapter_result = json.loads((scheduler_run_dir / "adapter_result.json").read_text(encoding="utf-8"))
            agent_status = json.loads((evidence_run_dir / "agent_status.json").read_text(encoding="utf-8"))
            record = json.loads((evidence_run_dir / "codex_fixture_record.json").read_text(encoding="utf-8"))

            self.assertEqual(adapter_input["adapter"], "codex_cli")
            self.assertEqual(adapter_input["command"], "codex")
            self.assertEqual(adapter_input["role"], "worker")
            self.assertEqual(adapter_input["runner_id"], "worker")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_ROLE"], "worker")
            self.assertIn("First task", adapter_input["prompt_content"])
            self.assertEqual(adapter_result["adapter"], "codex_cli")
            self.assertEqual(adapter_result["command"], "codex")
            self.assertEqual(adapter_result["exit_code"], 0)
            self.assertFalse(adapter_result["timed_out"])
            self.assertTrue(adapter_result["adapter_metadata"]["external_execution"])
            self.assertEqual(adapter_result["adapter_metadata"]["delivery_mode"], "file_argument")
            self.assertEqual(record["executable"], (fixture_bin / "codex").resolve().as_posix())
            self.assertEqual(record["prompt_source"], "stdin")
            self.assertEqual(record["env"]["LOOPPLANE_ROLE"], "worker")
            self.assertEqual(record["env"]["LOOPPLANE_TASK_ID"], "P0.T001")
            self.assertEqual(record["env"]["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"], evidence_run_dir.as_posix())
            self.assertEqual(agent_status["status"], "completed")
            self.assertEqual(agent_status["task_id"], "P0.T001")
            self.assertIn("Fake Codex worker completed", (evidence_run_dir / "report.md").read_text(encoding="utf-8"))
            self.assertIn(
                (evidence_run_dir / "agent_status.json").as_posix(),
                set(adapter_result["produced_files"]),
            )
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertIn("worker_adapter_started", event_types)
            self.assertNotIn("worker_adapter_completed", event_types)
            self.assertIn("worker_run_classified", event_types)

    def test_usage_limited_worker_sets_availability_hold_and_next_run_waits_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            init_project(project, "Avoid retry storms when the runner usage limit is exhausted.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            record_accepted_plan_hash(project)
            configure_codex_usage_limit_worker(project)
            fixture_bin = install_cli_adapter_fixture_bin(root)
            env = dict(os.environ)
            env["PATH"] = fixture_bin.as_posix() + os.pathsep + env.get("PATH", "")

            first = subprocess.run(
                [sys.executable, str(LoopPlane), "run", "--project", str(project), "--json"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(first.returncode, EXIT_RUNNER_UNAVAILABLE, first.stdout + first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertEqual(first_payload["selected_action"]["action"], "run_worker")
            first_execution = first_payload["selected_action"]["execution_result"]
            self.assertFalse(first_execution["ok"], json.dumps(first_payload, indent=2, sort_keys=True))
            self.assertEqual(first_execution["runner_id"], "worker")
            self.assertEqual(first_execution["adapter"], "codex_cli")
            self.assertEqual(first_execution["next_step"], "runner_availability_wait")
            self.assertEqual(first_execution["runner_health_update"]["scope"], "runner_unavailable")
            self.assertEqual(first_execution["failure_registry_update"]["status"], "skipped")
            availability = first_execution["runner_availability"]
            self.assertEqual(availability["reason_class"], "usage_limit_exhausted")
            self.assertEqual(availability["recoverability"], "auto_after_cooldown")

            failure_registry_path = project / ".loopplane" / "runtime" / "failure_registry.json"
            if failure_registry_path.exists():
                registry = json.loads(failure_registry_path.read_text(encoding="utf-8"))
                self.assertEqual(registry.get("failures", []), [])

            health_path = project / ".loopplane" / "runtime" / "runner_health.json"
            health = json.loads(health_path.read_text(encoding="utf-8"))
            hold = health["runners"]["worker"]["availability_hold"]
            self.assertEqual(hold["status"], "active")
            self.assertEqual(hold["reason_class"], "usage_limit_exhausted")
            self.assertEqual(hold["scope"], {"type": "runner", "key": "worker"})
            self.assertEqual(hold["seen_count"], 1)
            run_dirs_before = sorted(path.name for path in (project / ".loopplane" / "runtime" / "runs").iterdir() if path.is_dir())

            second = subprocess.run(
                [sys.executable, str(LoopPlane), "run", "--project", str(project), "--json"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(second.returncode, EXIT_RUNNER_UNAVAILABLE, second.stdout + second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertEqual(second_payload["selected_action"]["action"], "wait_runner_availability")
            self.assertEqual(second_payload["status"], "ok")
            self.assertEqual(second_payload["stopped_reason"], "wait_runner_availability")
            self.assertNotIn("execution_result", second_payload["selected_action"])
            run_dirs_after = sorted(path.name for path in (project / ".loopplane" / "runtime" / "runs").iterdir() if path.is_dir())
            self.assertEqual(run_dirs_after, run_dirs_before)

            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertEqual(event_types.count("runner_availability_hold_started"), 1)
            self.assertIn("scheduler_wait_tick", event_types)

    def test_loopplane_run_cli_recovers_codex_cli_failure_with_claude_code_cli_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            init_project(project, "Recover a Codex CLI failure with Claude Code CLI.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "}, max_attempts=5)
            record_accepted_plan_hash(project)
            configure_codex_failure_to_claude_recovery(project)
            fixture_bin = install_cli_adapter_fixture_bin(root)
            env = dict(os.environ)
            env["PATH"] = fixture_bin.as_posix() + os.pathsep + env.get("PATH", "")

            registry_path = project / ".loopplane" / "runtime" / "failure_registry.json"
            failure_id = None
            for attempt_index in range(4):
                failed = subprocess.run(
                    [sys.executable, str(LoopPlane), "run", "--project", str(project), "--json"],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
                failed_payload = json.loads(failed.stdout)
                failed_execution = failed_payload["selected_action"]["execution_result"]
                expected_action = "run_worker" if attempt_index == 0 else "run_recovery"
                self.assertEqual(failed_payload["selected_action"]["action"], expected_action)
                self.assertFalse(failed_execution["ok"], json.dumps(failed_payload, indent=2, sort_keys=True))
                self.assertEqual(failed_execution["runner_id"], "worker")
                self.assertEqual(failed_execution["adapter"], "codex_cli")
                self.assertEqual(failed_execution["status"], "failed_agent")
                self.assertEqual(failed_execution["failure_scope"], "runner")
                self.assertEqual(failed_execution["runner_health_update"]["scope"], "runner_failure")
                self.assertEqual(failed_execution["adapter_exit_code"], 17)
                self.assertIn("CODEX FAILURE requested", failed_execution["stderr_excerpt"])
                if attempt_index == 0:
                    failure_id = failed_execution["failure_id"]
                else:
                    self.assertEqual(failed_execution["failure_id"], failure_id)

            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(len(registry["failures"]), 1)
            failure = registry["failures"][0]
            self.assertEqual(failure["status"], "unrecovered")
            self.assertEqual(failure["task_id"], "P0.T001")
            self.assertTrue(failure["budget_remaining"])
            self.assertEqual(failure["recovery_attempts"], 3)

            health_path = project / ".loopplane" / "runtime" / "runner_health.json"
            health = json.loads(health_path.read_text(encoding="utf-8"))
            codex_events = health["runners"]["worker"]["events"]
            self.assertEqual([event["scope"] for event in codex_events], ["runner_failure"] * 4)

            recovered = subprocess.run(
                [sys.executable, str(LoopPlane), "run", "--project", str(project), "--json"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            recovered_payload = json.loads(recovered.stdout)
            self.assertEqual(recovered_payload["selected_action"]["action"], "run_recovery")
            recovered_execution = recovered_payload["selected_action"]["execution_result"]
            self.assertTrue(recovered_execution["ok"], json.dumps(recovered_payload, indent=2, sort_keys=True))
            self.assertEqual(recovered_execution["runner_id"], "worker_fallback")
            self.assertEqual(recovered_execution["adapter"], "claude_code_cli")
            self.assertEqual(recovered_execution["role"], "recovery_worker")
            self.assertEqual(recovered_execution["failure_scope"], "success")
            self.assertEqual(recovered_execution["runner_health_update"]["scope"], "success")
            self.assertEqual(recovered_execution["status"], "completed")
            self.assertEqual(recovered_execution["failure_id"], failure_id)

            run_id = recovered_execution["run_id"]
            scheduler_run_dir = project / ".loopplane" / "runtime" / "runs" / run_id
            evidence_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / run_id
            adapter_input = json.loads((scheduler_run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            adapter_result = json.loads((scheduler_run_dir / "adapter_result.json").read_text(encoding="utf-8"))
            claude_record = json.loads((evidence_run_dir / "claude_fixture_record.json").read_text(encoding="utf-8"))

            self.assertEqual(adapter_input["adapter"], "claude_code_cli")
            self.assertEqual(adapter_input["runner_id"], "worker_fallback")
            self.assertEqual(adapter_input["role"], "recovery_worker")
            self.assertEqual(adapter_result["adapter"], "claude_code_cli")
            self.assertEqual(adapter_result["exit_code"], 0)
            self.assertEqual(claude_record["fixture"], "claude")
            self.assertEqual(claude_record["env"]["LOOPPLANE_ROLE"], "recovery_worker")
            self.assertIn("Failure Summary", claude_record["prompt"])
            self.assertIn("Fake Claude worker completed", (evidence_run_dir / "report.md").read_text(encoding="utf-8"))

            updated = json.loads(registry_path.read_text(encoding="utf-8"))["failures"][0]
            self.assertEqual(updated["status"], "recovered")
            self.assertEqual(updated["recovery_attempts"], 4)
            self.assertEqual(updated["recovery_run_ids"][-1], run_id)
            self.assertEqual(len(updated["recovery_run_ids"]), 4)
            health = json.loads(health_path.read_text(encoding="utf-8"))
            self.assertEqual(health["runners"]["worker_fallback"]["events"][-1]["scope"], "success")
            self.assertIn("- [x] P0.T001: First task", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_loopplane_run_cli_emits_active_run_progress_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Run progress heartbeat fixture.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            record_accepted_plan_hash(project)
            script = write_worker_script(
                project,
                "worker_progress.py",
                """
                import json
                import os
                import time
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                report = run_dir / "report.md"
                report.write_text("# Worker Report\\n\\nProgress fixture completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_progress.py\\n", encoding="utf-8")
                Path(os.environ["LOOPPLANE_STDOUT_LOG"]).write_text("worker progress evidence visible\\n", encoding="utf-8")
                time.sleep(0.8)
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_progress.py", "exit_code": 0}],
                    "key_outputs": [str(report)],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["Worker fixture produced evidence."],
                            "evidence": [str(report)],
                        }
                    ],
                    "validation_claim": {
                        "claim": "completed",
                        "checks_claimed": [{"name": "fixture", "status": "pass"}],
                        "limitations": [],
                    },
                    "summary_candidate": {
                        "one_line": "Progress fixture completed.",
                        "highlights": ["report.md written"],
                        "warnings": [],
                        "blockers": [],
                    },
                    "background": {
                        "pids": [],
                        "commands": [],
                        "logs": [],
                        "heartbeat_required": False,
                        "wake_next_agent_when": None,
                    },
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, script)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "run",
                    "--project",
                    str(project),
                    "--json",
                    "--progress-interval",
                    "0.1",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertIn("[loopplane run] active", completed.stderr)
            self.assertIn("run_id=run_", completed.stderr)
            self.assertIn("scheduler_pid=", completed.stderr)
            self.assertRegex(completed.stderr, r"adapter_pid=\d+")
            self.assertIn("heartbeat=", completed.stderr)
            self.assertIn("new_files=", completed.stderr)
            self.assertIn("report.md", completed.stderr)
            self.assertIn("adapter_phase=awaiting_adapter_exit", completed.stderr)
            self.assertIn("stdout_tail=worker progress evidence visible", completed.stderr)

    def test_scheduler_runs_shell_worker_and_records_outputs_logs_and_lease_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Execute worker run.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_success.py",
                """
                import json
                import os
                import time
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Worker Report\\n\\nShell worker completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_success.py\\n", encoding="utf-8")
                time.sleep(0.25)
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [
                        {
                            "cmd": "python worker_success.py",
                            "exit_code": 0,
                            "log": str(run_dir / "logs" / "stdout.log"),
                        }
                    ],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["Worker fixture produced evidence."],
                            "evidence": [str(run_dir / "report.md")],
                        }
                    ],
                    "validation_claim": {
                        "claim": "completed",
                        "checks_claimed": [{"name": "fixture", "status": "pass"}],
                        "limitations": [],
                    },
                    "summary_candidate": {
                        "one_line": "Shell worker fixture completed.",
                        "highlights": ["report.md written"],
                        "warnings": [],
                        "blockers": [],
                    },
                    "background": {
                        "pids": [],
                        "commands": [],
                        "logs": [],
                        "heartbeat_required": False,
                        "wake_next_agent_when": None,
                    },
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                print("worker stdout marker")
                print("worker stderr marker", file=__import__("sys").stderr)
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["status"], "completed")
            self.assertEqual(execution["classification"], "worker_agent_status")
            run_id = execution["run_id"]
            scheduler_run_dir = project / ".loopplane" / "runtime" / "runs" / run_id
            evidence_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / run_id

            self.assertTrue((scheduler_run_dir / "adapter_input.json").is_file())
            self.assertTrue((scheduler_run_dir / "adapter_result.json").is_file())
            self.assertTrue((scheduler_run_dir / "run_execution.json").is_file())
            self.assertTrue((scheduler_run_dir / "stdout.log").read_text(encoding="utf-8").strip())
            adapter_input = json.loads((scheduler_run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            self.assertTrue(adapter_input["prompt_path"].endswith(".loopplane/runtime/runs/" + run_id + "/prompt.md"))
            self.assertIn("First task", adapter_input["prompt_content"])
            self.assertEqual(adapter_input["env"]["LOOPPLANE_PLAN_FILE_REL"], "PLAN.md")
            self.assertEqual(adapter_input["env"]["LOOPPLANE_TASK_EVIDENCE_RUN_DIR_REL"], f".loopplane/results/P0.T001/runs/{run_id}")
            adapter_result = json.loads((scheduler_run_dir / "adapter_result.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_result["exit_code"], 0)
            self.assertFalse(adapter_result["timed_out"])
            self.assertLessEqual(
                {
                    (scheduler_run_dir / "adapter_input.json").as_posix(),
                    (scheduler_run_dir / "adapter_result.json").as_posix(),
                    (scheduler_run_dir / "stdout.log").as_posix(),
                    (scheduler_run_dir / "stderr.log").as_posix(),
                    (scheduler_run_dir / "final.md").as_posix(),
                    (evidence_run_dir / "agent_status.json").as_posix(),
                    (evidence_run_dir / "commands.sh").as_posix(),
                    (evidence_run_dir / "report.md").as_posix(),
                },
                set(adapter_result["produced_files"]),
            )
            self.assertIn("worker stdout marker", (evidence_run_dir / "logs" / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("worker stderr marker", (evidence_run_dir / "logs" / "stderr.log").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((evidence_run_dir / "agent_status.json").read_text(encoding="utf-8"))["status"], "completed")
            self.assertEqual(json.loads((evidence_run_dir / "node_summary.json").read_text(encoding="utf-8"))["status"], "completed")
            lease = json.loads((project / ".loopplane" / "runtime" / "active_run_leases" / f"{run_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(lease["status"], "completed")
            self.assertGreaterEqual(lease["heartbeat_count"], 2)
            self.assertEqual(lease["adapter_pid"], os.getpid())
            self.assertIsInstance(lease["adapter_child_pid"], int)
            self.assertGreater(lease["adapter_child_pid"], 0)

            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] P0.T001: First task", plan_text)
            self.assertEqual(execution["next_step"], "reconciled")
            self.assertEqual(execution["auto_validation"]["status"], "pass")
            self.assertEqual(execution["auto_reconciliation"]["status"], "reconciled")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertIn("worker_adapter_started", event_types)
            self.assertNotIn("worker_adapter_completed", event_types)
            self.assertIn("worker_run_classified", event_types)
            self.assertEqual(execution["git_metadata"]["pre"]["status"], "skipped")
            self.assertEqual(execution["git_metadata"]["post"]["status"], "skipped")
            self.assertEqual(execution["git_metadata"]["pre"]["metadata"]["reason"], "run_metadata_disabled")
            self.assertEqual(execution["git_metadata"]["post"]["metadata"]["reason"], "run_metadata_disabled")
            self.assertEqual(
                execution["git_metadata"]["pre"]["metadata"]["policy_checkpoint"]["reason"],
                "checkpoint_policy_disabled",
            )
            self.assertEqual(execution["auto_validation_checkpoint"]["checkpoint"]["reason"], "after_validation_pass")
            git_dir = evidence_run_dir / "git"
            self.assertFalse(git_dir.exists())
            checkpoint_log = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
            checkpoint_records = read_jsonl(checkpoint_log) if checkpoint_log.exists() else []
            reasons = [record.get("reason") for record in checkpoint_records]
            self.assertNotIn("before_worker_run", reasons)
            self.assertIn("after_validation_pass", reasons)

    def test_scheduler_honors_worker_checkpoint_policy_with_status_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Execute worker run with checkpoints.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            version_config_path = project / ".loopplane" / "config" / "version_control.json"
            version_config = json.loads(version_config_path.read_text(encoding="utf-8"))
            version_config["run_metadata"]["enabled"] = True
            version_config["run_metadata"]["detail_level"] = "status"
            version_config["checkpoint_policy"]["before_worker_run"] = True
            version_config_path.write_text(json.dumps(version_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            init = subprocess.run(["git", "init", "-q", str(project)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            for key, value in (("user.name", "LoopPlane Test"), ("user.email", "loopplane-test@example.invalid")):
                configured = subprocess.run(
                    ["git", "-C", str(project), "config", key, value],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(configured.returncode, 0, configured.stderr + configured.stdout)
            script = write_worker_script(
                project,
                "worker_checkpoint.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Worker Report\\n\\nCheckpoint worker completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_checkpoint.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_checkpoint.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["Worker fixture produced evidence."],
                            "evidence": [str(run_dir / "report.md")],
                        }
                    ],
                    "validation_claim": {"claim": "completed", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Checkpoint worker completed.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertIn("checkpoint", execution["git_metadata"]["pre"]["metadata"])
            self.assertEqual(execution["git_metadata"]["pre"]["metadata"]["checkpoint"]["reason"], "before_worker_run")
            self.assertEqual(execution["auto_validation_checkpoint"]["checkpoint"]["reason"], "after_validation_pass")
            checkpoint_records = read_jsonl(project / ".loopplane" / "runtime" / "git_checkpoints.jsonl")
            reasons = [record.get("reason") for record in checkpoint_records]
            self.assertIn("before_worker_run", reasons)
            self.assertIn("after_validation_pass", reasons)

    def test_scheduler_treats_satisfied_agent_status_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Execute worker run with satisfied status.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_satisfied.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Worker Report\\n\\nSatisfied worker completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_satisfied.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "satisfied",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_satisfied.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["First task acceptance."],
                            "evidence": [str(run_dir / "report.md")],
                        }
                    ],
                    "validation_claim": {"claim": "satisfied", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Satisfied.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["status"], "satisfied")
            self.assertEqual(execution["next_step"], "reconciled")

    def test_scheduler_normalizes_complete_agent_status_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Execute worker run with complete alias.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_complete_alias.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Worker Report\\n\\nComplete alias worker finished.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_complete_alias.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "complete",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_complete_alias.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["First task acceptance."],
                            "evidence": [str(run_dir / "report.md")],
                        }
                    ],
                    "validation_claim": {"claim": "completed", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Complete alias.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["status"], "completed")
            self.assertEqual(execution["next_step"], "reconciled")
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["failures"], [])

    def test_scheduler_accepts_compatible_agent_status_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Execute worker run with old compatible status schema.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_old_schema.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Worker Report\\n\\nOld schema worker completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_old_schema.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.0",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_old_schema.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [
                        {
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "relationship": "primary",
                            "acceptance_claimed": ["First task acceptance."],
                            "evidence": [str(run_dir / "report.md")],
                        }
                    ],
                    "validation_claim": {"claim": "completed", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Compatible old schema.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["classification"], "worker_agent_status")
            self.assertNotIn("agent_status_problem", execution)

    def test_machine_runner_lock_does_not_replace_project_local_scheduler_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "home"
            init_project(project, "Machine runner lock should not replace scheduler locks.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            lock_key = "shared_scheduler_worker"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = write_worker_script(
                project,
                "worker_lock_observer.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                lock_path = Path(os.environ["EXPECTED_LOCK_PATH"])
                (run_dir / "lock_observed.json").write_text(
                    json.dumps({"exists_during_run": lock_path.is_file()}, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                (run_dir / "report.md").write_text("# Worker Report\\n\\nMachine lock observed.\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_lock_observer.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md"), str(run_dir / "lock_observed.json")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "completed", "checks_claimed": [], "limitations": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(
                project,
                script,
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
            )
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["runners"]["worker"]["env"]["EXPECTED_LOCK_PATH"] = lock_path.as_posix()
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            run_id = execution["run_id"]
            scheduler_run_dir = project / ".loopplane" / "runtime" / "runs" / run_id
            evidence_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / run_id
            observed = json.loads((evidence_run_dir / "lock_observed.json").read_text(encoding="utf-8"))
            adapter_result = json.loads((scheduler_run_dir / "adapter_result.json").read_text(encoding="utf-8"))
            lease = json.loads((project / ".loopplane" / "runtime" / "active_run_leases" / f"{run_id}.json").read_text(encoding="utf-8"))

            self.assertTrue(observed["exists_during_run"], observed)
            self.assertFalse(lock_path.exists())
            self.assertEqual(adapter_result["adapter_metadata"]["runner_resource_lock"]["lock_path"], lock_path.as_posix())
            self.assertTrue((project / ".loopplane" / "runtime" / "lock" / "scheduler_instance_lock").is_dir())
            self.assertTrue((project / ".loopplane" / "runtime" / "lock" / "event_append_lock").is_dir())
            self.assertFalse((project / ".loopplane" / "runtime" / "lock" / "runner_locks").exists())
            self.assertEqual(lease["status"], "completed")

    def test_running_background_worker_status_persists_registry_and_blocks_next_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Persist background job from worker status.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_background.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "logs").mkdir(exist_ok=True)
                (run_dir / "report.md").write_text("# Background Worker\\n\\nBackground work started.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python worker_background.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "running_background",
                    "next_prompt_ready": False,
                    "wake_next_agent_when": "Continue after bg_done.marker exists.",
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": None,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_background.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "running_background", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Background work started.", "highlights": [], "warnings": [], "blockers": ["background running"]},
                    "background_jobs": [
                        {
                            "job_id": "bg_fixture",
                            "task_id": os.environ["LOOPPLANE_TASK_ID"],
                            "run_id": os.environ["LOOPPLANE_RUN_ID"],
                            "status": "running",
                            "wake_next_agent_when": "Continue after bg_done.marker exists.",
                            "wake_check": {
                                "type": "file_exists",
                                "paths": [str(run_dir / "bg_done.marker")],
                            },
                            "done_marker": str(run_dir / "bg_done.marker"),
                            "logs": [str(run_dir / "logs" / "background.log")],
                        }
                    ],
                    "background": {
                        "pids": [],
                        "commands": ["python -m fixture_background"],
                        "logs": [str(run_dir / "logs" / "background.log")],
                        "heartbeat_required": True,
                        "wake_next_agent_when": "Continue after bg_done.marker exists.",
                    },
                    "repair_attempts": [],
                    "known_risks": ["background continuation unsafe"],
                    "remaining_incomplete_items": [os.environ["LOOPPLANE_TASK_ID"]],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], EXIT_WAITING_BACKGROUND_JOB, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertTrue(execution["ok"], json.dumps(execution, indent=2, sort_keys=True))
            self.assertEqual(execution["status"], "running_background")
            self.assertEqual(execution["next_step"], "waiting_background")
            self.assertEqual(execution["background_registry_update"]["job_ids"], ["bg_fixture"])
            registry = json.loads((project / ".loopplane" / "runtime" / "background_jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["schema_version"], "1.5")
            self.assertEqual(registry["jobs"][0]["job_id"], "bg_fixture")
            self.assertEqual(registry["jobs"][0]["status"], "running")
            self.assertFalse(registry["jobs"][0]["next_prompt_ready"])
            self.assertEqual(registry["jobs"][0]["wake_next_agent_when"], "Continue after bg_done.marker exists.")
            self.assertTrue(registry["jobs"][0]["heartbeat_at"])

            next_action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(next_action["action"], "wait_background_job")
            self.assertEqual(next_action["selected"]["job_id"], "bg_fixture")

    def test_blocked_needs_human_worker_status_does_not_register_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Do not treat human-blocked workers as background jobs.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_blocked_needs_human.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Human Blocked Worker\\n\\nApproval is required.\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "blocked_needs_human",
                    "next_prompt_ready": False,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_blocked_needs_human.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": "Blocked until a human decision is recorded.",
                    "summary_candidate": {"one_line": "Approval is required.", "highlights": [], "warnings": [], "blockers": ["human approval required"]},
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 1, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertFalse(execution["ok"])
            self.assertEqual(execution["status"], "blocked_needs_human")
            self.assertEqual(execution["next_step"], "recovery_pending")
            self.assertNotIn("background_registry_update", execution)
            runtime_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["status"], "worker_run_failed")
            registry = json.loads((project / ".loopplane" / "runtime" / "background_jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(registry.get("jobs", []), [])

    def test_scheduler_classifies_exit_zero_without_agent_status_as_failed_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Classify missing worker status.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_missing_status.py",
                """
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Missing Status\\n", encoding="utf-8")
                print("worker exited without agent_status")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 1, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertFalse(execution["ok"])
            self.assertEqual(execution["status"], "failed_agent")
            self.assertEqual(execution["classification"], "missing_agent_status")
            self.assertEqual(execution["adapter_exit_code"], 0)
            run_id = execution["run_id"]
            evidence_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / run_id
            agent_status = json.loads((evidence_run_dir / "agent_status.json").read_text(encoding="utf-8"))
            self.assertEqual(agent_status["status"], "failed_agent")
            self.assertEqual(agent_status["scheduler_classification"]["classification"], "missing_agent_status")
            lease = json.loads((project / ".loopplane" / "runtime" / "active_run_leases" / f"{run_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(lease["status"], "failed")
            self.assertTrue((project / ".loopplane" / "runtime" / "runs" / run_id / "adapter_result.json").is_file())

    def test_scheduler_classifies_nonzero_without_agent_status_as_failed_agent_with_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Classify real worker failure.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_real_failure.py",
                """
                import os
                import sys
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Failed Run\\n", encoding="utf-8")
                print("boom from stderr", file=sys.stderr)
                raise SystemExit(7)
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 1, json.dumps(result, indent=2, sort_keys=True))
            execution = result["selected_action"]["execution_result"]
            self.assertFalse(execution["ok"])
            self.assertEqual(execution["status"], "failed_agent")
            self.assertEqual(execution["classification"], "failed_agent")
            self.assertEqual(execution["agent_status_problem"], "missing")
            self.assertIn("boom from stderr", execution["stderr_excerpt"])
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            self.assertIn("failed_agent", registry["failures"][0]["failure_signature"])
            self.assertIn("boom from stderr", registry["failures"][0]["summary"])

    def test_run_scheduler_max_ticks_advances_multiple_successful_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Run max ticks should advance multiple tasks.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_success.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Done\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_success.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "ok", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Done.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=2, lease_heartbeat_interval_seconds=0.05)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["ticks_requested"], 2)
            self.assertEqual(result["ticks_run"], 2)
            plan = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] P0.T001: First task", plan)
            self.assertIn("- [x] P1.T001: Second task", plan)

    def test_run_scheduler_reports_max_ticks_reached_with_pending_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Run max ticks should report pending work.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            script = write_worker_script(
                project,
                "worker_one_tick.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Done\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "project_changes": [],
                    "commands_run": [{"cmd": "python worker_one_tick.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [{"task_id": os.environ["LOOPPLANE_TASK_ID"], "relationship": "primary", "acceptance_claimed": ["done"], "evidence": [str(run_dir / "report.md")]}],
                    "validation_claim": {"claim": "ok", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Done.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(json.dumps(status) + "\\n", encoding="utf-8")
                """,
            )
            configure_shell_worker(project, script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["stopped_reason"], "max_ticks_reached")
            self.assertEqual(result["pending_tasks"], 1)
            self.assertIn("--max-ticks", result["hint"])


class FailureRegistryRecoveryTest(unittest.TestCase):
    def test_worker_failure_registers_and_recovery_run_updates_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Recover a failed worker run.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            failing_script = write_worker_script(
                project,
                "worker_missing_status.py",
                """
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Missing Status\\n", encoding="utf-8")
                print("worker produced evidence but no status")
                """,
            )
            configure_shell_worker(project, failing_script)

            failed = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(failed["exit_code"], 1, json.dumps(failed, indent=2, sort_keys=True))
            failed_execution = failed["selected_action"]["execution_result"]
            registry_path = project / ".loopplane" / "runtime" / "failure_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["schema_version"], "1.5")
            self.assertEqual(len(registry["failures"]), 1)
            failure = registry["failures"][0]
            self.assertEqual(failure["status"], "unrecovered")
            self.assertEqual(failure["failure_class"], "worker_failed")
            self.assertEqual(failure["task_id"], "P0.T001")
            self.assertEqual(failure["run_id"], failed_execution["run_id"])
            self.assertIn("missing_agent_status", failure["failure_signature"])
            self.assertEqual(failure["attempts"], 1)
            self.assertEqual(failure["recovery_attempts"], 0)
            self.assertTrue(failure["budget_remaining"])

            action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(action["action"], "run_recovery")
            self.assertEqual(action["selected"]["failure_id"], failure["failure_id"])
            self.assertEqual(action["selected"]["task_id"], "P0.T001")

            recovery_script = write_worker_script(
                project,
                "recovery_success.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                raw_dir = run_dir / "raw"
                raw_dir.mkdir(exist_ok=True)
                prompt_text = Path(os.environ["LOOPPLANE_PROMPT_PATH"]).read_text(encoding="utf-8")
                (raw_dir / "prompt_contains_failure.txt").write_text(
                    str("missing_agent_status" in prompt_text and "Failure Summary" in prompt_text),
                    encoding="utf-8",
                )
                (raw_dir / "role.txt").write_text(os.environ["LOOPPLANE_ROLE"], encoding="utf-8")
                (run_dir / "report.md").write_text("# Recovery Report\\n\\nTargeted repair completed.\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python recovery_success.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python recovery_success.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "recovery_completed", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Recovery completed.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [{"failure_signature": "missing_agent_status", "new_information": True}],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, recovery_script)

            recovered = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(recovered["exit_code"], 0, json.dumps(recovered, indent=2, sort_keys=True))
            self.assertEqual(recovered["selected_action"]["action"], "run_recovery")
            recovered_execution = recovered["selected_action"]["execution_result"]
            self.assertEqual(recovered_execution["role"], "recovery_worker")
            self.assertEqual(recovered_execution["status"], "completed")
            self.assertEqual(recovered_execution["next_step"], "reconciled")
            self.assertEqual(recovered_execution["auto_reconciliation"]["status"], "reconciled")
            self.assertEqual(recovered_execution["failure_id"], failure["failure_id"])
            recovery_run_dir = project / ".loopplane" / "results" / "P0.T001" / "runs" / recovered_execution["run_id"]
            self.assertEqual((recovery_run_dir / "raw" / "role.txt").read_text(encoding="utf-8"), "recovery_worker")
            self.assertEqual((recovery_run_dir / "raw" / "prompt_contains_failure.txt").read_text(encoding="utf-8"), "True")
            adapter_input = json.loads(
                (project / ".loopplane" / "runtime" / "runs" / recovered_execution["run_id"] / "adapter_input.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(adapter_input["role"], "recovery_worker")

            updated = json.loads(registry_path.read_text(encoding="utf-8"))["failures"][0]
            self.assertEqual(updated["status"], "recovered")
            self.assertEqual(updated["recovery_attempts"], 1)
            self.assertFalse(updated["budget_remaining"])
            self.assertEqual(updated["recovery_run_ids"], [recovered_execution["run_id"]])
            self.assertEqual(updated["last_recovery_status"], "completed")
            self.assertIn("- [x] P0.T001: First task", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_failed_validation_is_registered_and_recovered_before_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Recover a validation failure.")
            write_active_plan(project, {"P0.T001": "~", "P1.T001": " "})
            validation_path = project / ".loopplane" / "results" / "P0.T001" / "runs" / "run_validation_failed" / "validation.json"
            validation_path.parent.mkdir(parents=True)
            validation_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "run_id": "run_validation_failed",
                        "primary_task_id": "P0.T001",
                        "status": "fail",
                        "failure_signature": "validation:expected-output-mismatch",
                        "summary": "Expected output was not produced.",
                        "checks": [{"name": "expected_output", "status": "fail", "message": "missing output"}],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            recovery_script = write_worker_script(
                project,
                "recovery_validation_success.py",
                """
                import json
                import os
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Validation Recovery\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python recovery_validation_success.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "completed",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python recovery_validation_success.py", "exit_code": 0}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "validation_failure_recovered", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Validation recovery completed.", "highlights": [], "warnings": [], "blockers": []},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [{"failure_signature": "validation:expected-output-mismatch", "new_information": True}],
                    "known_risks": [],
                    "remaining_incomplete_items": [],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                """,
            )
            configure_shell_worker(project, recovery_script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["selected_action"]["action"], "run_recovery")
            execution = result["selected_action"]["execution_result"]
            self.assertEqual(execution["task_id"], "P0.T001")
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            failure = registry["failures"][0]
            self.assertEqual(failure["failure_class"], "validation_failed")
            self.assertEqual(failure["failure_signature"], "validation:expected-output-mismatch")
            self.assertEqual(failure["run_id"], "run_validation_failed")
            self.assertEqual(failure["source_validation_path"], ".loopplane/results/P0.T001/runs/run_validation_failed/validation.json")
            self.assertEqual(failure["status"], "recovered")
            self.assertEqual(failure["recovery_run_ids"], [execution["run_id"]])

    def test_recurrent_recovered_validation_failure_reuses_failure_id_and_exhausts_when_budget_spent(self) -> None:
        registry = {
            "schema_version": "1.5",
            "workflow_id": "wf_fixture",
            "failures": [
                {
                    "failure_id": "fail_validation",
                    "task_id": "P0.T001",
                    "run_id": "run_validation_failed",
                    "status": "recovered",
                    "failure_class": "validation_failed",
                    "failure_signature": "validation:expected-output-mismatch",
                    "summary": "Expected output was not produced.",
                    "source_validation_path": ".loopplane/results/P0.T001/runs/run_validation_failed/validation.json",
                    "first_seen_at": "2026-06-10T00:00:00Z",
                    "last_seen_at": "2026-06-10T00:00:00Z",
                    "attempts": 1,
                    "recovery_attempts": 1,
                    "max_recovery_attempts": 1,
                    "budget_remaining": False,
                    "run_ids": ["run_validation_failed"],
                }
            ],
        }
        candidate = {
            "failure_id": "fail_new",
            "task_id": "P0.T001",
            "run_id": "run_validation_failed_again",
            "status": "unrecovered",
            "failure_class": "validation_failed",
            "failure_signature": "validation:expected-output-mismatch",
            "summary": "Expected output is still missing.",
            "source_validation_path": ".loopplane/results/P0.T001/runs/run_validation_failed_again/validation.json",
            "first_seen_at": "2026-06-10T00:05:00Z",
            "last_seen_at": "2026-06-10T00:05:00Z",
            "attempts": 1,
            "recovery_attempts": 0,
            "max_recovery_attempts": 1,
            "budget_remaining": True,
        }

        changed = _upsert_failure(registry, candidate)

        self.assertIs(changed, registry["failures"][0])
        self.assertEqual(len(registry["failures"]), 1)
        failure = registry["failures"][0]
        self.assertEqual(failure["failure_id"], "fail_validation")
        self.assertEqual(failure["status"], "exhausted")
        self.assertEqual(failure["reopened_reason"], "validation_recurred_after_recovery")
        self.assertEqual(failure["exhausted_reason"], "max_recovery_attempts_exhausted")
        self.assertEqual(failure["run_id"], "run_validation_failed_again")
        self.assertEqual(failure["attempts"], 2)
        self.assertFalse(failure["budget_remaining"])
        self.assertEqual(failure["run_ids"], ["run_validation_failed", "run_validation_failed_again"])

    def test_identical_recovery_failure_without_new_information_exhausts_and_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Do not repeat identical recovery failures.")
            write_active_plan(project, {"P0.T001": " ", "P1.T001": " "})
            registry = {
                "schema_version": "1.5",
                "workflow_id": json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                    "workflow_id"
                ],
                "failures": [
                    {
                        "failure_id": "fail_same",
                        "task_id": "P0.T001",
                        "run_id": "run_failed",
                        "status": "unrecovered",
                        "failure_class": "worker_failed",
                        "failure_signature": "same-signature",
                        "summary": "The same failure already happened.",
                        "first_seen_at": "2026-06-10T00:00:00Z",
                        "last_seen_at": "2026-06-10T00:00:00Z",
                        "attempts": 1,
                        "recovery_attempts": 0,
                        "max_recovery_attempts": 3,
                        "budget_remaining": True,
                    }
                ],
            }
            (project / ".loopplane" / "runtime" / "failure_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            recovery_script = write_worker_script(
                project,
                "recovery_same_failure.py",
                """
                import json
                import os
                import sys
                from pathlib import Path

                run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text("# Same Failure\\n", encoding="utf-8")
                (run_dir / "commands.sh").write_text("python recovery_same_failure.py\\n", encoding="utf-8")
                status = {
                    "schema_version": "1.5",
                    "run_id": os.environ["LOOPPLANE_RUN_ID"],
                    "task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                    "phase": "Phase P0: Scheduler Fixture",
                    "status": "failed_agent",
                    "failure_signature": "same-signature",
                    "next_prompt_ready": True,
                    "started_at": "2026-06-10T00:00:00Z",
                    "ended_at": "2026-06-10T00:00:01Z",
                    "project_changes": [],
                    "commands_run": [{"cmd": "python recovery_same_failure.py", "exit_code": 1}],
                    "key_outputs": [str(run_dir / "report.md")],
                    "evidence_satisfies": [],
                    "validation_claim": {"claim": "failed_agent", "checks_claimed": [], "limitations": []},
                    "summary_candidate": {"one_line": "Same failure repeated.", "highlights": [], "warnings": [], "blockers": ["same-signature"]},
                    "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
                    "repair_attempts": [{"failure_signature": "same-signature", "new_information": False}],
                    "known_risks": ["same failure repeated"],
                    "remaining_incomplete_items": [os.environ["LOOPPLANE_TASK_ID"]],
                }
                (run_dir / "agent_status.json").write_text(
                    json.dumps(status, indent=2, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )
                sys.exit(1)
                """,
            )
            configure_shell_worker(project, recovery_script)

            result = run_scheduler(project, max_ticks=1, lease_heartbeat_interval_seconds=0.05)

            self.assertEqual(result["exit_code"], 5, json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["selected_action"]["action"], "run_recovery")
            failure = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))[
                "failures"
            ][0]
            self.assertEqual(failure["status"], "exhausted")
            self.assertEqual(failure["recovery_attempts"], 1)
            self.assertEqual(
                failure["exhausted_reason"],
                "recovery_repeated_identical_failure_without_new_information",
            )

            next_action = select_next_action(load_scheduler_snapshot(project))
            self.assertNotEqual(next_action["action"], "run_recovery")
            self.assertEqual(next_action["action"], "run_expansion_planner")

    def test_exhausted_failure_selects_self_expansion_before_later_executable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Exhausted failures must expand before later work.")
            write_active_plan(project, {"P0.T001": "x", "P1.T001": " "})
            workflow_id = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                "workflow_id"
            ]
            registry = {
                "schema_version": "1.5",
                "workflow_id": workflow_id,
                "failures": [
                    {
                        "failure_id": "fail_exhausted",
                        "task_id": "P0.T001",
                        "run_id": "run_failed",
                        "status": "exhausted",
                        "failure_class": "validation_failed",
                        "failure_signature": "validation:missing-required-evidence",
                        "summary": "Required validation evidence is still missing.",
                        "source_validation_path": ".loopplane/results/P0.T001/runs/run_failed/validation.json",
                        "first_seen_at": "2026-06-10T00:00:00Z",
                        "last_seen_at": "2026-06-10T00:05:00Z",
                        "attempts": 1,
                        "recovery_attempts": 1,
                        "max_recovery_attempts": 1,
                        "budget_remaining": False,
                        "exhausted_reason": "max_recovery_attempts_exhausted",
                    }
                ],
            }
            write_json(project / ".loopplane" / "runtime" / "failure_registry.json", registry)

            action = select_next_action(load_scheduler_snapshot(project))

            self.assertEqual(action["action"], "run_expansion_planner")
            self.assertEqual(action["selected"]["candidate"]["trigger"], "recovery_exhausted")
            self.assertEqual(action["selected"]["candidate"]["target_task_ids"], ["P0.T001"])


class SchedulerMainLoopTest(unittest.TestCase):
    def test_event_append_writes_monotonic_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Event chain smoke.")
            context_result = load_scheduler_context(project)
            self.assertTrue(context_result["ok"], context_result)
            context = context_result["context"]

            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="first_event",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )
            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="second_event",
                data={"task_id": "T002"},
                run_id="run_002",
                snapshot_interval=None,
            )

            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertEqual([event["seq"] for event in events], [1, 2])
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(events[0]["event_id"], "evt_000000000001")
            self.assertEqual(events[1]["event_id"], "evt_000000000002")
            self.assertIsNone(events[0]["prev_event_id"])
            self.assertIsNone(events[0]["prev_event_hash"])
            self.assertEqual(events[1]["prev_event_id"], events[0]["event_id"])
            self.assertEqual(events[1]["prev_event_hash"], events[0]["event_hash"])
            self.assertEqual(events[0]["event_hash"], event_hash(events[0]))
            self.assertEqual(events[1]["event_hash"], event_hash(events[1]))
            self.assertEqual(events[1]["payload"]["task_id"], "T002")
            self.assertEqual(events[1]["subject"]["run_id"], "run_002")

    def test_event_append_requires_event_append_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Event lock smoke.")
            context_result = load_scheduler_context(project)
            self.assertTrue(context_result["ok"], context_result)
            context = context_result["context"]
            lock = AtomicOwnerLock(context.paths.runtime_dir / "lock" / "event_append_lock", "test-lock-holder")
            held = lock.acquire()
            try:
                with self.assertRaises(SchedulerLockError):
                    append_event(
                        context.paths,
                        workflow_id=context.workflow_id,
                        event_type="blocked_event",
                        data={},
                    )
            finally:
                held.release()

            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertEqual(events, [])

    def test_event_snapshot_replay_uses_latest_snapshot_and_subsequent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Event snapshot smoke.")
            context_result = load_scheduler_context(project)
            self.assertTrue(context_result["ok"], context_result)
            context = context_result["context"]

            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="first_event",
                data={},
                snapshot_interval=2,
            )
            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="second_event",
                data={},
                snapshot_interval=2,
            )
            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="third_event",
                data={},
                snapshot_interval=2,
            )

            snapshot = load_latest_event_snapshot(context.paths)
            self.assertEqual(snapshot["events_through_sequence"], 2)
            self.assertEqual(snapshot["state"]["event_count"], 2)
            self.assertEqual(snapshot["state"]["event_type_counts"]["first_event"], 1)
            self.assertEqual(snapshot["state"]["event_type_counts"]["second_event"], 1)
            replayed = replay_events_after_snapshot(context.paths, snapshot=snapshot)
            self.assertEqual([event["event_type"] for event in replayed], ["third_event"])

            projection = load_event_log_projection(context.paths)
            self.assertEqual(projection["events_replayed"], 1)
            self.assertEqual(projection["state"]["event_count"], 3)
            self.assertEqual(projection["state"]["event_type_counts"]["third_event"], 1)
            self.assertEqual(projection["state"]["latest_event"]["event_type"], "third_event")

    def test_scheduler_tick_consumes_control_request_and_appends_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Consume a control request.")
            append_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl", {"request_id": "ctrl1", "action": "pause"})

            result = run_scheduler(project, max_ticks=1)

            self.assertEqual(result["exit_code"], 0, json.dumps(result, indent=2, sort_keys=True))
            responses = read_jsonl(project / ".loopplane" / "runtime" / "control_responses.jsonl")
            self.assertEqual(responses[-1]["request_id"], "ctrl1")
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "paused")
            self.assertTrue(state["scheduler"]["paused"])
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            event_types = [event["event_type"] for event in events]
            self.assertIn("scheduler_tick", event_types)
            self.assertIn("scheduler_action_selected", event_types)
            self.assertIn("control_request_handled", event_types)

    def test_duplicate_scheduler_exits_11_while_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Duplicate scheduler smoke.")
            context_result = load_scheduler_context(project)
            self.assertTrue(context_result["ok"], context_result)
            context = context_result["context"]
            lock = AtomicOwnerLock(context.paths.runtime_dir / "lock" / "scheduler_instance_lock", "test-owner")
            held = lock.acquire()
            try:
                completed = subprocess.run(
                    [sys.executable, str(LoopPlane), "tick", "--project", str(project), "--json"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            finally:
                held.release()

            self.assertEqual(completed.returncode, EXIT_DUPLICATE_SCHEDULER, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "duplicate_scheduler")
            self.assertEqual(payload["exit_code"], EXIT_DUPLICATE_SCHEDULER)

    def test_scheduler_reclaims_stale_dead_owner_lock_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Stale scheduler lock recovery smoke.")
            context_result = load_scheduler_context(project)
            self.assertTrue(context_result["ok"], context_result)
            context = context_result["context"]
            owner_path = context.paths.runtime_dir / "lock" / "scheduler_instance_lock" / "owner.json"
            write_json(
                owner_path,
                {
                    "schema_version": "1.5",
                    "owner": "stale-host:99999999:deadbeef",
                    "pid": 99999999,
                    "started_at": timestamp(timedelta(hours=-1)),
                    "heartbeat_at": timestamp(timedelta(hours=-1)),
                    "ttl_seconds": 1,
                },
            )

            result = run_scheduler(project, max_ticks=1)

            self.assertNotEqual(result["status"], "duplicate_scheduler", json.dumps(result, indent=2, sort_keys=True))
            self.assertNotEqual(result["exit_code"], EXIT_DUPLICATE_SCHEDULER)
            self.assertFalse(owner_path.exists())

    def test_loopplane_tick_summary_json_emits_compact_scheduler_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Scheduler summary CLI smoke.")

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "tick", "--project", str(project), "--summary", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(completed.returncode, EXIT_DUPLICATE_SCHEDULER, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertIn("status", payload)
            self.assertIn("action", payload)
            self.assertIn("pending_tasks", payload)
            self.assertNotIn("selected_action", payload)


if __name__ == "__main__":
    unittest.main()
