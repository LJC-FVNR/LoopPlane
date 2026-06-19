from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.control import record_control_request
from runtime.exit_codes import EXIT_GENERIC_FAILURE, EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.reconciliation import reconciliation_exit_code, run_reconciler
from runtime.scheduler import run_scheduler, scheduler_exit_code
from runtime.validation import run_validator, validation_exit_code


SCHEMA_VERSION = "1.5"
SUPERVISOR_FILENAME = "supervisor.json"
SUPERVISOR_LOG_DIRNAME = "supervisor"
SUPERVISOR_STDOUT_FILENAME = "supervisor_stdout.log"
SUPERVISOR_STDERR_FILENAME = "supervisor_stderr.log"
SUPERVISOR_HEARTBEAT_TTL_SECONDS = 120


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return max(0.05, float(raw_value))
    except ValueError:
        return default


DEFAULT_POLL_INTERVAL_SECONDS = _positive_float_from_env("LOOPPLANE_SUPERVISOR_POLL_INTERVAL_SECONDS", 0.2)
BACKGROUND_WAIT_INTERVAL_SECONDS = _positive_float_from_env("LOOPPLANE_BACKGROUND_WAIT_INTERVAL_SECONDS", 60.0)
TERMINAL_SUPERVISOR_STATUSES = frozenset(
    {
        "completed",
        "stopped",
        "requires_attention",
        "failed",
        "exited",
    }
)
ACTIVE_SUPERVISOR_STATUSES = frozenset(
    {
        "launching",
        "running",
        "paused",
        "waiting_config",
        "waiting_approval",
        "waiting_background",
    }
)
RECOVERABLE_WAIT_ACTIONS = frozenset({"wait_paused", "wait_config", "wait_approval"})
RECOVERABLE_WAIT_INTERVAL_SECONDS = 1.0


def start_detached_scheduler(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        context = _load_supervisor_context(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(project, f"Unable to load workflow configuration: {error}", started_at=started_at)
    paths = context["paths"]

    control_result = record_control_request(project, "start", source="cli", payload={"detach": True})
    if not control_result.get("ok"):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "control_request_failed",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "control_request": control_result,
            "supervisor": load_supervisor_status(project),
            "errors": list(control_result.get("errors") or []),
            "warnings": [],
        }

    existing = load_supervisor_status(project)
    if existing.get("liveness") == "alive":
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "already_running",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "request": control_result.get("request"),
            "requests_path": control_result.get("requests_path"),
            "responses_path": control_result.get("responses_path"),
            "pending_count": control_result.get("pending_count"),
            "control_request": control_result,
            "supervisor": existing,
            "errors": [],
            "warnings": ["A detached scheduler supervisor is already running."],
        }

    supervisor = _launch_supervisor_process(project, context, started_at=started_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "request": control_result.get("request"),
        "requests_path": control_result.get("requests_path"),
        "responses_path": control_result.get("responses_path"),
        "pending_count": control_result.get("pending_count"),
        "control_request": control_result,
        "supervisor": supervisor,
        "errors": [],
        "warnings": [],
    }


