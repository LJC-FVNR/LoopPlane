from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.agent_status import is_success_worker_status, normalize_worker_status
from runtime.exit_codes import EXIT_GENERIC_FAILURE, EXIT_INVALID_CONFIG
from runtime.path_resolution import WorkflowPathError
from runtime.scheduler import (
    ALLOWED_BACKGROUND_JOB_STATUSES,
    BACKGROUND_JOB_SAFE_STATUSES,
    BACKGROUND_JOB_TERMINAL_STATUSES,
    AtomicOwnerLock,
    SCHEMA_VERSION,
    SchedulerLockError,
    load_scheduler_context,
)


BACKGROUND_JOBS_FILENAME = "background_jobs.json"
BACKGROUND_JOB_TTL_SECONDS = 600
BACKGROUND_JOB_PID_STARTUP_GRACE_SECONDS = 15
RECORDED_PROCESS_TERMINATE_TIMEOUT_SECONDS = 1.0
RECORDED_PROCESS_KILL_TIMEOUT_SECONDS = 1.0
DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_WATCHDOG_RECENT_CHECK_LIMIT = 5
SUPERVISOR_SCHEMA_VERSION = "1.0"
BACKGROUND_REGISTRY_LOCK_TTL_SECONDS = 30
BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS = 35.0


def start_background_job(
    project_root: Path | str,
    *,
    command: Sequence[str],
    shell: bool = False,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    job_id: str | None = None,
    label: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    timeout_seconds: int | None = None,
    wake_next_agent_when: str | None = None,
    watchdog_interval_seconds: int | None = None,
    watchdog_runner: str | None = None,
    watchdog_question: str | None = None,
) -> dict[str, Any]:
    command = _normalized_command(command)
    if not command:
        return _failure(
            project=Path(project_root).expanduser(),
            workflow_id=workflow_id,
            status="invalid_command",
            message="A background command is required after `--`.",
            exit_code=EXIT_INVALID_CONFIG,
        )
    loaded = _load_project_paths(project_root, workflow_id=workflow_id)
    if loaded.get("ok") is not True:
        return loaded
    project: Path = loaded["project"]
    paths = loaded["paths"]
    workflow_id = str(loaded["workflow_id"])

    task_id = _non_empty_text(task_id) or _non_empty_text(os.environ.get("LOOPPLANE_TASK_ID"))
    run_id = _non_empty_text(run_id) or _non_empty_text(os.environ.get("LOOPPLANE_RUN_ID"))
    try:
        job_id = _safe_job_id(job_id) if job_id else _new_job_id(task_id=task_id, run_id=run_id)
    except ValueError as error:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="invalid_job_id",
            message=str(error),
            exit_code=EXIT_INVALID_CONFIG,
        )
    job_dir = paths.runtime_dir / "background_jobs" / job_id

    started_at = utc_timestamp()
    supervisor_host = socket.gethostname()
    timeout_at = _timestamp_after(started_at, timeout_seconds) if timeout_seconds and timeout_seconds > 0 else None
    watchdog_interval = _positive_int(watchdog_interval_seconds) or _positive_int(
        os.environ.get("LOOPPLANE_BACKGROUND_WATCHDOG_INTERVAL_SECONDS")
    )
    watchdog_runner = _non_empty_text(watchdog_runner) or _non_empty_text(os.environ.get("LOOPPLANE_BACKGROUND_WATCHDOG_RUNNER"))
    watchdog_question = _non_empty_text(watchdog_question)
    cwd_path = Path(cwd).expanduser() if cwd else project
    if not cwd_path.is_absolute():
        cwd_path = project / cwd_path
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    supervisor_log_path = job_dir / "supervisor.log"
    exit_code_file = job_dir / "exit_code.txt"
    launch_path = job_dir / "launch.json"
    command_display = _command_display(command, shell=shell)
    command_hash = "sha256:" + sha256(command_display.encode("utf-8")).hexdigest()

    launch = {
        "schema_version": SUPERVISOR_SCHEMA_VERSION,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "job_id": job_id,
        "command": list(command),
        "command_display": command_display,
        "shell": bool(shell),
        "cwd": cwd_path.as_posix(),
        "env": dict(env or {}),
        "stdout_path": stdout_path.as_posix(),
        "stderr_path": stderr_path.as_posix(),
        "supervisor_log_path": supervisor_log_path.as_posix(),
        "exit_code_file": exit_code_file.as_posix(),
        "timeout_seconds": timeout_seconds,
        "timeout_at": timeout_at,
        "heartbeat_seconds": DEFAULT_HEARTBEAT_SECONDS,
        "supervisor_host": supervisor_host,
    }
    if watchdog_interval is not None:
        launch.update(
            {
                "watchdog_interval_seconds": watchdog_interval,
                "watchdog_runner": watchdog_runner,
                "watchdog_question": watchdog_question,
            }
        )
    launch = {key: value for key, value in launch.items() if value is not None}

    job = {
        "job_id": job_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "label": _non_empty_text(label),
        "status": "running",
        "next_prompt_ready": False,
        "started_at": started_at,
        "heartbeat_at": started_at,
        "wake_next_agent_when": _non_empty_text(wake_next_agent_when) or "Continue after the LoopPlane-managed background job reaches a safe terminal status.",
        "command": command_display,
        "commands": [command_display],
        "command_hash": command_hash,
        "cwd": _path_for_record(project, cwd_path),
        "logs": [
            _path_for_record(project, stdout_path),
            _path_for_record(project, stderr_path),
            _path_for_record(project, supervisor_log_path),
        ],
        "exit_code_file": _path_for_record(project, exit_code_file),
        "launch_path": _path_for_record(project, launch_path),
        "source": "loopplane_background_start",
        "supervisor_host": supervisor_host,
        "timeout_at": timeout_at,
    }
    if watchdog_interval is not None:
        watchdog_record = {
            "enabled": True,
            "interval_seconds": watchdog_interval,
            "runner_id": watchdog_runner or "inspector",
            "status": "pending",
            "check_count": 0,
            "recent_checks": [],
            "next_check_after": _timestamp_after(started_at, watchdog_interval),
        }
        if watchdog_question:
            watchdog_record["question"] = watchdog_question
        job["watchdog"] = watchdog_record
    job = {key: value for key, value in job.items() if value is not None}
    try:
        duplicate = _upsert_job(paths, workflow_id=workflow_id, job=job, fail_if_exists=True)
    except SchedulerLockError as error:
        return _failure(project=project, workflow_id=workflow_id, status="registry_locked", message=str(error))
    if duplicate is not None:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="duplicate_job_id",
            message=f"Background job {job_id!r} already exists.",
            exit_code=EXIT_INVALID_CONFIG,
            details={"job_id": job_id},
        )
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(launch_path, launch, mode=0o600)
    except OSError as error:
        failure_job = {
            **job,
            "status": "failed",
            "next_prompt_ready": False,
            "ended_at": utc_timestamp(),
            "status_problem": f"launch_write_failed:{type(error).__name__}",
        }
        _upsert_job(paths, workflow_id=workflow_id, job=failure_job)
        return _failure(project=project, workflow_id=workflow_id, status="launch_write_failed", message=str(error))

    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = _with_repo_on_pythonpath(process_env.get("PYTHONPATH"))
    supervisor_command = [
        sys.executable,
        "-m",
        "runtime.background_jobs",
        "supervise",
        "--project",
        project.as_posix(),
        "--workflow",
        workflow_id,
        "--job-id",
        job_id,
        "--launch",
        launch_path.as_posix(),
    ]
    try:
        supervisor = subprocess.Popen(
            supervisor_command,
            cwd=project,
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        failure_job = {
            **job,
            "status": "failed",
            "next_prompt_ready": False,
            "ended_at": utc_timestamp(),
            "status_problem": f"supervisor_start_failed:{type(error).__name__}",
        }
        _upsert_job(paths, workflow_id=workflow_id, job=failure_job)
        return _failure(project=project, workflow_id=workflow_id, status="supervisor_start_failed", message=str(error))

    launched_at = utc_timestamp()
    _reap_process_async(supervisor)
    job.update(
        {
            "supervisor_pid": supervisor.pid,
            "pid": supervisor.pid,
            "heartbeat_at": launched_at,
            "updated_at": launched_at,
        }
    )
    updated_job = _update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        update=lambda existing: _start_supervisor_record_update(existing, job),
    )
    if updated_job is not None:
        job = updated_job
    agent_status_fragment = {
        "status": "running_background",
        "next_prompt_ready": False,
        "wake_next_agent_when": job["wake_next_agent_when"],
        "background_jobs": [_agent_status_job_fragment(job)],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "job_id": job_id,
        "supervisor_pid": supervisor.pid,
        "registry_path": _path_for_record(project, paths.runtime_dir / BACKGROUND_JOBS_FILENAME),
        "job_dir": _path_for_record(project, job_dir),
        "launch_path": _path_for_record(project, launch_path),
        "background_job": job,
        "agent_status_fragment": agent_status_fragment,
        "message": "LoopPlane is supervising the background command and will block unsafe scheduling until it reaches a safe status.",
        "errors": [],
        "warnings": [],
    }


