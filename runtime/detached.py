from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
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
from runtime.scheduler import EXIT_DUPLICATE_SCHEDULER, SchedulerLockError, run_scheduler, scheduler_exit_code
from runtime.validation import run_validator, validation_exit_code


SCHEMA_VERSION = "1.5"
SUPERVISOR_FILENAME = "supervisor.json"
SUPERVISOR_LOG_DIRNAME = "supervisor"
SUPERVISOR_STDOUT_FILENAME = "supervisor_stdout.log"
SUPERVISOR_STDERR_FILENAME = "supervisor_stderr.log"
SUPERVISOR_HEARTBEAT_TTL_SECONDS = 120
SUPERVISOR_METADATA_LOCK = threading.RLock()


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return max(0.05, float(raw_value))
    except ValueError:
        return default


DEFAULT_POLL_INTERVAL_SECONDS = _positive_float_from_env("LOOPPLANE_SUPERVISOR_POLL_INTERVAL_SECONDS", 0.2)
SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS = _positive_float_from_env(
    "LOOPPLANE_SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS",
    SUPERVISOR_HEARTBEAT_TTL_SECONDS / 4,
)
BACKGROUND_WAIT_INTERVAL_SECONDS = _positive_float_from_env("LOOPPLANE_BACKGROUND_WAIT_INTERVAL_SECONDS", 60.0)
DUPLICATE_SCHEDULER_STARTUP_GRACE_SECONDS = _positive_float_from_env(
    "LOOPPLANE_DUPLICATE_SCHEDULER_STARTUP_GRACE_SECONDS",
    150.0,
)
DUPLICATE_SCHEDULER_RETRY_INTERVAL_SECONDS = 5.0
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
        "waiting_runner_availability",
    }
)
RECOVERABLE_WAIT_ACTIONS = frozenset(
    {"wait_paused", "wait_config", "wait_approval", "wait_runner_availability"}
)
RECOVERABLE_WAIT_INTERVAL_SECONDS = _positive_float_from_env(
    "LOOPPLANE_RECOVERABLE_WAIT_INTERVAL_SECONDS", 30.0
)
RUNNER_AVAILABILITY_WAIT_INTERVAL_SECONDS = _positive_float_from_env(
    "LOOPPLANE_RUNNER_AVAILABILITY_WAIT_INTERVAL_SECONDS", 30.0
)
ACTION_FAILURE_BACKOFF_BASE_SECONDS = _positive_float_from_env(
    "LOOPPLANE_ACTION_FAILURE_BACKOFF_BASE_SECONDS", 2.0
)
ACTION_FAILURE_BACKOFF_MAX_SECONDS = _positive_float_from_env(
    "LOOPPLANE_ACTION_FAILURE_BACKOFF_MAX_SECONDS", 60.0
)
ACTION_FAILURE_MAX_REPEATS = max(
    1,
    int(_positive_float_from_env("LOOPPLANE_ACTION_FAILURE_MAX_REPEATS", 3.0)),
)
SUPERVISOR_CIRCUIT_BREAKER_ACTIONS = frozenset(
    {"run_expansion_planner", "run_phase_objective_verifier", "run_final_objective_verifier"}
)


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