def resume_detached_scheduler(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        context = _load_supervisor_context(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(project, f"Unable to load workflow configuration: {error}", started_at=started_at)
    paths = context["paths"]

    control_result = record_control_request(project, "resume", source="cli")
    if not control_result.get("ok"):
        result = dict(control_result)
        result["supervisor"] = load_supervisor_status(project)
        result["detached_resume"] = {
            "attempted": False,
            "reason": "control_request_failed",
        }
        return result

    existing = load_supervisor_status(project)
    result = dict(control_result)
    result["supervisor"] = existing
    launch_reason = _detached_resume_launch_reason(paths, existing)
    if launch_reason is None:
        result["detached_resume"] = {
            "attempted": False,
            "reason": "supervisor_not_resume_launchable",
        }
        return result
    if existing.get("liveness") == "alive":
        if launch_reason == "stopped":
            existing = _wait_for_supervisor_not_alive(project, timeout_seconds=2.0)
            result["supervisor"] = existing
            launch_reason = _detached_resume_launch_reason(paths, existing)
            if launch_reason is None:
                result["detached_resume"] = {
                    "attempted": False,
                    "reason": "supervisor_not_resume_launchable",
                }
                return result
        if existing.get("liveness") != "alive":
            supervisor = _launch_supervisor_process(project, context, started_at=started_at)
            result["supervisor"] = supervisor
            result["detached_resume"] = {
                "attempted": True,
                "reason": launch_reason,
                "status": "started",
                "started_at": started_at,
                "ended_at": utc_timestamp(),
            }
            return result
        result["detached_resume"] = {
            "attempted": False,
            "reason": "supervisor_already_alive",
        }
        return result

    supervisor = _launch_supervisor_process(project, context, started_at=started_at)
    result["supervisor"] = supervisor
    result["detached_resume"] = {
        "attempted": True,
        "reason": launch_reason,
        "status": "started",
        "started_at": started_at,
        "ended_at": utc_timestamp(),
    }
    return result


def run_supervisor(project_root: Path | str, *, poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> int:
    project = Path(project_root).expanduser().resolve()
    owner = _supervisor_owner()
    try:
        context = _load_supervisor_context(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        print(f"loopplane supervisor: unable to load workflow configuration: {error}", file=sys.stderr)
        return EXIT_INVALID_CONFIG

    paths = context["paths"]
    _merge_supervisor_metadata(
        paths,
        {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": context["workflow_id"],
            "project_root": project.as_posix(),
            "status": "running",
            "pid": os.getpid(),
            "process_handle": {"pid": os.getpid()},
            "owner": owner,
            "started_at": _existing_started_at(paths) or utc_timestamp(),
            "updated_at": utc_timestamp(),
            "heartbeat_at": utc_timestamp(),
            "exit_status": None,
            "exit_code": None,
            "ended_at": None,
        },
    )

    exit_code = EXIT_SUCCESS
    exit_status = "completed"
    stop_reason = "complete"
    poll_interval = max(0.05, float(poll_interval_seconds))
    try:
        while True:
            _heartbeat(paths, owner=owner, status="running")
            result = run_scheduler(project, max_ticks=1)
            selected = result.get("selected_action") if isinstance(result.get("selected_action"), Mapping) else {}
            action = str(selected.get("action") or "")
            follow_up = _complete_worker_follow_up(project, selected)
            should_continue, reason, loop_exit_code = _should_continue_after_tick(result, selected, follow_up)
            supervisor_update = {
                "status": _supervisor_status_after_tick(action, should_continue=should_continue, reason=reason),
                "updated_at": utc_timestamp(),
                "heartbeat_at": utc_timestamp(),
                "last_scheduler_result": _compact_scheduler_result(result),
                "last_action": action,
                "last_exit_code": scheduler_exit_code(result),
                "last_loop_reason": reason,
            }
            if follow_up is not None:
                supervisor_update["last_follow_up"] = follow_up
            _merge_supervisor_metadata(paths, supervisor_update)

            if not should_continue:
                exit_code = loop_exit_code
                exit_status = _exit_status_for_reason(reason)
                stop_reason = reason
                break

            time.sleep(_poll_interval_after_tick(action, poll_interval))
    except BaseException as error:
        exit_code = EXIT_GENERIC_FAILURE
        exit_status = "failed"
        stop_reason = f"exception:{error.__class__.__name__}"
        _merge_supervisor_metadata(
            paths,
            {
                "status": "failed",
                "updated_at": utc_timestamp(),
                "heartbeat_at": utc_timestamp(),
                "exit_status": exit_status,
                "exit_code": exit_code,
                "ended_at": utc_timestamp(),
                "stop_reason": stop_reason,
                "error": str(error),
            },
        )
        raise
    finally:
        _merge_supervisor_metadata(
            paths,
            {
                "status": exit_status,
                "updated_at": utc_timestamp(),
                "heartbeat_at": utc_timestamp(),
                "exit_status": exit_status,
                "exit_code": exit_code,
                "ended_at": utc_timestamp(),
                "stop_reason": stop_reason,
            },
        )
    return exit_code


def load_supervisor_status(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    try:
        context = _load_supervisor_context(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "exists": False,
            "status": "unavailable",
            "project_root": project.as_posix(),
            "workflow_id": None,
            "metadata_path": None,
            "metadata": {},
            "liveness": "unavailable",
            "errors": [str(error)],
            "warnings": [],
        }
    paths = context["paths"]
    metadata_path = _supervisor_metadata_path(paths)
    metadata = _read_json(metadata_path, default={})
    if not isinstance(metadata, Mapping) or not metadata:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "exists": False,
            "status": "absent",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "metadata_path": _path_for_record(project, metadata_path),
            "metadata": {},
            "liveness": "absent",
            "errors": [],
            "warnings": [],
        }

    pid = _positive_int(metadata.get("pid"))
    alive = _pid_exists(pid) if pid is not None else None
    liveness = "alive" if alive is True else ("dead" if alive is False else "unknown")
    metadata_status = str(metadata.get("status") or "unknown")
    heartbeat_stale = _heartbeat_is_stale(metadata.get("heartbeat_at"))
    status = metadata_status
    warnings: list[str] = []
    status_problems: list[str] = []
    missing_fields = _missing_supervisor_metadata_fields(metadata)
    active_or_unknown = metadata_status in ACTIVE_SUPERVISOR_STATUSES or metadata_status == "unknown"
    if metadata_status not in TERMINAL_SUPERVISOR_STATUSES and alive is False:
        status = "stale"
        status_problems.append("dead_process")
        warnings.append("Supervisor PID is no longer alive.")
    if metadata_status not in TERMINAL_SUPERVISOR_STATUSES and active_or_unknown and heartbeat_stale:
        status = "stale"
        status_problems.append("stale_heartbeat")
        warnings.append("Supervisor heartbeat is stale.")
    if metadata_status not in TERMINAL_SUPERVISOR_STATUSES and active_or_unknown and missing_fields:
        status = "stale"
        status_problems.append("incomplete_metadata")
        warnings.append("Supervisor metadata is incomplete: missing " + ", ".join(missing_fields) + ".")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "exists": True,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "metadata_path": _path_for_record(project, metadata_path),
        "metadata": dict(metadata),
        "pid": pid,
        "liveness": liveness,
        "heartbeat_stale": heartbeat_stale,
        "status_problem": status_problems[0] if status_problems else None,
        "status_problems": status_problems,
        "errors": [],
        "warnings": warnings,
    }


def load_supervisor_logs(project_root: Path | str, *, lines: int = 50) -> dict[str, Any]:
    status = load_supervisor_status(project_root)
    project = Path(project_root).expanduser().resolve()
    log_paths = status.get("metadata", {}).get("log_paths") if isinstance(status.get("metadata"), Mapping) else {}
    stdout_path = _resolve_project_path(project, log_paths.get("stdout")) if isinstance(log_paths, Mapping) else None
    stderr_path = _resolve_project_path(project, log_paths.get("stderr")) if isinstance(log_paths, Mapping) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(status.get("ok")),
        "status": status.get("status"),
        "supervisor": status,
        "stdout": _tail_text(stdout_path, lines=lines) if stdout_path is not None else [],
        "stderr": _tail_text(stderr_path, lines=lines) if stderr_path is not None else [],
        "stdout_path": stdout_path.as_posix() if stdout_path is not None else None,
        "stderr_path": stderr_path.as_posix() if stderr_path is not None else None,
    }


def detached_start_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") else EXIT_GENERIC_FAILURE


def format_detached_start_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane start: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    supervisor = result.get("supervisor")
    if isinstance(supervisor, Mapping):
        if supervisor.get("pid") is not None:
            lines.append(f"supervisor_pid: {supervisor.get('pid')}")
        if supervisor.get("status"):
            lines.append(f"supervisor_status: {supervisor.get('status')}")
        if supervisor.get("liveness"):
            lines.append(f"supervisor_liveness: {supervisor.get('liveness')}")
        if supervisor.get("metadata_path"):
            lines.append(f"supervisor_metadata: {supervisor.get('metadata_path')}")
        metadata = supervisor.get("metadata")
        log_paths = metadata.get("log_paths") if isinstance(metadata, Mapping) else None
        if isinstance(log_paths, Mapping):
            for key in ("stdout", "stderr"):
                if log_paths.get(key):
                    lines.append(f"supervisor_{key}: {log_paths[key]}")
    control = result.get("control_request")
    request = control.get("request") if isinstance(control, Mapping) else None
    if isinstance(request, Mapping):
        lines.append(f"request_id: {request.get('request_id')}")
    _append_problem_lines(lines, result)
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runtime.detached")
    subparsers = parser.add_subparsers(dest="command", required=True)
    supervisor = subparsers.add_parser("supervisor")
    supervisor.add_argument("--project", required=True)
    supervisor.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    if args.command == "supervisor":
        return run_supervisor(args.project, poll_interval_seconds=args.poll_interval)
    return EXIT_GENERIC_FAILURE


def _complete_worker_follow_up(project: Path, selected: Mapping[str, Any]) -> dict[str, Any] | None:
    action = str(selected.get("action") or "")
    if action not in {"run_worker", "run_recovery"}:
        return None
    execution = selected.get("execution_result")
    if not isinstance(execution, Mapping):
        return None
    auto_follow_up = _auto_follow_up_from_execution(execution)
    if auto_follow_up is not None:
        return auto_follow_up
    if str(execution.get("next_step") or "") != "validation_pending":
        return None
    task_id = str(execution.get("task_id") or "").strip()
    run_dir_value = execution.get("role_output_dir")
    if not task_id or not isinstance(run_dir_value, str) or not run_dir_value.strip():
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "invalid_worker_result",
            "message": "Worker result did not include task_id and role_output_dir for validation.",
        }

    validation = run_validator(project, task_id=task_id, run_dir=run_dir_value, write=True)
    reconciliation = run_reconciler(project, task_id=task_id, run_dir=run_dir_value, write=True)
    validation_ok = validation_exit_code(validation) == EXIT_SUCCESS
    reconciliation_ok = reconciliation_exit_code(reconciliation) == EXIT_SUCCESS
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": validation_ok and reconciliation_ok,
        "status": "reconciled" if reconciliation_ok else "reconciliation_failed",
        "task_id": task_id,
        "run_dir": run_dir_value,
        "validation": _compact_mapping(validation),
        "reconciliation": _compact_mapping(reconciliation),
    }