def list_background_jobs(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    job_id: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    loaded = _load_project_paths(project_root, workflow_id=workflow_id)
    if loaded.get("ok") is not True:
        return loaded
    project: Path = loaded["project"]
    paths = loaded["paths"]
    workflow_id = str(loaded["workflow_id"])
    if refresh:
        jobs = _refresh_registry_jobs(project, paths, workflow_id=workflow_id)
    else:
        registry = _read_registry(paths, workflow_id=workflow_id)
        jobs = [dict(job) for job in registry.get("jobs", []) if isinstance(job, Mapping)]
    if job_id:
        jobs = [job for job in jobs if str(job.get("job_id") or "") == job_id]
    unsafe = [
        job
        for job in jobs
        if str(job.get("status") or "running").lower() not in BACKGROUND_JOB_SAFE_STATUSES
        or job.get("next_prompt_ready") is False
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "waiting_background_job" if unsafe else "ready",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "registry_path": _path_for_record(project, paths.runtime_dir / BACKGROUND_JOBS_FILENAME),
        "job_count": len(jobs),
        "unsafe_count": len(unsafe),
        "jobs": jobs,
        "errors": [],
        "warnings": [],
    }


def complete_background_job(
    project_root: Path | str,
    job_id: str,
    *,
    workflow_id: str | None = None,
    status: str = "completed",
    reason: str | None = None,
) -> dict[str, Any]:
    normalized = str(status or "completed").strip().lower()
    if normalized not in {"completed", "failed", "timed_out", "cancelled", "needs_recovery"}:
        return _failure(
            project=Path(project_root).expanduser(),
            workflow_id=workflow_id,
            status="invalid_status",
            message="Manual background status must be completed, failed, timed_out, cancelled, or needs_recovery.",
            exit_code=EXIT_INVALID_CONFIG,
        )
    loaded = _load_project_paths(project_root, workflow_id=workflow_id)
    if loaded.get("ok") is not True:
        return loaded
    project: Path = loaded["project"]
    paths = loaded["paths"]
    workflow_id = str(loaded["workflow_id"])
    existing = _registry_job(paths, workflow_id=workflow_id, job_id=job_id)
    if existing is None:
        return _unknown_job(project, workflow_id, job_id)
    now = utc_timestamp()
    updated = _update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        update=lambda job: {
            **job,
            "status": normalized,
            "next_prompt_ready": normalized in BACKGROUND_JOB_SAFE_STATUSES,
            "ended_at": now,
            "updated_at": now,
            "manual_resolution": True,
            "manual_reason": _non_empty_text(reason),
        },
    )
    if updated is None:
        return _unknown_job(project, workflow_id, job_id)
    killed = _terminate_recorded_processes(updated or existing)
    if killed:
        updated = _update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            update=lambda job: {
                **job,
                "manual_resolution_pids": killed,
                "updated_at": utc_timestamp(),
            },
        ) or updated
    return _job_action_result(project, paths, workflow_id, "updated", updated)


def cancel_background_job(
    project_root: Path | str,
    job_id: str,
    *,
    workflow_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    loaded = _load_project_paths(project_root, workflow_id=workflow_id)
    if loaded.get("ok") is not True:
        return loaded
    project: Path = loaded["project"]
    paths = loaded["paths"]
    workflow_id = str(loaded["workflow_id"])
    existing = _registry_job(paths, workflow_id=workflow_id, job_id=job_id)
    if existing is None:
        return _unknown_job(project, workflow_id, job_id)
    now = utc_timestamp()
    cancelling = _update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        update=lambda job: {
            **job,
            "status": "cancelled",
            "next_prompt_ready": False,
            "updated_at": now,
            "cancelling_at": now,
            "cancelled_at": now,
            "cancel_reason": _non_empty_text(reason),
        },
    )
    target = cancelling or existing
    killed = _terminate_recorded_processes(target)
    updated = _update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        update=lambda job: {
            **job,
            "status": "cancelled",
            "next_prompt_ready": True,
            "ended_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
            "cancelled_pids": killed,
        },
    )
    return _job_action_result(project, paths, workflow_id, "cancelled", updated or existing)


def format_background_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane background: {result.get('status') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    if result.get("job_id"):
        lines.append(f"job_id: {result.get('job_id')}")
    if result.get("registry_path"):
        lines.append(f"registry: {result.get('registry_path')}")
    if result.get("supervisor_pid"):
        lines.append(f"supervisor_pid: {result.get('supervisor_pid')}")
    if result.get("message"):
        lines.append(str(result.get("message")))
    jobs = result.get("jobs")
    if isinstance(jobs, Sequence) and not isinstance(jobs, (str, bytes)):
        lines.append(f"jobs: {len(jobs)}")
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            lines.append(
                "  - "
                f"{job.get('job_id') or 'unknown'}: "
                f"{job.get('status') or 'unknown'} "
                f"next_prompt_ready={str(job.get('next_prompt_ready')).lower()} "
                f"task={job.get('task_id') or '-'}"
            )
            command = job.get("command")
            if command:
                lines.append(f"    command: {command}")
            wake = job.get("wake_next_agent_when")
            if wake:
                lines.append(f"    wake: {wake}")
            watchdog = job.get("watchdog")
            if isinstance(watchdog, Mapping) and watchdog.get("enabled") is True:
                lines.append(
                    "    watchdog: "
                    f"{watchdog.get('status') or 'pending'} "
                    f"interval={watchdog.get('interval_seconds') or '-'}s "
                    f"checks={watchdog.get('check_count') or 0}"
                )
                issue = watchdog.get("last_issue_summary")
                if issue:
                    lines.append(f"    watchdog_issue: {issue}")
    job = result.get("background_job") or result.get("job")
    if isinstance(job, Mapping):
        lines.append(f"job_status: {job.get('status') or 'unknown'}")
        if job.get("command"):
            lines.append(f"command: {job.get('command')}")
        logs = job.get("logs")
        if isinstance(logs, Sequence) and not isinstance(logs, (str, bytes)):
            lines.append("logs:")
            lines.extend(f"  - {log}" for log in logs)
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    fragment = result.get("agent_status_fragment")
    if isinstance(fragment, Mapping):
        lines.append("agent_status_fragment: available in --json output")
    return "\n".join(lines) + "\n"


def background_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return 0
    code = _positive_int(result.get("exit_code"))
    if code is not None:
        return code
    status = str(result.get("status") or "")
    if status in {"invalid_command", "invalid_job_id", "invalid_status", "duplicate_job_id", "unknown_job"}:
        return EXIT_INVALID_CONFIG
    return EXIT_GENERIC_FAILURE


def supervise_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runtime.background_jobs supervise")
    subparsers = parser.add_subparsers(dest="command", required=True)
    supervise = subparsers.add_parser("supervise")
    supervise.add_argument("--project", required=True)
    supervise.add_argument("--workflow", required=True)
    supervise.add_argument("--job-id", required=True)
    supervise.add_argument("--launch", required=True)
    args = parser.parse_args(argv)
    if args.command == "supervise":
        return _run_supervisor(Path(args.project), workflow_id=args.workflow, job_id=args.job_id, launch_path=Path(args.launch))
    return EXIT_INVALID_CONFIG


def _supervisor_update_job(
    paths: Any,
    *,
    workflow_id: str,
    job_id: str,
    supervisor_log: Path,
    operation: str,
    update: Callable[[dict[str, Any]], Mapping[str, Any]],
    durable: bool,
) -> dict[str, Any] | None:
    try:
        return _update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            update=update,
            lock_wait_seconds=BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS if durable else 0.0,
        )
    except SchedulerLockError as error:
        disposition = "retry_exhausted" if durable else "update_deferred"
        _append_log(supervisor_log, f"{operation}_registry_lock_contended {disposition}: {error}")
        return _registry_job(paths, workflow_id=workflow_id, job_id=job_id)