def resume_detached_scheduler(
    project_root: Path | str,
    *,
    clear_runner_availability_holds: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        context = _load_supervisor_context(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(project, f"Unable to load workflow configuration: {error}", started_at=started_at)
    paths = context["paths"]

    payload = {"clear_runner_availability_holds": True} if clear_runner_availability_holds else None
    control_result = record_control_request(project, "resume", source="cli", payload=payload)
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
            "host": socket.gethostname(),
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
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_supervisor_heartbeat_loop,
        args=(paths,),
        kwargs={
            "owner": owner,
            "stop_event": heartbeat_stop,
            "interval_seconds": SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS,
        },
        name="loopplane-supervisor-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    previous_action_failure_signature: str | None = None
    consecutive_action_failures = 0
    action_failure_repeat_limit = _action_failure_repeat_limit(context["workflow"])
    startup_duplicate_first_seen: float | None = None
    successful_scheduler_tick = False
    try:
        while True:
            _heartbeat(paths, owner=owner, status="running")
            try:
                result = run_scheduler(project, max_ticks=1)
            except SchedulerLockError as error:
                # Event, failure-registry, and other short-lived authority locks
                # can legitimately overlap with background watchdog/inspector
                # writes.  A detached supervisor must retry this transient
                # contention instead of dying and leaving a completed
                # background job with no scheduler to consume it.
                _merge_supervisor_metadata(
                    paths,
                    {
                        "status": "running",
                        "updated_at": utc_timestamp(),
                        "heartbeat_at": utc_timestamp(),
                        "last_action": "scheduler_lock_contention_retry",
                        "last_loop_reason": "transient_scheduler_lock_contention",
                        "last_lock_error": str(error),
                    },
                )
                time.sleep(poll_interval)
                continue
            if scheduler_exit_code(result) == EXIT_DUPLICATE_SCHEDULER and not successful_scheduler_tick:
                now = time.monotonic()
                startup_duplicate_first_seen = (
                    startup_duplicate_first_seen if startup_duplicate_first_seen is not None else now
                )
                elapsed = max(0.0, now - startup_duplicate_first_seen)
                if elapsed <= DUPLICATE_SCHEDULER_STARTUP_GRACE_SECONDS:
                    _merge_supervisor_metadata(
                        paths,
                        {
                            "status": "running",
                            "updated_at": utc_timestamp(),
                            "heartbeat_at": utc_timestamp(),
                            "last_action": "duplicate_scheduler_startup_retry",
                            "last_loop_reason": "duplicate_scheduler_startup_grace",
                            "last_scheduler_result": _compact_scheduler_result(result),
                            "duplicate_scheduler_retry_elapsed_seconds": elapsed,
                            "duplicate_scheduler_retry_grace_seconds": DUPLICATE_SCHEDULER_STARTUP_GRACE_SECONDS,
                        },
                    )
                    time.sleep(DUPLICATE_SCHEDULER_RETRY_INTERVAL_SECONDS)
                    continue
            successful_scheduler_tick = True
            selected = result.get("selected_action") if isinstance(result.get("selected_action"), Mapping) else {}
            action = str(selected.get("action") or "")
            follow_up = _complete_worker_follow_up(project, selected)
            should_continue, reason, loop_exit_code = _should_continue_after_tick(result, selected, follow_up)
            action_failure_signature = _action_failure_signature(result, selected)
            action_failure_backoff_seconds: float | None = None
            effective_action_failure_repeat_limit = action_failure_repeat_limit
            if _deterministic_infrastructure_failure(selected):
                effective_action_failure_repeat_limit = 1
            if action_failure_signature is None:
                previous_action_failure_signature = None
                consecutive_action_failures = 0
            else:
                if action_failure_signature == previous_action_failure_signature:
                    consecutive_action_failures += 1
                else:
                    previous_action_failure_signature = action_failure_signature
                    consecutive_action_failures = 1
                if consecutive_action_failures >= effective_action_failure_repeat_limit:
                    should_continue = False
                    reason = "repeated_action_failure"
                    loop_exit_code = EXIT_GENERIC_FAILURE
                else:
                    should_continue = True
                    reason = "action_failure_backoff"
                    loop_exit_code = EXIT_SUCCESS
                    action_failure_backoff_seconds = _action_failure_backoff_seconds(
                        consecutive_action_failures
                    )
            supervisor_update = {
                "status": _supervisor_status_after_tick(action, should_continue=should_continue, reason=reason),
                "updated_at": utc_timestamp(),
                "heartbeat_at": utc_timestamp(),
                "last_scheduler_result": _compact_scheduler_result(result),
                "last_action": action,
                "last_exit_code": scheduler_exit_code(result),
                "last_loop_reason": reason,
                "action_failure_signature": action_failure_signature,
                "consecutive_action_failures": consecutive_action_failures,
                "action_failure_repeat_limit": effective_action_failure_repeat_limit,
                "action_failure_backoff_seconds": action_failure_backoff_seconds,
            }
            if follow_up is not None:
                supervisor_update["last_follow_up"] = follow_up
            _merge_supervisor_metadata(paths, supervisor_update)

            if not should_continue:
                exit_code = loop_exit_code
                exit_status = _exit_status_for_reason(reason)
                stop_reason = reason
                break

            wait_seconds = _poll_interval_after_tick(
                action,
                poll_interval,
                failure_backoff_seconds=action_failure_backoff_seconds,
            )
            if action in RECOVERABLE_WAIT_ACTIONS or action == "wait_background_job":
                _wait_for_supervisor_wakeup(
                    paths,
                    action=action,
                    timeout_seconds=wait_seconds,
                    stat_poll_seconds=poll_interval,
                )
            else:
                time.sleep(wait_seconds)
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
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(1.0, SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS * 2))
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
    metadata_status = str(metadata.get("status") or "unknown")
    metadata_heartbeat_stale = _heartbeat_is_stale(metadata.get("heartbeat_at"))
    active_run_lease_id = _fresh_supervisor_owned_active_run_lease(paths, supervisor_pid=pid)
    heartbeat_covered_by_active_run_lease = bool(
        metadata_heartbeat_stale and active_run_lease_id
    )
    heartbeat_stale = metadata_heartbeat_stale and not heartbeat_covered_by_active_run_lease
    active_or_unknown = metadata_status in ACTIVE_SUPERVISOR_STATUSES or metadata_status == "unknown"
    supervisor_host = _supervisor_host(metadata)
    host_is_local = _host_is_local(supervisor_host)
    pid_probe_scope = "remote" if host_is_local is False else ("local" if host_is_local is True else "unknown")
    if host_is_local is False:
        alive = True if active_or_unknown and not heartbeat_stale else None
        liveness_source = "heartbeat" if alive is True else None
    else:
        alive = _pid_exists(pid) if pid is not None else None
        liveness_source = "pid" if alive is not None else None
    liveness = "alive" if alive is True else ("dead" if alive is False else "unknown")
    status = metadata_status
    warnings: list[str] = []
    status_problems: list[str] = []
    missing_fields = _missing_supervisor_metadata_fields(metadata)
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
        "supervisor_host": supervisor_host,
        "pid_probe_scope": pid_probe_scope,
        "liveness": liveness,
        "liveness_source": liveness_source,
        "heartbeat_stale": heartbeat_stale,
        "metadata_heartbeat_stale": metadata_heartbeat_stale,
        "heartbeat_covered_by_active_run_lease": heartbeat_covered_by_active_run_lease,
        "active_run_lease_id": active_run_lease_id,
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


def _action_failure_signature(result: Mapping[str, Any], selected: Mapping[str, Any]) -> str | None:
    action = str(selected.get("action") or "")
    if action not in SUPERVISOR_CIRCUIT_BREAKER_ACTIONS:
        return None
    execution = selected.get("execution_result")
    execution_mapping = execution if isinstance(execution, Mapping) else {}
    failed = (
        result.get("ok") is False
        or selected.get("ok") is False
        or execution_mapping.get("ok") is False
    )
    if not failed:
        return None
    return "|".join(
        (
            action,
            str(execution_mapping.get("runner_id") or ""),
            str(execution_mapping.get("status") or result.get("status") or ""),
            str(execution_mapping.get("classification") or ""),
            str(execution_mapping.get("adapter_exit_code") or result.get("exit_code") or ""),
            str(bool(execution_mapping.get("adapter_timed_out"))),
        )
    )


def _deterministic_infrastructure_failure(selected: Mapping[str, Any]) -> bool:
    execution = selected.get("execution_result")
    if not isinstance(execution, Mapping) or execution.get("ok") is not False:
        return False
    status = str(execution.get("status") or "").strip().lower()
    error_code = str(execution.get("error_code") or "").strip().lower()
    classification = str(execution.get("classification") or "").strip().lower()
    try:
        adapter_exit_code = int(execution.get("adapter_exit_code"))
    except (TypeError, ValueError):
        adapter_exit_code = None
    availability = execution.get("runner_availability")
    availability_mapping = availability if isinstance(availability, Mapping) else {}
    reason_class = str(availability_mapping.get("reason_class") or "").strip().lower()
    requires_attention = availability_mapping.get("requires_attention") is True
    return bool(
        status == "waiting_config"
        or "runner_unavailable" in error_code
        or classification in {"runner_configuration_error", "command_unavailable"}
        or adapter_exit_code == 127
        or reason_class in {
            "auth_required",
            "billing_required",
            "credits_exhausted",
            "runner_configuration_error",
        }
        or requires_attention
    )


def _action_failure_repeat_limit(workflow: Mapping[str, Any]) -> int:
    policy = workflow.get("self_expansion")
    configured = policy.get("max_repeated_signature_count") if isinstance(policy, Mapping) else None
    # ``max_repeated_signature_count`` bounds semantic plan expansion, where a
    # relatively generous budget can be useful.  Repeating the same failed
    # planner/verifier process is an infrastructure failure and must have a
    # much tighter independent ceiling.  Respect a stricter workflow limit,
    # but never inherit a looser one (the generated default is currently 100).
    workflow_limit = _positive_int(configured)
    if workflow_limit is None:
        return ACTION_FAILURE_MAX_REPEATS
    return min(workflow_limit, ACTION_FAILURE_MAX_REPEATS)


def _action_failure_backoff_seconds(
    consecutive_failures: int,
    *,
    base_seconds: float = ACTION_FAILURE_BACKOFF_BASE_SECONDS,
    max_seconds: float = ACTION_FAILURE_BACKOFF_MAX_SECONDS,
) -> float:
    if consecutive_failures <= 0:
        return 0.0
    return min(max(0.05, float(max_seconds)), max(0.05, float(base_seconds)) * (2 ** (consecutive_failures - 1)))


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
        "resolve_expansion_failure",
    }:
        return True, "continue", EXIT_SUCCESS
    if action in {
        "run_expansion_planner",
        "run_phase_objective_verifier",
        "run_final_objective_verifier",
    }:
        execution = selected.get("execution_result")
        if isinstance(execution, Mapping) and execution.get("ok") is False:
            failure_update = execution.get("failure_registry_update")
            retry_pending = (
                isinstance(failure_update, Mapping)
                and bool(failure_update.get("budget_remaining"))
                and str(failure_update.get("status") or "")
                not in {"recovered", "waived", "exhausted", "needs_human"}
            )
            if retry_pending:
                return True, "action_failure_retry_pending", EXIT_SUCCESS
            return False, "action_failure_exhausted", exit_code
        if not result.get("ok"):
            return False, "scheduler_failed", exit_code
        return True, "continue", EXIT_SUCCESS
    if action == "run_final_verification":
        execution = selected.get("execution_result")
        if isinstance(execution, Mapping) and (execution.get("pass") is True or execution.get("status") == "pass"):
            return True, "final_verification_passed", EXIT_SUCCESS
        if isinstance(execution, Mapping) and isinstance(execution.get("runner_availability"), Mapping):
            return True, "runner_availability_wait", EXIT_SUCCESS
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
        details = blocker.get("details") if isinstance(blocker.get("details"), Mapping) else {}
        if str(details.get("recommended_action") or "").strip().lower() == "self_expand":
            return True
    return False