def _auto_follow_up_from_execution(execution: Mapping[str, Any]) -> dict[str, Any] | None:
    validation = execution.get("auto_validation")
    reconciliation = execution.get("auto_reconciliation")
    if not isinstance(validation, Mapping) and not isinstance(reconciliation, Mapping):
        return None
    task_id = str(execution.get("task_id") or "").strip()
    run_dir_value = str(execution.get("role_output_dir") or "").strip()
    validation_ok = validation_exit_code(validation) == EXIT_SUCCESS if isinstance(validation, Mapping) else False
    reconciliation_status = str(reconciliation.get("status") or "") if isinstance(reconciliation, Mapping) else ""
    reconciliation_ok = reconciliation_status in {"reconciled", "skipped"}
    status = "reconciled" if reconciliation_status == "reconciled" else "reconciliation_skipped" if reconciliation_ok else "reconciliation_failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": validation_ok and reconciliation_ok,
        "status": status,
        "task_id": task_id or None,
        "run_dir": run_dir_value or None,
        "validation": _compact_mapping(validation) if isinstance(validation, Mapping) else None,
        "reconciliation": _compact_mapping(reconciliation) if isinstance(reconciliation, Mapping) else None,
    }


def _should_continue_after_tick(
    result: Mapping[str, Any],
    selected: Mapping[str, Any],
    follow_up: Mapping[str, Any] | None,
) -> tuple[bool, str, int]:
    action = str(selected.get("action") or "")
    exit_code = scheduler_exit_code(result)
    if follow_up is not None and not follow_up.get("ok"):
        if action in {"run_worker", "run_recovery"} and _follow_up_should_continue_for_recovery(
            follow_up
        ):
            return True, "recovery_pending", EXIT_SUCCESS
        return False, "follow_up_failed", EXIT_GENERIC_FAILURE
    if action in {
        "handle_control_request",
        "run_worker",
        "run_recovery",
        "run_expansion_planner",
        "run_phase_objective_verifier",
        "run_final_objective_verifier",
        "resolve_expansion_failure",
    }:
        return True, "continue", EXIT_SUCCESS
    if action == "run_final_verification":
        execution = selected.get("execution_result")
        if isinstance(execution, Mapping) and (execution.get("pass") is True or execution.get("status") == "pass"):
            return True, "final_verification_passed", EXIT_SUCCESS
        if _final_verification_has_expandable_blocker(execution):
            return True, "final_verification_expandable", EXIT_SUCCESS
        return False, "final_verification_failed", exit_code
    if action == "wait_background_job":
        return True, "waiting_background_job", EXIT_SUCCESS
    if action in RECOVERABLE_WAIT_ACTIONS:
        return True, action, EXIT_SUCCESS
    if action == "complete":
        return False, "complete", EXIT_SUCCESS
    if action == "wait_stopped":
        return False, action, exit_code
    if action == "requires_attention":
        return False, action, exit_code
    if action == "wait_no_executable_work":
        return False, action, EXIT_GENERIC_FAILURE
    if not result.get("ok"):
        return False, "scheduler_failed", exit_code
    return False, action or "unknown", exit_code