def _run_supervisor(project: Path, *, workflow_id: str, job_id: str, launch_path: Path) -> int:
    loaded = _load_project_paths(project, workflow_id=workflow_id)
    if loaded.get("ok") is not True:
        return EXIT_INVALID_CONFIG
    paths = loaded["paths"]
    workflow_config = loaded.get("workflow_config") if isinstance(loaded.get("workflow_config"), Mapping) else {}
    execution_config = (
        workflow_config.get("execution")
        if isinstance(workflow_config, Mapping) and isinstance(workflow_config.get("execution"), Mapping)
        else {}
    )
    continue_on_fail = execution_config.get("continue_on_fail") is True
    default_job_dir = paths.runtime_dir / "background_jobs" / job_id
    supervisor_log = default_job_dir / "supervisor.log"
    try:
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="launch_read_failure",
            durable=True,
            update=lambda job: _supervisor_terminal_update(
                job,
                {
                    "status": "needs_recovery",
                    "next_prompt_ready": False,
                    "ended_at": utc_timestamp(),
                    "updated_at": utc_timestamp(),
                    "status_problem": f"launch_read_failed:{type(error).__name__}",
                },
            ),
        )
        return EXIT_INVALID_CONFIG
    if not isinstance(launch, Mapping):
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="launch_validation_failure",
            durable=True,
            update=lambda job: _supervisor_terminal_update(
                job,
                {
                    "status": "needs_recovery",
                    "next_prompt_ready": False,
                    "ended_at": utc_timestamp(),
                    "updated_at": utc_timestamp(),
                    "status_problem": "launch_not_object",
                },
            ),
        )
        return EXIT_INVALID_CONFIG
    supervisor_log = Path(str(launch.get("supervisor_log_path") or default_job_dir / "supervisor.log"))
    _append_log(supervisor_log, f"supervisor_started pid={os.getpid()} job_id={job_id}")
    command = _normalized_command(launch.get("command") if isinstance(launch.get("command"), Sequence) else [])
    shell = bool(launch.get("shell"))
    cwd = Path(str(launch.get("cwd") or project))
    stdout_path = Path(str(launch.get("stdout_path") or default_job_dir / "stdout.log"))
    stderr_path = Path(str(launch.get("stderr_path") or default_job_dir / "stderr.log"))
    exit_code_file = Path(str(launch.get("exit_code_file") or default_job_dir / "exit_code.txt"))
    heartbeat_seconds = max(0.5, _float_value(launch.get("heartbeat_seconds"), DEFAULT_HEARTBEAT_SECONDS))
    timeout_seconds = _positive_int(launch.get("timeout_seconds"))
    timeout_at = time.monotonic() + timeout_seconds if timeout_seconds else None
    watchdog_interval = _positive_int(launch.get("watchdog_interval_seconds"))
    next_watchdog_at = time.monotonic() + watchdog_interval if watchdog_interval is not None else None
    env = os.environ.copy()
    launch_env = launch.get("env")
    if isinstance(launch_env, Mapping):
        env.update({str(key): str(value) for key, value in launch_env.items()})
    if not command:
        _append_log(supervisor_log, "launch_command_missing")
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="launch_command_failure",
            durable=True,
            update=lambda job: _supervisor_terminal_update(
                job,
                {
                    "status": "needs_recovery",
                    "next_prompt_ready": False,
                    "ended_at": utc_timestamp(),
                    "updated_at": utc_timestamp(),
                    "status_problem": "launch_command_missing",
                },
            ),
        )
        return EXIT_INVALID_CONFIG
    supervisor_host = socket.gethostname()
    initial_job = _supervisor_update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        supervisor_log=supervisor_log,
        operation="initial_state",
        durable=True,
        update=lambda job: _supervisor_running_update(
            job,
            {
                "supervisor_pid": os.getpid(),
                "supervisor_host": supervisor_host,
                "heartbeat_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
            },
        ),
    )
    if initial_job is None:
        _append_log(supervisor_log, "registry_missing_before_start")
        return EXIT_INVALID_CONFIG
    initial_stop_status = _job_supervisor_stop_status_from_record(initial_job)
    if initial_stop_status is not None:
        _append_log(supervisor_log, f"pre_start_stop_detected status={initial_stop_status}")
        return 0
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    signal_stop: dict[str, int | None] = {"signum": None}

    def handle_stop_signal(signum: int, _frame: Any) -> None:
        signal_stop["signum"] = signum
        if process is not None:
            _terminate_process_group(process.pid)

    previous_sigterm = signal.signal(signal.SIGTERM, handle_stop_signal)
    previous_sigint = signal.signal(signal.SIGINT, handle_stop_signal)
    try:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            pre_spawn_stop_status = _job_supervisor_stop_status(paths, workflow_id=workflow_id, job_id=job_id)
            if pre_spawn_stop_status is not None:
                _append_log(supervisor_log, f"pre_spawn_stop_detected status={pre_spawn_stop_status}")
                return 0
            process = subprocess.Popen(
                _supervisor_popen_command(command, shell=shell),
                shell=shell,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            _append_log(supervisor_log, f"command_started pid={process.pid}")
            started_job = _supervisor_update_job(
                paths,
                workflow_id=workflow_id,
                job_id=job_id,
                supervisor_log=supervisor_log,
                operation="command_start",
                durable=True,
                update=lambda job: _supervisor_running_update(
                    job,
                    {
                        "pid": process.pid,
                        "child_pid": process.pid,
                        "supervisor_pid": os.getpid(),
                        "supervisor_host": supervisor_host,
                        "heartbeat_at": utc_timestamp(),
                        "updated_at": utc_timestamp(),
                    },
                ),
            )
            started_stop_status = _job_supervisor_stop_status_from_record(started_job)
            if started_stop_status is not None:
                _append_log(supervisor_log, f"post_spawn_stop_detected status={started_stop_status}")
                _terminate_popen_process(process)
                return 0
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    break
                if signal_stop.get("signum") is not None:
                    _append_log(supervisor_log, f"stop_signal_detected signum={signal_stop['signum']}")
                    _terminate_popen_process(process)
                    break
                stop_status = _job_supervisor_stop_status(paths, workflow_id=workflow_id, job_id=job_id)
                if stop_status is not None:
                    _append_log(supervisor_log, f"manual_stop_detected status={stop_status}")
                    _terminate_popen_process(process)
                    return 0
                if timeout_at is not None and time.monotonic() >= timeout_at:
                    timed_out = True
                    _append_log(supervisor_log, "timeout_reached")
                    exit_code = _terminate_popen_process(process)
                    break
                if next_watchdog_at is not None and time.monotonic() >= next_watchdog_at:
                    watchdog_result = _run_watchdog_check(
                        project,
                        paths,
                        workflow_id=workflow_id,
                        job_id=job_id,
                        launch=launch,
                        supervisor_log=supervisor_log,
                        process=process,
                    )
                    if process.poll() is not None:
                        break
                    stop_status = _job_supervisor_stop_status(paths, workflow_id=workflow_id, job_id=job_id)
                    if stop_status is not None:
                        _append_log(supervisor_log, f"watchdog_external_stop_detected status={stop_status}")
                        _terminate_popen_process(process)
                        return 0
                    if watchdog_result.get("stop_job") is True:
                        timed_out = str(watchdog_result.get("status") or "") == "timed_out"
                        final_status = str(watchdog_result.get("status") or "needs_recovery")
                        _append_log(supervisor_log, f"watchdog_stopping_job status={final_status}")
                        _terminate_popen_process(process)
                        _write_text_atomic(exit_code_file, f"{process.returncode if process.returncode is not None else -1}\n")
                        final_job = _supervisor_update_job(
                            paths,
                            workflow_id=workflow_id,
                            job_id=job_id,
                            supervisor_log=supervisor_log,
                            operation="watchdog_terminal",
                            durable=True,
                            update=lambda job: _supervisor_terminal_update(
                                job,
                                _background_terminal_update(
                                    final_status=final_status,
                                    exit_code=process.returncode if process.returncode is not None else -1,
                                    continue_on_fail=continue_on_fail,
                                    status_problem=watchdog_result.get("status_problem") or f"watchdog:{final_status}",
                                ),
                            ),
                        )
                        preserved_status = _job_supervisor_stop_status_from_record(final_job)
                        if preserved_status is not None and preserved_status != final_status:
                            _append_log(supervisor_log, f"watchdog_terminal_update_preserved status={preserved_status}")
                        return 0
                    next_watchdog_at = time.monotonic() + watchdog_interval
                heartbeat_job = _supervisor_update_job(
                    paths,
                    workflow_id=workflow_id,
                    job_id=job_id,
                    supervisor_log=supervisor_log,
                    operation="heartbeat",
                    durable=False,
                    update=lambda job: _supervisor_running_update(
                        job,
                        {
                            "pid": process.pid,
                            "child_pid": process.pid,
                            "supervisor_pid": os.getpid(),
                            "supervisor_host": supervisor_host,
                            "heartbeat_at": utc_timestamp(),
                            "updated_at": utc_timestamp(),
                        },
                    ),
                )
                heartbeat_stop_status = _job_supervisor_stop_status_from_record(heartbeat_job)
                if heartbeat_stop_status is not None:
                    _append_log(supervisor_log, f"heartbeat_stop_detected status={heartbeat_stop_status}")
                    _terminate_popen_process(process)
                    return 0
                wait_seconds = heartbeat_seconds
                if next_watchdog_at is not None:
                    wait_seconds = max(0.1, min(wait_seconds, next_watchdog_at - time.monotonic()))
                if timeout_at is not None:
                    wait_seconds = max(0.1, min(wait_seconds, timeout_at - time.monotonic()))
                try:
                    process.wait(timeout=wait_seconds)
                except subprocess.TimeoutExpired:
                    pass
    except BaseException as error:
        _append_log(supervisor_log, f"supervisor_error {type(error).__name__}: {error}")
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="supervisor_failure",
            durable=True,
            update=lambda job: _supervisor_terminal_update(
                job,
                {
                    "status": "failed",
                    "next_prompt_ready": False,
                    "ended_at": utc_timestamp(),
                    "updated_at": utc_timestamp(),
                    "status_problem": f"supervisor_error:{type(error).__name__}",
                },
            ),
        )
        return EXIT_GENERIC_FAILURE
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    exit_code = int(process.returncode if process is not None and process.returncode is not None else 1)
    _write_text_atomic(exit_code_file, f"{exit_code}\n")
    stop_status = _job_supervisor_stop_status(paths, workflow_id=workflow_id, job_id=job_id)
    if stop_status is not None:
        _append_log(supervisor_log, f"command_resolved_externally status={stop_status} exit_code={exit_code}")
        return 0
    if signal_stop.get("signum") is not None:
        _append_log(supervisor_log, f"command_stopped_by_signal signum={signal_stop['signum']} exit_code={exit_code}")
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="signal_terminal",
            durable=True,
            update=lambda job: {
                **job,
                "status": "cancelled",
                "next_prompt_ready": False,
                "exit_code": exit_code,
                "ended_at": utc_timestamp(),
                "updated_at": utc_timestamp(),
                "status_problem": f"supervisor_signal:{signal_stop['signum']}",
            },
        )
        return 0
    final_status = "timed_out" if timed_out else ("completed" if exit_code == 0 else "failed")
    _append_log(supervisor_log, f"command_finished status={final_status} exit_code={exit_code}")
    final_job = _supervisor_update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        supervisor_log=supervisor_log,
        operation="command_terminal",
        durable=True,
        update=lambda job: _supervisor_terminal_update(
            job,
            _background_terminal_update(
                final_status=final_status,
                exit_code=exit_code,
                continue_on_fail=continue_on_fail,
            ),
        ),
    )
    preserved_status = _job_supervisor_stop_status_from_record(final_job)
    if preserved_status is not None and preserved_status != final_status:
        _append_log(supervisor_log, f"final_update_preserved status={preserved_status} exit_code={exit_code}")
    return 0