def _supervisor_status_after_tick(action: str, *, should_continue: bool, reason: str) -> str:
    if not should_continue:
        return _exit_status_for_reason(reason)
    if reason == "runner_availability_wait":
        return "waiting_runner_availability"
    return {
        "wait_background_job": "waiting_background",
        "wait_paused": "paused",
        "wait_config": "waiting_config",
        "wait_approval": "waiting_approval",
        "wait_runner_availability": "waiting_runner_availability",
    }.get(action, "running")


def _poll_interval_after_tick(
    action: str,
    poll_interval: float,
    *,
    failure_backoff_seconds: float | None = None,
) -> float:
    if failure_backoff_seconds is not None:
        return max(poll_interval, failure_backoff_seconds)
    if action == "wait_background_job":
        return BACKGROUND_WAIT_INTERVAL_SECONDS
    if action == "wait_runner_availability":
        return max(poll_interval, RUNNER_AVAILABILITY_WAIT_INTERVAL_SECONDS)
    if action in RECOVERABLE_WAIT_ACTIONS:
        return max(poll_interval, RECOVERABLE_WAIT_INTERVAL_SECONDS)
    return poll_interval


def _wait_for_supervisor_wakeup(
    paths: WorkflowPaths,
    *,
    action: str,
    timeout_seconds: float,
    stat_poll_seconds: float,
) -> None:
    """Wait cheaply until an authoritative input changes or a timer expires.

    The supervisor heartbeat has its own thread, so a waiting workflow does not
    need to rebuild a full scheduler snapshot every second.  Portable stat
    polling keeps control requests responsive without adding an inotify-only
    dependency or writing any wait-state files.
    """

    watched = _supervisor_wakeup_paths(paths, action=action)
    before = _path_stat_signature(watched)
    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    interval = min(1.0, max(0.05, float(stat_poll_seconds)))
    wake = threading.Event()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        wake.wait(min(interval, remaining))
        if _path_stat_signature(watched) != before:
            return