def _follow_up_should_continue_for_recovery(follow_up: Mapping[str, Any]) -> bool:
    validation = follow_up.get("validation")
    reconciliation = follow_up.get("reconciliation")
    validation_status = str(validation.get("status") or "") if isinstance(validation, Mapping) else ""
    reconciliation_status = str(reconciliation.get("status") or "") if isinstance(reconciliation, Mapping) else ""
    if validation_status == "needs_human" or reconciliation_status == "needs_human":
        return False
    if reconciliation_status in {"validation_failed", "recovery_pending", "recovery_exhausted"}:
        return True
    return validation_status in {"fail", "blocked"}


def _final_verification_has_expandable_blocker(execution: Any) -> bool:
    if not isinstance(execution, Mapping):
        return False
    blockers = execution.get("blockers")
    if not isinstance(blockers, Sequence) or isinstance(blockers, (str, bytes)):
        return False
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            continue
        if blocker.get("expandable") is True:
            return True
    return False


def _supervisor_status_after_tick(action: str, *, should_continue: bool, reason: str) -> str:
    if not should_continue:
        return _exit_status_for_reason(reason)
    return {
        "wait_background_job": "waiting_background",
        "wait_paused": "paused",
        "wait_config": "waiting_config",
        "wait_approval": "waiting_approval",
    }.get(action, "running")