def _background_terminal_update(
    *,
    final_status: str,
    exit_code: int,
    continue_on_fail: bool,
    status_problem: object | None = None,
) -> dict[str, Any]:
    now = utc_timestamp()
    update: dict[str, Any] = {
        "status": final_status,
        "next_prompt_ready": final_status in BACKGROUND_JOB_SAFE_STATUSES,
        "exit_code": exit_code,
        "ended_at": now,
        "updated_at": now,
        "heartbeat_at": now,
    }
    if status_problem is not None:
        update["status_problem"] = status_problem
    if continue_on_fail and final_status in {"failed", "timed_out", "needs_recovery"}:
        update.update(
            {
                "status": "cancelled",
                "next_prompt_ready": True,
                "status_problem": None,
                "original_terminal_status": final_status,
                "original_status_problem": status_problem,
                "auto_resolved_for_continue_on_fail": True,
                "auto_resolution_reason": (
                    "The background command's terminal failure remains recorded by original_terminal_status, "
                    "exit_code, logs, and watchdog evidence. execution.continue_on_fail=true releases the "
                    "scheduler so the pending task can be retried or redesigned; this is not acceptance."
                ),
            }
        )
    return update


def _run_watchdog_check(
    project: Path,
    paths: Any,
    *,
    workflow_id: str,
    job_id: str,
    launch: Mapping[str, Any],
    supervisor_log: Path,
    process: subprocess.Popen[bytes],
) -> dict[str, Any]:
    from runtime.inspector import answer_inspection

    check_id = f"watchdog_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    started_at = utc_timestamp()
    runner_id = _non_empty_text(launch.get("watchdog_runner")) or "inspector"
    _append_log(supervisor_log, f"watchdog_started check_id={check_id} runner={runner_id}")
    _supervisor_update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        supervisor_log=supervisor_log,
        operation="watchdog_start",
        durable=False,
        update=lambda job: _job_with_watchdog_update(
            job,
            {
                "status": "running",
                "current_check_id": check_id,
                "current_check_started_at": started_at,
                "runner_id": runner_id,
            },
        ),
    )
    try:
        result = answer_inspection(
            project,
            _watchdog_question(
                project,
                paths,
                workflow_id=workflow_id,
                job_id=job_id,
                launch=launch,
                process=process,
            ),
            runner_id=runner_id,
            allowed_paths=_watchdog_allowed_paths(project, paths, job_id=job_id),
            source="background_watchdog",
        )
    except BaseException as error:
        ended_at = utc_timestamp()
        record = {
            "check_id": check_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": "inspector_error",
            "runner_id": runner_id,
            "error": f"{type(error).__name__}: {error}",
        }
        _append_log(supervisor_log, f"watchdog_error check_id={check_id} error={type(error).__name__}: {error}")
        _supervisor_update_job(
            paths,
            workflow_id=workflow_id,
            job_id=job_id,
            supervisor_log=supervisor_log,
            operation="watchdog_error",
            durable=False,
            update=lambda job: _job_with_watchdog_update(job, _watchdog_record_update(job, record, "inspector_error")),
        )
        return {"stop_job": False, "status": "running"}

    verdict = _watchdog_verdict(result)
    ended_at = utc_timestamp()
    record = {
        "check_id": check_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": "ok" if result.get("ok") else str(result.get("status") or "inspector_failed"),
        "runner_id": runner_id,
        "inspector_run_id": _watchdog_inspector_run_id(result),
        "answer": _bounded_text(result.get("answer"), limit=500),
        "recommended_status": verdict.get("recommended_status"),
        "healthy_progress": verdict.get("healthy_progress"),
        "issue_summary": _bounded_text(verdict.get("issue_summary"), limit=500),
        "repair_actions_taken": verdict.get("repair_actions_taken"),
    }
    _append_log(
        supervisor_log,
        "watchdog_completed "
        f"check_id={check_id} "
        f"status={record['status']} "
        f"recommended_status={record.get('recommended_status') or '-'} "
        f"healthy_progress={record.get('healthy_progress')}",
    )
    _supervisor_update_job(
        paths,
        workflow_id=workflow_id,
        job_id=job_id,
        supervisor_log=supervisor_log,
        operation="watchdog_complete",
        durable=False,
        update=lambda job: _job_with_watchdog_update(
            job,
            _watchdog_record_update(
                job,
                record,
                "requires_attention" if verdict.get("stop_job") else record["status"],
            ),
        ),
    )
    if verdict.get("stop_job"):
        status = str(verdict.get("recommended_status") or "needs_recovery")
        return {
            "stop_job": True,
            "status": status,
            "status_problem": _bounded_text(verdict.get("issue_summary"), limit=300) or f"watchdog:{status}",
        }
    return {"stop_job": False, "status": "running"}