def _supervisor_wakeup_paths(paths: WorkflowPaths, *, action: str) -> tuple[Path, ...]:
    common = (
        paths.runtime_dir / "control_requests.jsonl",
        paths.runtime_dir / "state.json",
    )
    if action == "wait_approval":
        return (*common, paths.runtime_dir / "approval_responses.jsonl", paths.runtime_dir / "human_approval_responses.jsonl")
    if action == "wait_background_job":
        return (*common, paths.runtime_dir / "background_jobs.json", paths.runtime_dir / "active_run_leases")
    if action == "wait_runner_availability":
        return (
            *common,
            paths.runtime_dir / "runner_health.json",
            paths.config_file("agent_runners.json"),
            paths.workflow_config_dir / "local",
        )
    if action == "wait_config":
        return (*common, paths.workflow_config_dir, paths.plan_file)
    return common


def _path_stat_signature(paths: Sequence[Path]) -> tuple[tuple[str, bool, int, int], ...]:
    signature: list[tuple[str, bool, int, int]] = []
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            signature.append((path.as_posix(), False, 0, 0))
            continue
        signature.append(
            (
                path.as_posix(),
                True,
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
            )
        )
    return tuple(signature)


def _exit_status_for_reason(reason: str) -> str:
    return {
        "complete": "completed",
        "wait_paused": "paused",
        "wait_stopped": "stopped",
        "wait_config": "waiting_config",
        "wait_approval": "waiting_approval",
        "requires_attention": "requires_attention",
        "repeated_action_failure": "requires_attention",
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
    # A terminal supervisor may deliberately exit after surfacing an incident.
    # Once an operator or autonomous repair explicitly issues ``resume``, a
    # dead requires-attention/failed/exited supervisor must be launchable so it
    # can consume that acknowledgement and continue recovery.  Otherwise the
    # durable resume request has no process capable of applying it.
    if supervisor_status in {"requires_attention", "failed", "exited"}:
        return f"{supervisor_status}_supervisor"
    if runtime_status in {"requires_attention", "failed"}:
        return f"{runtime_status}_runtime"
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


def _heartbeat(paths: WorkflowPaths, *, owner: str, status: str | None) -> None:
    updates: dict[str, Any] = {
        "owner": owner,
        "updated_at": utc_timestamp(),
        "heartbeat_at": utc_timestamp(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }
    if status is not None:
        updates["status"] = status
    _merge_supervisor_metadata(paths, updates)


def _supervisor_heartbeat_loop(
    paths: WorkflowPaths,
    *,
    owner: str,
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    interval = max(0.05, float(interval_seconds))
    while not stop_event.wait(interval):
        _heartbeat(paths, owner=owner, status=None)


def _existing_started_at(paths: WorkflowPaths) -> str | None:
    metadata = _read_json(_supervisor_metadata_path(paths), default={})
    if isinstance(metadata, Mapping) and isinstance(metadata.get("started_at"), str):
        return str(metadata["started_at"])
    return None


def _merge_supervisor_metadata(paths: WorkflowPaths, updates: Mapping[str, Any]) -> dict[str, Any]:
    with SUPERVISOR_METADATA_LOCK:
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


def _fresh_supervisor_owned_active_run_lease(
    paths: WorkflowPaths,
    *,
    supervisor_pid: int | None,
) -> str | None:
    if supervisor_pid is None:
        return None
    lease_dir = paths.runtime_dir / "active_run_leases"
    if not lease_dir.is_dir():
        return None
    for lease_path in sorted(lease_dir.glob("*.json"), reverse=True):
        lease = _read_json(lease_path, default={})
        if not isinstance(lease, Mapping):
            continue
        if str(lease.get("status") or "").lower() != "running":
            continue
        owner_pids = {
            _positive_int(lease.get("adapter_pid")),
            _positive_int(lease.get("scheduler_pid")),
        }
        if supervisor_pid not in owner_pids:
            continue
        if _heartbeat_is_stale(lease.get("heartbeat_at")):
            continue
        run_id = str(lease.get("run_id") or lease_path.stem).strip()
        return run_id or lease_path.stem
    return None


def _missing_supervisor_metadata_fields(metadata: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if _positive_int(metadata.get("pid")) is None:
        missing.append("pid")
    if not isinstance(metadata.get("heartbeat_at"), str) or not str(metadata.get("heartbeat_at") or "").strip():
        missing.append("heartbeat_at")
    return missing


def _supervisor_host(metadata: Mapping[str, Any]) -> str | None:
    explicit = str(metadata.get("host") or "").strip()
    if explicit:
        return explicit
    owner = str(metadata.get("owner") or "").strip()
    parts = owner.rsplit(":", 2)
    if len(parts) == 3 and parts[0].strip():
        return parts[0].strip()
    return None


def _host_is_local(host: str | None) -> bool | None:
    if not host:
        return None

    def aliases(value: str) -> set[str]:
        normalized = value.strip().lower().rstrip(".")
        if not normalized:
            return set()
        return {normalized, normalized.split(".", 1)[0]}

    local_aliases: set[str] = set()
    for value in (socket.gethostname(), socket.getfqdn()):
        local_aliases.update(aliases(value))
    return bool(local_aliases.intersection(aliases(host)))


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