def _poll_interval_after_tick(action: str, poll_interval: float) -> float:
    if action == "wait_background_job":
        return BACKGROUND_WAIT_INTERVAL_SECONDS
    if action in RECOVERABLE_WAIT_ACTIONS:
        return max(poll_interval, RECOVERABLE_WAIT_INTERVAL_SECONDS)
    return poll_interval


def _exit_status_for_reason(reason: str) -> str:
    return {
        "complete": "completed",
        "wait_paused": "paused",
        "wait_stopped": "stopped",
        "wait_config": "waiting_config",
        "wait_approval": "waiting_approval",
        "requires_attention": "requires_attention",
        "wait_no_executable_work": "requires_attention",
        "follow_up_failed": "failed",
        "scheduler_failed": "failed",
        "final_verification_failed": "failed",
    }.get(reason, "exited")


def _compact_scheduler_result(result: Mapping[str, Any]) -> dict[str, Any]:
    selected = result.get("selected_action")
    selected_compact = _compact_mapping(selected) if isinstance(selected, Mapping) else None
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "message": result.get("message"),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "selected_action": selected_compact,
    }


def _compact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for key in (
        "schema_version",
        "ok",
        "status",
        "action",
        "reason",
        "message",
        "exit_code",
        "task_id",
        "run_id",
        "role",
        "runner_id",
        "adapter",
        "classification",
        "next_step",
        "primary_task_id",
        "accepted_task_ids",
        "rejected_task_ids",
        "validation_path",
        "run_dir",
        "resulting_workflow_status",
    ):
        if key in value:
            compact[key] = value[key]
    selected = value.get("selected")
    if isinstance(selected, Mapping):
        compact["selected"] = _compact_mapping(selected)
    execution = value.get("execution_result")
    if isinstance(execution, Mapping):
        compact["execution_result"] = _compact_mapping(execution)
    return compact