def _watchdog_question(
    project: Path,
    paths: Any,
    *,
    workflow_id: str,
    job_id: str,
    launch: Mapping[str, Any],
    process: subprocess.Popen[bytes],
) -> str:
    job = _registry_job(paths, workflow_id=workflow_id, job_id=job_id) or {}
    operator_question = _non_empty_text(launch.get("watchdog_question"))
    command = str(launch.get("command_display") or job.get("command") or "")
    job_payload = {
        "workflow_id": workflow_id,
        "job_id": job_id,
        "task_id": job.get("task_id"),
        "run_id": job.get("run_id"),
        "label": job.get("label"),
        "status": job.get("status"),
        "command": command,
        "cwd": job.get("cwd") or launch.get("cwd"),
        "pid": process.pid,
        "supervisor_pid": os.getpid(),
        "started_at": job.get("started_at"),
        "heartbeat_at": job.get("heartbeat_at"),
        "timeout_at": job.get("timeout_at") or launch.get("timeout_at"),
        "logs": job.get("logs"),
        "launch_path": job.get("launch_path"),
        "exit_code_file": job.get("exit_code_file"),
    }
    question = operator_question or (
        "Inspect whether this LoopPlane-managed background job is still healthy, making meaningful progress, "
        "and aligned with its intended command."
    )
    return (
        "LoopPlane background job watchdog check.\n\n"
        f"Operator question: {question}\n\n"
        "Job metadata:\n"
        f"{json.dumps(job_payload, indent=2, sort_keys=True)}\n\n"
        "Inspect the launch metadata, stdout/stderr logs, supervisor log, exit-code file if present, and any task artifacts "
        "needed to decide whether the job is healthy. If you can safely repair a local project/artifact issue, do so. "
        "Do not edit LoopPlane runtime authority files such as background_jobs.json; the supervisor consumes your response.\n\n"
        "Write a structured inspection_response.json with this shape, in addition to a concise human answer:\n"
        "{\n"
        '  "answer": "...",\n'
        '  "background_watchdog": {\n'
        f'    "job_id": "{job_id}",\n'
        '    "healthy_progress": true,\n'
        '    "recommended_status": "running",\n'
        '    "issue_summary": "",\n'
        '    "repair_actions_taken": [],\n'
        '    "follow_up_needed": "",\n'
        '    "confidence": "medium"\n'
        "  }\n"
        "}\n\n"
        "Use recommended_status=running when the job should continue. Use needs_recovery, failed, timed_out, or cancelled "
        "only when the supervisor should stop treating the job as healthy. Do not recommend completed while the process is still alive."
    )


def _watchdog_allowed_paths(project: Path, paths: Any, *, job_id: str) -> list[str]:
    refs = [
        paths.value("plan_file"),
        paths.value("shared_context_file"),
        paths.value("read_models_dir").rstrip("/") + "/",
        paths.value("results_dir").rstrip("/") + "/",
        paths.value("runtime_dir").rstrip("/") + "/state.json",
        paths.value("runtime_dir").rstrip("/") + f"/background_jobs/{job_id}/",
    ]
    return sorted(dict.fromkeys(ref for ref in refs if ref))


def _watchdog_verdict(result: Mapping[str, Any]) -> dict[str, Any]:
    details = _watchdog_details(result)
    recommended_status = _normalized_watchdog_status(details.get("recommended_status") or details.get("status"))
    healthy_progress = _optional_bool(details.get("healthy_progress"))
    if recommended_status is None:
        recommended_status = "running"
    if recommended_status == "completed":
        recommended_status = "running"
    if healthy_progress is False and recommended_status == "running":
        recommended_status = "needs_recovery"
    stop_statuses = {"needs_recovery", "failed", "timed_out", "cancelled"}
    return {
        "stop_job": recommended_status in stop_statuses and result.get("ok") is True,
        "recommended_status": recommended_status,
        "healthy_progress": healthy_progress,
        "issue_summary": details.get("issue_summary") or details.get("summary") or result.get("answer"),
        "repair_actions_taken": _list_strings(details.get("repair_actions_taken")),
    }


def _watchdog_details(result: Mapping[str, Any]) -> Mapping[str, Any]:
    response = result.get("response")
    response_details = response.get("details") if isinstance(response, Mapping) else None
    if isinstance(response_details, Mapping):
        watchdog = response_details.get("background_watchdog")
        if isinstance(watchdog, Mapping):
            return watchdog
        return response_details
    return {}