def _append_problem_lines(lines: list[str], result: Mapping[str, Any]) -> None:
    problems = result.get("configuration_problems")
    if isinstance(problems, Sequence) and not isinstance(problems, (str, bytes)) and problems:
        lines.append("configuration_problems:")
        for problem in problems:
            if not isinstance(problem, Mapping):
                lines.append(f"  - {problem}")
                continue
            code = problem.get("code") or "configuration_problem"
            message = problem.get("message") or problem.get("reason") or "Workflow configuration requires attention."
            lines.append(f"  - {code}: {message}")
            actions = problem.get("recovery_actions")
            if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
                for action in actions:
                    lines.append(f"    recovery: {action}")


def _load_supervisor_context(project: Path) -> dict[str, Any]:
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    return {
        "workflow_id": str(workflow.get("workflow_id") or "unknown_workflow"),
        "workflow": workflow,
        "paths": paths,
    }


def _supervisor_metadata_path(paths: WorkflowPaths) -> Path:
    return paths.runtime_dir / SUPERVISOR_FILENAME


def _supervisor_log_paths(paths: WorkflowPaths) -> dict[str, Path]:
    log_dir = paths.runtime_dir / SUPERVISOR_LOG_DIRNAME
    return {
        "stdout": log_dir / SUPERVISOR_STDOUT_FILENAME,
        "stderr": log_dir / SUPERVISOR_STDERR_FILENAME,
    }


def _launch_supervisor_process(project: Path, context: Mapping[str, Any], *, started_at: str) -> dict[str, Any]:
    paths = context["paths"]
    logs = _supervisor_log_paths(paths)
    for path in logs.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "runtime.detached",
        "supervisor",
        "--project",
        project.as_posix(),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = _pythonpath_with_repo(repo_root, env.get("PYTHONPATH"))
    _merge_supervisor_metadata(
        paths,
        {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": context["workflow_id"],
            "project_root": project.as_posix(),
            "status": "launching",
            "started_at": started_at,
            "updated_at": started_at,
            "heartbeat_at": started_at,
            "pid": None,
            "command": command,
            "command_display": _display_command(command),
            "log_paths": _relative_log_paths(project, logs),
            "exit_status": None,
            "exit_code": None,
            "ended_at": None,
        },
    )

    with logs["stdout"].open("ab", buffering=0) as stdout, logs["stderr"].open("ab", buffering=0) as stderr:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )

    _merge_supervisor_metadata(
        paths,
        {
            "status": "running",
            "pid": process.pid,
            "process_handle": {"pid": process.pid},
            "updated_at": utc_timestamp(),
            "heartbeat_at": utc_timestamp(),
        },
    )
    return load_supervisor_status(project)


def _detached_resume_launch_reason(paths: WorkflowPaths, supervisor: Mapping[str, Any]) -> str | None:
    state = _read_json(paths.runtime_dir / "state.json", default={})
    scheduler = state.get("scheduler") if isinstance(state, Mapping) else None
    detached_requested = isinstance(scheduler, Mapping) and scheduler.get("detach_requested") is True
    supervisor_exists = supervisor.get("exists") is True
    if not detached_requested and not supervisor_exists:
        return None
    if supervisor.get("liveness") == "alive":
        return "supervisor_already_alive"
    runtime_status = str(state.get("status") or "").strip().lower() if isinstance(state, Mapping) else ""
    supervisor_status = str(supervisor.get("status") or "").strip().lower()
    if runtime_status == "stopped" or supervisor_status == "stopped":
        return "stopped"
    if supervisor_status == "stale":
        return "stale_supervisor"
    return None


def _wait_for_supervisor_not_alive(project: Path, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest = load_supervisor_status(project)
    while latest.get("liveness") == "alive" and time.monotonic() < deadline:
        time.sleep(0.05)
        latest = load_supervisor_status(project)
    return latest


def _relative_log_paths(project: Path, logs: Mapping[str, Path]) -> dict[str, str]:
    return {key: _path_for_record(project, path) for key, path in logs.items()}


def _heartbeat(paths: WorkflowPaths, *, owner: str, status: str) -> None:
    _merge_supervisor_metadata(
        paths,
        {
            "status": status,
            "owner": owner,
            "updated_at": utc_timestamp(),
            "heartbeat_at": utc_timestamp(),
            "pid": os.getpid(),
        },
    )


def _existing_started_at(paths: WorkflowPaths) -> str | None:
    metadata = _read_json(_supervisor_metadata_path(paths), default={})
    if isinstance(metadata, Mapping) and isinstance(metadata.get("started_at"), str):
        return str(metadata["started_at"])
    return None


def _merge_supervisor_metadata(paths: WorkflowPaths, updates: Mapping[str, Any]) -> dict[str, Any]:
    metadata_path = _supervisor_metadata_path(paths)
    existing = _read_json(metadata_path, default={})
    metadata = dict(existing) if isinstance(existing, Mapping) else {}
    metadata.update(_json_safe(updates))
    metadata["schema_version"] = str(metadata.get("schema_version") or SCHEMA_VERSION)
    metadata["updated_at"] = str(metadata.get("updated_at") or utc_timestamp())
    _write_json_atomic(metadata_path, metadata)
    return metadata


def _failure(project: Path, message: str, *, started_at: str) -> dict[str, Any]:
    problem = _workflow_config_problem(project, message)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "waiting_config",
        "project_root": project.as_posix(),
        "workflow_id": None,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "message": message,
        "configuration_problems": [problem],
        "recovery_actions": list(problem["recovery_actions"]),
        "errors": [message],
        "warnings": [],
    }


def _pythonpath_with_repo(repo_root: Path, current: str | None) -> str:
    values = [repo_root.as_posix()]
    if current:
        values.append(current)
    return os.pathsep.join(values)


def _display_command(command: Sequence[str]) -> str:
    return " ".join(command)


def _supervisor_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _path_for_record(project_root: Path | None, path: Path) -> str:
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _resolve_project_path(project: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        path = project / path
    return path


def _tail_text(path: Path, *, lines: int) -> list[str]:
    if not path.is_file():
        return []
    limit = max(1, int(lines))
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError as error:
        return [f"<unable to read {path}: {error}>"]


def _heartbeat_is_stale(value: object) -> bool:
    heartbeat = _parse_iso_timestamp(value)
    if heartbeat is None:
        return True
    age = datetime.now(UTC) - heartbeat
    return age.total_seconds() > SUPERVISOR_HEARTBEAT_TTL_SECONDS


def _missing_supervisor_metadata_fields(metadata: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if _positive_int(metadata.get("pid")) is None:
        missing.append("pid")
    if not isinstance(metadata.get("heartbeat_at"), str) or not str(metadata.get("heartbeat_at") or "").strip():
        missing.append("heartbeat_at")
    if not isinstance(metadata.get("command"), Sequence) or isinstance(metadata.get("command"), (str, bytes)):
        missing.append("command")
    log_paths = metadata.get("log_paths")
    if not isinstance(log_paths, Mapping) or not log_paths.get("stdout") or not log_paths.get("stderr"):
        missing.append("log_paths")
    return missing


def _workflow_config_problem(project: Path, message: str) -> dict[str, Any]:
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    return {
        "code": "workflow_config_unavailable",
        "message": message,
        "recoverable": True,
        "path": _path_for_record(project, workflow_path),
        "recovery_actions": [
            "Restore .loopplane/config/workflow.json from a checkpoint or backup.",
            "Run loopplane init --project <project> --brief <brief> if this directory has not been initialized.",
        ],
    }


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _pid_exists(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_json(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(_json_safe(data), indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