def _normalized_watchdog_status(value: object) -> str | None:
    text = _non_empty_text(value)
    if text is None:
        return None
    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in ALLOWED_BACKGROUND_JOB_STATUSES else None


def _watchdog_inspector_run_id(result: Mapping[str, Any]) -> str | None:
    response = result.get("response")
    run = response.get("agent_run") if isinstance(response, Mapping) else None
    return _non_empty_text(run.get("run_id")) if isinstance(run, Mapping) else None


def _watchdog_record_update(job: Mapping[str, Any], record: Mapping[str, Any], status: str) -> dict[str, Any]:
    watchdog = dict(job.get("watchdog") if isinstance(job.get("watchdog"), Mapping) else {})
    interval = _positive_int(watchdog.get("interval_seconds"))
    update = {
        "enabled": watchdog.get("enabled") is not False,
        "status": status,
        "last_checked_at": record.get("ended_at"),
        "last_check_id": record.get("check_id"),
        "last_recommended_status": record.get("recommended_status"),
        "last_healthy_progress": record.get("healthy_progress"),
        "last_issue_summary": record.get("issue_summary"),
        "current_check_id": None,
        "current_check_started_at": None,
        "check_count": int(watchdog.get("check_count") or 0) + 1,
        "recent_checks": _recent_watchdog_checks([*(_sequence_records(watchdog.get("recent_checks"))), dict(record)]),
    }
    if interval is not None:
        update["next_check_after"] = _timestamp_after(str(record.get("ended_at") or utc_timestamp()), interval)
    return update


def _job_with_watchdog_update(job: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    watchdog = dict(job.get("watchdog") if isinstance(job.get("watchdog"), Mapping) else {})
    watchdog.update(dict(updates))
    return {**dict(job), "watchdog": watchdog, "updated_at": utc_timestamp()}


def _recent_watchdog_checks(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(check) for check in checks if isinstance(check, Mapping)][-DEFAULT_WATCHDOG_RECENT_CHECK_LIMIT:]


def _sequence_records(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "healthy", "ok", "pass"}:
        return True
    if text in {"false", "no", "0", "unhealthy", "fail", "failed"}:
        return False
    return None


def _list_strings(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value if str(item).strip()]
    text = _non_empty_text(value)
    return [text] if text else []


def _bounded_text(value: object, *, limit: int) -> str | None:
    text = _non_empty_text(value)
    if text is None:
        return None
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _load_project_paths(project_root: Path | str, *, workflow_id: str | None = None) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    try:
        context_result = load_scheduler_context(project, workflow_id=workflow_id)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(project=project, workflow_id=workflow_id, status="workflow_config_unavailable", message=str(error))
    if context_result.get("ok") is not True:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="workflow_config_unavailable",
            message=str(context_result.get("message") or "Unable to load workflow configuration."),
            exit_code=int(context_result.get("exit_code") or EXIT_INVALID_CONFIG),
        )
    context = context_result["context"]
    return {
        "ok": True,
        "project": context.project,
        "paths": context.paths,
        "workflow_id": context.workflow_id,
        "workflow_config": dict(context.workflow_config),
    }


def _read_registry(paths: Any, *, workflow_id: str) -> dict[str, Any]:
    path = paths.runtime_dir / BACKGROUND_JOBS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}
    if isinstance(data, list):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": [item for item in data if isinstance(item, Mapping)]}
    if not isinstance(data, Mapping):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
    registry = dict(data)
    registry["schema_version"] = str(registry.get("schema_version") or SCHEMA_VERSION)
    registry["workflow_id"] = str(registry.get("workflow_id") or workflow_id)
    registry["jobs"] = [dict(item) for item in jobs if isinstance(item, Mapping)]
    return registry


def _refresh_registry_jobs(project: Path, paths: Any, *, workflow_id: str) -> list[dict[str, Any]]:
    refreshed: list[dict[str, Any]] = []

    def update(registry: dict[str, Any]) -> None:
        nonlocal refreshed
        jobs = registry.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        refreshed = [_refresh_job(project, paths, job) for job in jobs if isinstance(job, Mapping)]
        registry["jobs"] = [dict(job) for job in refreshed]

    _update_registry_locked(paths, workflow_id=workflow_id, update=update)
    return refreshed


def _update_registry_locked(
    paths: Any,
    *,
    workflow_id: str,
    update: Callable[[dict[str, Any]], Any],
    lock_wait_seconds: float = BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS,
) -> Any:
    owner = f"background-cli:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(
        paths.runtime_dir / "lock" / "background_jobs_lock",
        owner,
        ttl_seconds=BACKGROUND_REGISTRY_LOCK_TTL_SECONDS,
    )
    with lock.acquire(timeout_seconds=max(0.0, lock_wait_seconds)):
        registry = _read_registry(paths, workflow_id=workflow_id)
        result = update(registry)
        registry["schema_version"] = SCHEMA_VERSION
        registry["workflow_id"] = workflow_id
        registry["updated_at"] = utc_timestamp()
        _write_json_atomic(paths.runtime_dir / BACKGROUND_JOBS_FILENAME, registry)
        return result


def _upsert_job(
    paths: Any,
    *,
    workflow_id: str,
    job: Mapping[str, Any],
    fail_if_exists: bool = False,
) -> Mapping[str, Any] | None:
    job_id = str(job.get("job_id") or "")

    def update(registry: dict[str, Any]) -> Mapping[str, Any] | None:
        jobs = registry.setdefault("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
            registry["jobs"] = jobs
        for index, existing in enumerate(jobs):
            if isinstance(existing, Mapping) and str(existing.get("job_id") or "") == job_id:
                if fail_if_exists:
                    return dict(existing)
                merged = {**dict(existing), **dict(job)}
                jobs[index] = merged
                return None
        jobs.append(dict(job))
        return None

    return _update_registry_locked(paths, workflow_id=workflow_id, update=update)


def _update_job(
    paths: Any,
    *,
    workflow_id: str,
    job_id: str,
    update: Callable[[dict[str, Any]], Mapping[str, Any]],
    lock_wait_seconds: float = BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS,
) -> dict[str, Any] | None:
    updated: dict[str, Any] | None = None

    def mutate(registry: dict[str, Any]) -> None:
        nonlocal updated
        jobs = registry.setdefault("jobs", [])
        if not isinstance(jobs, list):
            registry["jobs"] = []
            return
        for index, item in enumerate(jobs):
            if isinstance(item, Mapping) and str(item.get("job_id") or "") == job_id:
                next_job = dict(update(dict(item)))
                next_job["job_id"] = job_id
                jobs[index] = next_job
                updated = next_job
                return

    _update_registry_locked(
        paths,
        workflow_id=workflow_id,
        update=mutate,
        lock_wait_seconds=lock_wait_seconds,
    )
    return updated


def _registry_job(paths: Any, *, workflow_id: str, job_id: str) -> dict[str, Any] | None:
    registry = _read_registry(paths, workflow_id=workflow_id)
    for job in registry.get("jobs", []):
        if isinstance(job, Mapping) and str(job.get("job_id") or "") == job_id:
            return dict(job)
    return None


def _refresh_job(project: Path, paths: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    refreshed = dict(job)
    status = str(refreshed.get("status") or "").strip().lower()
    if status not in ALLOWED_BACKGROUND_JOB_STATUSES:
        refreshed["status"] = "needs_recovery"
        refreshed["next_prompt_ready"] = False
        refreshed["status_problem"] = f"invalid_status:{status or '<missing>'}"
        return refreshed
    source_status = _refresh_from_source_agent_status(project, refreshed)
    if source_status is not None:
        return source_status
    if status == "needs_recovery":
        refreshed["next_prompt_ready"] = False
        return refreshed
    exit_code_path = _resolve_job_path(project, refreshed.get("exit_code_file"))
    if status not in BACKGROUND_JOB_TERMINAL_STATUSES and exit_code_path is not None and exit_code_path.is_file():
        try:
            exit_code_text = exit_code_path.read_text(encoding="utf-8").strip()
        except OSError:
            return refreshed
        if not exit_code_text:
            return refreshed
        try:
            exit_code = int(exit_code_text)
        except ValueError:
            refreshed["status"] = "needs_recovery"
            refreshed["next_prompt_ready"] = False
            return refreshed
        refreshed["exit_code"] = exit_code
        refreshed["status"] = "completed" if exit_code == 0 else "failed"
        refreshed["next_prompt_ready"] = exit_code == 0
        refreshed.setdefault("ended_at", utc_timestamp())
        return refreshed
    if status in BACKGROUND_JOB_SAFE_STATUSES:
        refreshed["next_prompt_ready"] = refreshed.get("next_prompt_ready") is not False
        return refreshed
    if status in {"failed", "timed_out", "stale"}:
        refreshed["next_prompt_ready"] = False
        return refreshed
    if status in {"pending", "running"}:
        heartbeat = _parse_timestamp(refreshed.get("heartbeat_at") or refreshed.get("started_at"))
        age: int | None = None
        if heartbeat is None:
            refreshed["status"] = "needs_recovery"
            refreshed["status_problem"] = "missing_parseable_heartbeat"
            refreshed["next_prompt_ready"] = False
            return refreshed
        age = max(0, int((datetime.now(UTC) - heartbeat).total_seconds()))
        refreshed["heartbeat_age_seconds"] = age
        if age > BACKGROUND_JOB_TTL_SECONDS:
            child_pid = _positive_int(refreshed.get("pid") or refreshed.get("child_pid"))
            supervisor_pid = _positive_int(refreshed.get("supervisor_pid"))
            if (
                _job_pid_probe_may_confirm_liveness(refreshed)
                and
                child_pid is not None
                and supervisor_pid is not None
                and _pid_exists(child_pid)
                and _pid_exists(supervisor_pid)
            ):
                # Both independently recorded process levels are live.  This
                # covers synchronous watchdog inspections (which cannot emit a
                # heartbeat while the inspector runs) and a registry snapshot
                # race which may momentarily hide current_check_id.  Preserve
                # the workload instead of converting healthy compute to stale.
                refreshed["next_prompt_ready"] = False
                if refreshed.get("status_problem") == "stale_heartbeat":
                    refreshed.pop("status_problem", None)
                return refreshed
            refreshed["status"] = "stale"
            refreshed["status_problem"] = "stale_heartbeat"
            refreshed["next_prompt_ready"] = False
            return refreshed
        pid = _positive_int(refreshed.get("pid"))
        if (
            pid is not None
            and _job_supervisor_is_local(refreshed)
            and _pid_exists(pid) is False
        ):
            if _pid_missing_is_within_startup_grace(refreshed, pid=pid, age_seconds=age):
                refreshed["next_prompt_ready"] = False
                return refreshed
            refreshed["status"] = "stale"
            refreshed["status_problem"] = "process_not_live"
            refreshed["next_prompt_ready"] = False
            return refreshed
        if refreshed.get("status_problem") in {"stale_heartbeat", "process_not_live", "missing_parseable_heartbeat"}:
            refreshed.pop("status_problem", None)
        refreshed["next_prompt_ready"] = False
    return refreshed


def _refresh_from_source_agent_status(project: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    if job.get("manual_resolution") is True:
        return None
    if str(job.get("source") or "") == "loopplane_background_start":
        return None
    status_path = _resolve_job_path(project, job.get("source_agent_status_path"))
    if status_path is None or not status_path.is_file():
        return None
    try:
        status_record = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(status_record, Mapping):
        return None
    run_id = _non_empty_text(job.get("run_id"))
    source_run_id = _non_empty_text(status_record.get("run_id"))
    if run_id is not None and source_run_id is not None and run_id != source_run_id:
        return None
    task_id = _non_empty_text(job.get("task_id"))
    source_task_id = _non_empty_text(status_record.get("task_id") or status_record.get("primary_task_id"))
    if task_id is not None and source_task_id is not None and task_id != source_task_id:
        return None
    worker_status = normalize_worker_status(status_record.get("status")) or str(status_record.get("status") or "").strip().lower()
    if worker_status == "running_background":
        return None
    if _agent_next_prompt_ready(status_record) is True or is_success_worker_status(worker_status):
        refreshed = dict(job)
        refreshed["status"] = "completed"
        refreshed["next_prompt_ready"] = True
        refreshed["ended_at"] = _non_empty_text(status_record.get("ended_at")) or refreshed.get("ended_at") or utc_timestamp()
        refreshed["updated_at"] = utc_timestamp()
        refreshed["resolved_from_source_agent_status"] = True
        refreshed.pop("status_problem", None)
        return refreshed
    return None


def _agent_next_prompt_ready(agent_status_record: Mapping[str, Any]) -> bool | None:
    value = agent_status_record.get("next_prompt_ready")
    if isinstance(value, bool):
        return value
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        nested = background.get("next_prompt_ready")
        if isinstance(nested, bool):
            return nested
    background_state = agent_status_record.get("background_state")
    if isinstance(background_state, Mapping):
        nested = background_state.get("next_prompt_ready")
        if isinstance(nested, bool):
            return nested
    return None


def _agent_status_job_fragment(job: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "job_id",
        "task_id",
        "run_id",
        "status",
        "next_prompt_ready",
        "wake_next_agent_when",
        "pid",
        "supervisor_pid",
        "supervisor_host",
        "logs",
        "exit_code_file",
        "command",
        "timeout_at",
        "watchdog",
    )
    return {field: job[field] for field in fields if field in job and job[field] is not None}


def _job_action_result(project: Path, paths: Any, workflow_id: str, status: str, job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "job_id": job.get("job_id"),
        "registry_path": _path_for_record(project, paths.runtime_dir / BACKGROUND_JOBS_FILENAME),
        "job": dict(job),
        "errors": [],
        "warnings": [],
    }


def _unknown_job(project: Path, workflow_id: str | None, job_id: str) -> dict[str, Any]:
    return _failure(
        project=project,
        workflow_id=workflow_id,
        status="unknown_job",
        message=f"Background job {job_id!r} was not found.",
        exit_code=EXIT_INVALID_CONFIG,
        details={"job_id": job_id},
    )


def _failure(
    *,
    project: Path,
    workflow_id: str | None,
    status: str,
    message: str,
    exit_code: int = EXIT_GENERIC_FAILURE,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "message": message,
        "errors": [message],
        "warnings": [],
        "exit_code": exit_code,
    }
    if details:
        payload.update(dict(details))
    return payload


def _normalized_command(command: object) -> list[str]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        return []
    values = [str(item) for item in command if str(item) != ""]
    if values and values[0] == "--":
        values = values[1:]
    return values


def _supervisor_popen_command(command: Sequence[str], *, shell: bool) -> str | list[str]:
    if shell:
        return str(command[0]) if len(command) == 1 else shlex.join(str(part) for part in command)
    return list(command)


def _command_display(command: Sequence[str], *, shell: bool) -> str:
    if shell:
        return str(command[0]) if len(command) == 1 else shlex.join(str(part) for part in command)
    return shlex.join(str(part) for part in command)


def _new_job_id(*, task_id: str | None, run_id: str | None) -> str:
    prefix_parts = ["bg"]
    if task_id:
        prefix_parts.append(_safe_identifier(task_id))
    if run_id:
        prefix_parts.append(_safe_identifier(run_id)[-24:])
    prefix_parts.append(datetime.now(UTC).strftime("%Y%m%d_%H%M%S"))
    prefix_parts.append(uuid.uuid4().hex[:8])
    return "_".join(part for part in prefix_parts if part)


def _safe_job_id(value: str | None) -> str:
    text = _non_empty_text(value) or ""
    cleaned = _safe_identifier(text)
    if not cleaned:
        raise ValueError("job_id must contain at least one alphanumeric character")
    return cleaned


def _safe_identifier(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in str(value))
    return cleaned.strip("_")


def _non_empty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_value(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _timestamp_after(started_at: str, seconds: int | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    parsed = _parse_timestamp(started_at) or datetime.now(UTC)
    return (parsed + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _resolve_job_path(project: Path, value: object) -> Path | None:
    text = _non_empty_text(value)
    if text is None:
        return None
    path = Path(text)
    return path if path.is_absolute() else project / path


def _write_json_atomic(path: Path, data: Mapping[str, Any], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_text_atomic(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _append_log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_timestamp()} {line}\n")
    except OSError:
        pass


def _with_repo_on_pythonpath(existing: str | None) -> str:
    repo_root = Path(__file__).resolve().parents[1].as_posix()
    if not existing:
        return repo_root
    parts = existing.split(os.pathsep)
    if repo_root in parts:
        return existing
    return os.pathsep.join([repo_root, existing])


def _reap_process_async(process: subprocess.Popen[Any]) -> None:
    def wait_for_exit() -> None:
        try:
            process.wait()
        except BaseException:
            pass

    threading.Thread(target=wait_for_exit, name=f"loopplane-bg-reap-{process.pid}", daemon=True).start()


def _pid_exists(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _pid_is_zombie(pid: int) -> bool:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        state = text.rsplit(") ", 1)[1].split()[0]
    except IndexError:
        return False
    return state == "Z"


def _pid_is_active(pid: int) -> bool | None:
    exists = _pid_exists(pid)
    if exists is not True:
        return exists
    return not _pid_is_zombie(pid)


def _job_supervisor_is_local(job: Mapping[str, Any]) -> bool:
    """Whether local PID probes are authoritative for this background job.

    Shared workflow state is routinely inspected from login, dashboard, and
    controller nodes whose PID namespaces are independent.  A missing local
    PID is evidence only on the host that launched the supervisor.  Legacy
    records without host provenance therefore rely on their heartbeat and
    exit-code evidence instead of being falsely marked stale.
    """

    supervisor_host = _non_empty_text(job.get("supervisor_host"))
    return supervisor_host is not None and _hostnames_match(supervisor_host, socket.gethostname())


def _job_pid_probe_may_confirm_liveness(job: Mapping[str, Any]) -> bool:
    """Whether positive local PID evidence may preserve a running record.

    PID values are meaningful only in the launching host's namespace. Legacy
    records without host provenance therefore cannot use either positive or
    negative PID evidence.
    """

    return _job_supervisor_is_local(job)


def _hostnames_match(left: str, right: str) -> bool:
    left_normalized = left.strip().lower().rstrip(".")
    right_normalized = right.strip().lower().rstrip(".")
    if not left_normalized or not right_normalized:
        return False
    return left_normalized == right_normalized or left_normalized.split(".", 1)[0] == right_normalized.split(".", 1)[0]


def _terminate_process_group(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


def _force_kill_process_group(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def _terminate_popen_process(process: subprocess.Popen[Any], *, timeout_seconds: float = 10.0) -> int:
    if process.poll() is not None:
        return int(process.returncode if process.returncode is not None else 0)
    _terminate_process_group(process.pid)
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _force_kill_process_group(process.pid)
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
    return int(process.returncode if process.returncode is not None else -1)


def _terminate_recorded_processes(job: Mapping[str, Any]) -> list[int]:
    # PID namespaces are node-local. Acting on PIDs recorded by a supervisor
    # on another host can signal unrelated processes on this host.
    if not _job_supervisor_is_local(job):
        return []
    killed: list[int] = []
    for key in ("child_pid", "pid", "supervisor_pid"):
        pid = _positive_int(job.get(key))
        if pid is not None and pid not in killed:
            if _terminate_recorded_pid(pid):
                killed.append(pid)
    return killed


def _terminate_recorded_pid(pid: int) -> bool:
    if not _terminate_process_group(pid):
        return False
    if _wait_for_pid_exit(pid, timeout_seconds=RECORDED_PROCESS_TERMINATE_TIMEOUT_SECONDS):
        return True
    _force_kill_process_group(pid)
    _wait_for_pid_exit(pid, timeout_seconds=RECORDED_PROCESS_KILL_TIMEOUT_SECONDS)
    return True


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        active = _pid_is_active(pid)
        if active is False:
            return True
        if time.monotonic() >= deadline:
            return active is not True
        time.sleep(0.05)


def _job_supervisor_stop_status(paths: Any, *, workflow_id: str, job_id: str) -> str | None:
    job = _registry_job(paths, workflow_id=workflow_id, job_id=job_id)
    return _job_supervisor_stop_status_from_record(job)


def _job_supervisor_stop_status_from_record(job: Mapping[str, Any] | None) -> str | None:
    if not isinstance(job, Mapping):
        return None
    status = str(job.get("status") or "").strip().lower()
    if status in {"cancelled", "needs_recovery"}:
        return status
    if job.get("manual_resolution") is True and status in ALLOWED_BACKGROUND_JOB_STATUSES:
        return status
    return None


def _pid_missing_is_within_startup_grace(job: Mapping[str, Any], *, pid: int, age_seconds: int | None) -> bool:
    child_pid = _positive_int(job.get("child_pid"))
    supervisor_pid = _positive_int(job.get("supervisor_pid"))
    if child_pid is not None:
        return False
    if supervisor_pid is None or supervisor_pid != pid:
        return False
    if age_seconds is None:
        return False
    return age_seconds <= BACKGROUND_JOB_PID_STARTUP_GRACE_SECONDS


def _start_supervisor_record_update(existing: Mapping[str, Any], start_job: Mapping[str, Any]) -> dict[str, Any]:
    current = dict(existing)
    status = str(current.get("status") or "").strip().lower()
    if status in BACKGROUND_JOB_TERMINAL_STATUSES.union({"needs_recovery", "stale"}) or current.get("manual_resolution") is True:
        return current

    update = dict(start_job)
    update.pop("pid", None)
    merged = {**current, **update}
    child_pid = _positive_int(current.get("child_pid"))
    current_pid = _positive_int(current.get("pid"))
    if child_pid is not None:
        merged["child_pid"] = child_pid
        merged["pid"] = current_pid or child_pid
    else:
        start_pid = _positive_int(start_job.get("pid"))
        if start_pid is not None:
            merged["pid"] = start_pid
    return merged


def _supervisor_running_update(job: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    if _job_supervisor_stop_status_from_record(job) is not None:
        return dict(job)
    return {
        **dict(job),
        "status": "running",
        "next_prompt_ready": False,
        **dict(updates),
    }


def _supervisor_terminal_update(job: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    if _job_supervisor_stop_status_from_record(job) is not None:
        return dict(job)
    return {**dict(job), **dict(updates)}


if __name__ == "__main__":
    raise SystemExit(supervise_main())
