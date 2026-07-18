from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.agent_status import CANONICAL_WORKER_STATUSES, normalize_worker_status
from runtime.agent_runners import AgentRunnerConfigError, load_agent_runners
from runtime.exit_codes import EXIT_HEALTH_FAILURE, EXIT_SUCCESS
from runtime.path_resolution import WorkflowPaths
from runtime.runner_locks import (
    RUNNER_LOCK_ACTIVE,
    RUNNER_LOCK_ABSENT,
    RUNNER_LOCK_MALFORMED,
    RUNNER_LOCK_STALE,
    RUNNER_LOCK_UNKNOWN,
    inspect_runner_lock,
    machine_resource_policy_from_runner,
)
from runtime.scheduler import SCHEMA_VERSION, completion_marker_status, load_scheduler_context
from runtime.schema_validation import validate_project_schemas
from runtime.version_control import SubprocessGitCommandRunner, run_git_doctor


HEALTHY = "healthy"
HEALTHY_WITH_WARNINGS = "healthy_with_warnings"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"

PASS = "pass"
WARN = "warn"
FAIL = "fail"

DEFAULT_HEARTBEAT_TTL_SECONDS = 120
BACKGROUND_JOB_TTL_SECONDS = 600
RECENT_FILE_LIMIT = 20
ALLOWED_BACKGROUND_JOB_STATUSES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "stale",
        "needs_recovery",
    }
)
BACKGROUND_JOB_SAFE_STATUSES = frozenset({"completed", "cancelled"})
BACKGROUND_JOB_PROBLEM_STATUSES = frozenset({"failed", "timed_out", "stale", "needs_recovery"})

INACTIVE_RUN_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "aborted", "released"})
ACTIVE_RUN_STATUSES = frozenset({"running", "pending", "starting", "waiting"})
WORKER_STATUSES = CANONICAL_WORKER_STATUSES
RESOLVED_FAILURE_STATUSES = frozenset({"recovered", "waived"})
VALIDATION_STATUSES = frozenset({"pass", "pass_with_warnings", "fail", "blocked", "needs_human"})
READ_MODEL_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "run_summaries.jsonl",
    "dashboard_feed.jsonl",
    "metrics.json",
    "version_control_status.json",
)


def run_health_probe(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    strict: bool = False,
    write: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    checked_at = utc_timestamp()
    context_result = load_scheduler_context(project, workflow_id=workflow_id)

    if not context_result["ok"]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "checked_at": checked_at,
            "project_root": project.as_posix(),
            "strict": strict,
            "status": UNHEALTHY,
            "ok": False,
            "checks": [
                _check(
                    "workflow_configuration",
                    FAIL,
                    str(context_result["message"]),
                    severity=UNHEALTHY,
                )
            ],
            "requires_attention": [],
        }
        result["requires_attention"] = _requires_attention(result["checks"])
        return result

    context = context_result["context"]
    paths: WorkflowPaths = context.paths
    now = _parse_timestamp(checked_at) or datetime.now(UTC)

    checks: list[dict[str, Any]] = []
    checks.append(_check_schema_validation(project))
    checks.append(_check_scheduler_lock(paths, now))
    leases_check, active_leases = _check_active_run_leases(paths, now)
    checks.append(leases_check)
    checks.append(_check_runner_liveness(active_leases))
    checks.append(_check_machine_runner_locks(project, now))
    checks.append(_check_background_jobs(paths, now))
    checks.append(_check_agent_status_files(paths))
    checks.append(_check_validation_files(paths))
    checks.append(_check_completion_marker(paths))
    checks.append(_check_failure_registry(paths))
    checks.append(_check_expansion_registry(paths))
    checks.append(_check_git_checkpoints(project, paths))
    checks.append(_check_event_segments(paths))
    checks.append(_check_read_models(paths))

    status = _overall_status(checks)
    result = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": context.workflow_id,
        "checked_at": checked_at,
        "project_root": project.as_posix(),
        "strict": strict,
        "status": status,
        "ok": status in {HEALTHY, HEALTHY_WITH_WARNINGS} and not (strict and status == HEALTHY_WITH_WARNINGS),
        "checks": checks,
        "requires_attention": _requires_attention(checks),
    }
    if write:
        health_path = paths.runtime_dir / "health_report.json"
        result["health_report_path"] = paths.value("runtime_dir") + "/health_report.json"
        _write_json(health_path, result)
    return result


def health_exit_code(result: Mapping[str, Any]) -> int:
    status = str(result.get("status") or "")
    if status == HEALTHY:
        return EXIT_SUCCESS
    if status == HEALTHY_WITH_WARNINGS and not bool(result.get("strict")):
        return EXIT_SUCCESS
    return EXIT_HEALTH_FAILURE


def format_health_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane health: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"strict: {str(bool(result.get('strict'))).lower()}",
    ]
    report_path = result.get("health_report_path")
    if report_path:
        lines.append(f"health_report_path: {report_path}")

    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        lines.append("checks:")
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            lines.append(f"  - [{check.get('status', 'unknown')}] {check.get('name', 'unknown')}: {check.get('message', '')}")

    attention = result.get("requires_attention")
    if isinstance(attention, Sequence) and attention and not isinstance(attention, (str, bytes)):
        lines.append("requires_attention:")
        for item in attention:
            if isinstance(item, Mapping):
                lines.append(f"  - {item.get('name', 'unknown')}: {item.get('message', '')}")
    return "\n".join(lines) + "\n"


def _check_schema_validation(project: Path) -> dict[str, Any]:
    result = validate_project_schemas(project)
    details = {
        "checked_files": result.get("checked_files", []),
        "schemas_used": result.get("schemas_used", []),
        "errors": result.get("errors", []),
        "warnings": result.get("warnings", []),
        "schema_dir": result.get("schema_dir"),
    }
    if result.get("ok"):
        checked = result.get("checked_files")
        count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else None
        return _check("schema_validation", PASS, "Required JSON files match registered schemas.", count=count, details=details)
    errors = result.get("errors")
    if _only_read_model_schema_errors(errors):
        return _check(
            "schema_validation",
            WARN,
            "Derived read model JSON failed schema validation but can be rebuilt from authoritative runtime files. Run `loopplane rebuild-read-models --project <project>`.",
            details=details,
        )
    return _check(
        "schema_validation",
        FAIL,
        "Required JSON files failed schema validation.",
        severity=UNHEALTHY,
        details=details,
    )


def _only_read_model_schema_errors(errors: Any) -> bool:
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)) or not errors:
        return False
    return all(_is_read_model_schema_error(str(error)) for error in errors)


def _is_read_model_schema_error(error: str) -> bool:
    location = error.split(":", 1)[0].strip().replace("\\", "/")
    if not location:
        return False
    if "/read_models/" in location:
        return True
    return location.startswith(".loopplane/read_models/")


def _check_scheduler_lock(paths: WorkflowPaths, now: datetime) -> dict[str, Any]:
    owner_path = paths.runtime_dir / "lock" / "scheduler_instance_lock" / "owner.json"
    if not owner_path.exists():
        return _check("scheduler_lock", PASS, "Scheduler lock is safely absent.", path=_relative(paths, owner_path))

    owner, error = _read_json_object(owner_path)
    if error:
        return _check("scheduler_lock", FAIL, f"Scheduler lock owner is malformed: {error}.", path=_relative(paths, owner_path))

    heartbeat = _parse_timestamp(owner.get("heartbeat_at") or owner.get("started_at"))
    if heartbeat is None:
        return _check("scheduler_lock", FAIL, "Scheduler lock owner is missing a parseable heartbeat_at.", path=_relative(paths, owner_path))
    ttl = _positive_int(owner.get("ttl_seconds"), DEFAULT_HEARTBEAT_TTL_SECONDS)
    age = _age_seconds(now, heartbeat)
    if age <= ttl:
        return _check(
            "scheduler_lock",
            PASS,
            "Scheduler lock heartbeat is fresh.",
            path=_relative(paths, owner_path),
            details={"owner": owner.get("owner"), "age_seconds": age, "ttl_seconds": ttl},
        )
    owner_pid = _positive_int(owner.get("pid"), 0)
    covering_lease = _fresh_scheduler_lock_owner_lease(paths, now=now, owner_pid=owner_pid)
    if owner_pid > 0 and _pid_exists(owner_pid) is True and covering_lease is not None:
        return _check(
            "scheduler_lock",
            PASS,
            "Scheduler lock metadata heartbeat is covered by a fresh owner-held active run lease.",
            path=_relative(paths, owner_path),
            details={
                "owner": owner.get("owner"),
                "owner_pid": owner_pid,
                "age_seconds": age,
                "ttl_seconds": ttl,
                "metadata_heartbeat_stale": True,
                "heartbeat_covered_by_active_run_lease": True,
                "active_run_lease": covering_lease,
            },
        )
    return _check(
        "scheduler_lock",
        FAIL,
        "Scheduler lock heartbeat is stale.",
        path=_relative(paths, owner_path),
        details={"owner": owner.get("owner"), "age_seconds": age, "ttl_seconds": ttl},
    )


def _fresh_scheduler_lock_owner_lease(
    paths: WorkflowPaths,
    *,
    now: datetime,
    owner_pid: int,
) -> dict[str, Any] | None:
    if owner_pid <= 0:
        return None
    lease_dir = paths.runtime_dir / "active_run_leases"
    if not lease_dir.is_dir():
        return None
    for lease_path in sorted(lease_dir.glob("*.json"), reverse=True):
        lease, error = _read_json_object(lease_path)
        if error or not isinstance(lease, Mapping):
            continue
        if str(lease.get("status") or "").lower() not in ACTIVE_RUN_STATUSES:
            continue
        lease_owner_pids = {
            _positive_int(lease.get("adapter_pid"), 0),
            _positive_int(lease.get("scheduler_pid"), 0),
        }
        if owner_pid not in lease_owner_pids:
            continue
        heartbeat = _parse_timestamp(lease.get("heartbeat_at") or lease.get("prepared_at"))
        ttl = _positive_int(lease.get("lease_ttl_seconds"), DEFAULT_HEARTBEAT_TTL_SECONDS)
        if heartbeat is None or _age_seconds(now, heartbeat) > ttl:
            continue
        return {
            "run_id": lease.get("run_id") or lease_path.stem,
            "path": _relative(paths, lease_path),
            "heartbeat_at": lease.get("heartbeat_at"),
            "ttl_seconds": ttl,
        }
    return None


def _check_active_run_leases(paths: WorkflowPaths, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lease_dir = paths.runtime_dir / "active_run_leases"
    if not lease_dir.exists():
        return _check("active_run_leases", WARN, "Active run lease directory is missing but can be recreated.", path=_relative(paths, lease_dir)), []

    lease_files = sorted(path for path in lease_dir.glob("*.json") if path.is_file())
    if not lease_files:
        return _check("active_run_leases", PASS, "No active run leases are present.", count=0), []

    problems: list[str] = []
    active: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    stale: list[str] = []
    for lease_file in lease_files:
        lease, error = _read_json_object(lease_file)
        rel = _relative(paths, lease_file)
        if error:
            problems.append(f"{rel}: {error}")
            continue
        run_id = _non_empty_string(lease.get("run_id"))
        status = str(lease.get("status") or "running").lower()
        if not _lease_blocks_scheduler(lease):
            external.append(
                {
                    "path": rel,
                    "run_id": run_id,
                    "role": lease.get("role"),
                    "status": status,
                    "blocks_scheduler": False,
                }
            )
            continue
        heartbeat = _parse_timestamp(lease.get("heartbeat_at"))
        expires = _parse_timestamp(lease.get("lease_expires_at"))
        if run_id is None:
            problems.append(f"{rel}: missing run_id")
        if status not in ACTIVE_RUN_STATUSES and status not in INACTIVE_RUN_STATUSES:
            problems.append(f"{rel}: unknown status {status!r}")
        if status in INACTIVE_RUN_STATUSES:
            continue
        if heartbeat is None and expires is None:
            problems.append(f"{rel}: missing parseable heartbeat_at or lease_expires_at")
            continue
        is_fresh = (expires is not None and expires >= now) or (
            heartbeat is not None and _age_seconds(now, heartbeat) <= DEFAULT_HEARTBEAT_TTL_SECONDS
        )
        lease_summary = {
            "path": rel,
            "run_id": run_id,
            "status": status,
            "adapter_pid": lease.get("adapter_pid"),
            "fresh": is_fresh,
        }
        active.append(lease_summary)
        if not is_fresh:
            stale.append(rel)

    details = {"active": active, "external_nonblocking": external}
    if problems:
        return _check("active_run_leases", FAIL, "One or more active run leases are malformed.", details={**details, "problems": problems}), active
    if stale:
        return _check("active_run_leases", FAIL, "One or more active run leases are stale.", details={**details, "stale": stale}), active
    if active:
        return _check("active_run_leases", PASS, f"{len(active)} active run lease(s) are fresh.", details=details), active
    if external:
        return _check("active_run_leases", PASS, "Only non-blocking external run leases are present.", details=details), active
    return _check("active_run_leases", PASS, "No active workflow-blocking run leases are present.", details=details), active


def _check_runner_liveness(active_leases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not active_leases:
        return _check("runner_liveness", PASS, "No active runner processes are expected.")

    missing_pid: list[str] = []
    dead_pid: list[str] = []
    alive_pid: list[str] = []
    for lease in active_leases:
        run_id = str(lease.get("run_id") or lease.get("path") or "unknown")
        pid_value = lease.get("adapter_pid")
        if pid_value is None:
            missing_pid.append(run_id)
            continue
        pid = _positive_int(pid_value, -1)
        if pid <= 0:
            dead_pid.append(run_id)
            continue
        alive = _pid_exists(pid)
        if alive is None:
            missing_pid.append(run_id)
        elif alive:
            alive_pid.append(run_id)
        else:
            dead_pid.append(run_id)

    if dead_pid:
        return _check("runner_liveness", FAIL, "Active run lease refers to a non-live runner process.", details={"dead": dead_pid})
    if missing_pid:
        return _check(
            "runner_liveness",
            WARN,
            "Process liveness is unavailable for one or more active run leases.",
            details={"missing_pid": missing_pid, "alive": alive_pid},
        )
    return _check("runner_liveness", PASS, "Active runner process liveness is consistent with leases.", details={"alive": alive_pid})


def _lease_blocks_scheduler(lease: Mapping[str, Any]) -> bool:
    if lease.get("blocks_scheduler") is False:
        return False
    return str(lease.get("role") or "").strip().lower() != "inspector"


def _check_machine_runner_locks(project: Path, now: datetime) -> dict[str, Any]:
    try:
        config = load_agent_runners(project)
    except AgentRunnerConfigError as error:
        return _check(
            "machine_runner_locks",
            WARN,
            "Machine runner lock diagnostics could not load agent runner configuration.",
            details={"errors": list(error.errors)},
        )

    configured: dict[str, list[str]] = {}
    for runner in config.runners.values():
        policy = machine_resource_policy_from_runner(runner)
        if policy is None:
            continue
        lock_key = str(policy.get("lock_key") or "").strip()
        if not lock_key:
            continue
        configured.setdefault(lock_key, []).append(runner.runner_id)

    if not configured:
        return _check(
            "machine_runner_locks",
            PASS,
            "No configured machine-level runner locks are required.",
            count=0,
            details={"configured": []},
        )

    inspections = [
        inspect_runner_lock(lock_key, runner_ids=runner_ids, now=now)
        for lock_key, runner_ids in sorted(configured.items())
    ]
    stale = _runner_lock_states(inspections, RUNNER_LOCK_STALE)
    malformed = _runner_lock_states(inspections, RUNNER_LOCK_MALFORMED)
    unknown = _runner_lock_states(inspections, RUNNER_LOCK_UNKNOWN)
    active = _runner_lock_states(inspections, RUNNER_LOCK_ACTIVE)
    absent = _runner_lock_states(inspections, RUNNER_LOCK_ABSENT)
    details = {
        "configured": [{"lock_key": key, "runner_ids": sorted(runner_ids)} for key, runner_ids in sorted(configured.items())],
        "active": active,
        "absent": absent,
        "stale": stale,
        "malformed": malformed,
        "unknown_liveness": unknown,
    }

    if stale or malformed:
        if stale and malformed:
            message = (
                "Stale and Malformed machine-level runner locks were found. "
                "Remove stale lock files only after verifying no active runner still uses the shared resource."
            )
        elif stale:
            message = (
                "Stale machine-level runner locks were found. "
                "Remove stale lock files only after verifying no active runner still uses the shared resource."
            )
        else:
            message = (
                "Malformed machine-level runner locks were found. "
                "Verify no active runner still uses the shared resource before removing malformed lock files."
            )
        return _check("machine_runner_locks", FAIL, message, details=details)

    if unknown:
        return _check(
            "machine_runner_locks",
            WARN,
            "Machine-level runner lock process liveness could not be determined.",
            details=details,
        )

    return _check(
        "machine_runner_locks",
        PASS,
        f"{len(active)} active and {len(absent)} absent configured machine-level runner lock(s) are healthy.",
        count=len(inspections),
        details=details,
    )


def _check_background_jobs(paths: WorkflowPaths, now: datetime) -> dict[str, Any]:
    jobs_path = paths.runtime_dir / "background_jobs.json"
    if not jobs_path.exists():
        return _check("background_jobs", WARN, "Background job registry is missing.", path=_relative(paths, jobs_path))

    data, error = _read_json(jobs_path)
    if error:
        return _check("background_jobs", FAIL, f"Background job registry is malformed: {error}.", severity=UNHEALTHY, path=_relative(paths, jobs_path))
    jobs = _jobs_from_background_registry(data)
    if jobs is None:
        return _check("background_jobs", FAIL, "Background job registry must be an object with jobs or a job array.", path=_relative(paths, jobs_path))
    if not jobs:
        return _check("background_jobs", PASS, "No background jobs are registered.", count=0)

    problems: list[str] = []
    warnings: list[str] = []
    for index, job in enumerate(jobs):
        label = str(job.get("job_id") or f"job[{index}]")
        status = str(job.get("status") or "").strip().lower()
        if not _non_empty_string(job.get("job_id")):
            problems.append(f"{label}: missing job_id")
        if not _non_empty_string(status):
            problems.append(f"{label}: missing status")
            status = "needs_recovery"
        elif status not in ALLOWED_BACKGROUND_JOB_STATUSES:
            problems.append(f"{label}: status {status!r} is not allowed")
        if job.get("next_prompt_ready") is not None and not isinstance(job.get("next_prompt_ready"), bool):
            problems.append(f"{label}: next_prompt_ready must be boolean when present")
        if status in {"failed", "timed_out"}:
            # These are valid terminal execution records. They must remain in
            # the registry as failure evidence and are ingested by the
            # scheduler's autonomous recovery queue; dead PIDs and old
            # heartbeats are expected after termination, not malformed state.
            warnings.append(f"{label}: terminal status {status!r} is retained for autonomous scheduler recovery")
            continue
        if status in BACKGROUND_JOB_PROBLEM_STATUSES:
            problems.append(f"{label}: status {status!r} requires scheduler recovery or human attention")
        if status in BACKGROUND_JOB_SAFE_STATUSES:
            continue
        heartbeat = _parse_timestamp(job.get("heartbeat_at") or job.get("started_at"))
        if heartbeat is None:
            problems.append(f"{label}: missing parseable heartbeat")
        elif _age_seconds(now, heartbeat) > BACKGROUND_JOB_TTL_SECONDS:
            problems.append(f"{label}: stale heartbeat")
        if job.get("next_prompt_ready") is False and not _non_empty_string(job.get("wake_next_agent_when")):
            warnings.append(f"{label}: next_prompt_ready=false should include wake_next_agent_when")
        pid_value = job.get("pid")
        supervisor_host = str(job.get("supervisor_host") or "").strip()
        pid_probe_is_local = not supervisor_host or supervisor_host == socket.gethostname()
        if pid_value is not None and pid_probe_is_local:
            pid = _positive_int(pid_value, -1)
            if pid <= 0 or _pid_exists(pid) is False:
                problems.append(f"{label}: process is not live")
        wake_check = job.get("wake_check")
        if isinstance(wake_check, Mapping) and wake_check.get("type") == "file_exists_and_process_exited":
            paths_value = wake_check.get("paths")
            if not isinstance(paths_value, list) or not all(isinstance(item, str) and item for item in paths_value):
                problems.append(f"{label}: wake_check paths must be a non-empty string list")

    if problems:
        return _check("background_jobs", FAIL, "One or more background job records are inconsistent.", details={"problems": problems, "warnings": warnings})
    if warnings:
        return _check("background_jobs", WARN, "Background job records are parseable with advisory issues.", details={"warnings": warnings})
    return _check("background_jobs", PASS, f"{len(jobs)} background job record(s) are well formed.", count=len(jobs))


def _check_agent_status_files(paths: WorkflowPaths) -> dict[str, Any]:
    files = _recent_files(paths.results_dir, "agent_status.json")
    if not files:
        return _check("agent_status_files", PASS, "No recent agent_status.json files are present.", count=0)
    problems: list[str] = []
    warnings: list[str] = []
    background_run_ids, background_job_ids = _background_registry_identities(paths)
    resolved_agent_status_paths = _resolved_failure_agent_status_paths(paths)
    for path in files:
        data, error = _read_json_object(path)
        rel = _relative(paths, path)
        file_problems: list[str] = []
        if error:
            file_problems.append(f"{rel}: {error}")
            if rel in resolved_agent_status_paths:
                warnings.extend(file_problems)
            else:
                problems.extend(file_problems)
            continue
        if data.get("schema_version") != SCHEMA_VERSION:
            warnings.append(
                f"{rel}: schema_version {data.get('schema_version')!r} was accepted as compatible with {SCHEMA_VERSION!r}"
            )
        raw_worker_status = data.get("status")
        worker_status = normalize_worker_status(raw_worker_status)
        if worker_status not in WORKER_STATUSES:
            if rel in resolved_agent_status_paths:
                status_label = str(raw_worker_status or "missing")
                file_problems.append(
                    f"{rel}: historical recovered run has status {status_label!r}; retained for audit and not blocking current completion"
                )
            else:
                file_problems.append(f"{rel}: status is missing or invalid")
        elif isinstance(raw_worker_status, str) and raw_worker_status.strip() and raw_worker_status.strip().lower().replace("-", "_").replace(" ", "_") != worker_status:
            warnings.append(f"{rel}: status {raw_worker_status!r} is accepted as alias for {worker_status!r}")
        if data.get("next_prompt_ready") is not None and not isinstance(data.get("next_prompt_ready"), bool):
            file_problems.append(f"{rel}: next_prompt_ready must be boolean when present")
        run_id = _non_empty_string(data.get("run_id"))
        if not run_id:
            file_problems.append(f"{rel}: missing run_id")
        next_prompt_ready = data.get("next_prompt_ready")
        if worker_status == "running_background" or next_prompt_ready is False:
            if not _agent_status_wake_next_agent_when(data):
                file_problems.append(f"{rel}: unsafe background status must include wake_next_agent_when")
            reported_background_job_ids = _agent_status_background_job_ids(data)
            has_matching_background = bool(
                (run_id and run_id in background_run_ids)
                or reported_background_job_ids.intersection(background_job_ids)
            )
            if run_id and not has_matching_background:
                file_problems.append(f"{rel}: unsafe background status has no matching background_jobs.json record")
        if file_problems:
            if rel in resolved_agent_status_paths:
                warnings.extend(file_problems)
            else:
                problems.extend(file_problems)
    if problems:
        return _check(
            "agent_status_files",
            FAIL,
            "Recent agent_status.json files failed parse or schema checks.",
            details={"problems": problems, "warnings": warnings},
        )
    if warnings:
        return _check(
            "agent_status_files",
            WARN,
            "Recent agent_status.json files are usable with advisory issues.",
            details={"warnings": warnings},
            count=len(files),
        )
    return _check("agent_status_files", PASS, f"{len(files)} recent agent_status.json file(s) are parseable and schema-valid.", count=len(files))


def _check_validation_files(paths: WorkflowPaths) -> dict[str, Any]:
    files = _recent_files(paths.results_dir, "validation.json")
    if not files:
        return _check("validations", PASS, "No validation.json files are present yet.", count=0)
    problems: list[str] = []
    for path in files:
        data, error = _read_json_object(path)
        rel = _relative(paths, path)
        if error:
            problems.append(f"{rel}: {error}")
            continue
        if data.get("schema_version") != SCHEMA_VERSION:
            problems.append(f"{rel}: schema_version must be {SCHEMA_VERSION}")
        if str(data.get("status") or "") not in VALIDATION_STATUSES:
            problems.append(f"{rel}: status is missing or invalid")
        if not (_non_empty_string(data.get("run_id")) or _non_empty_string(data.get("primary_task_id"))):
            problems.append(f"{rel}: missing run_id or primary_task_id")
    if problems:
        return _check("validations", FAIL, "Authoritative validation.json files failed parse or schema checks.", details={"problems": problems})
    return _check("validations", PASS, f"{len(files)} validation.json file(s) are parseable and schema-valid.", count=len(files))


def _check_completion_marker(paths: WorkflowPaths) -> dict[str, Any]:
    marker = completion_marker_status(paths)
    if not marker.get("exists"):
        return _check("completion_marker_freshness", PASS, "No completion marker is present.", details=marker)
    if marker.get("fresh"):
        return _check("completion_marker_freshness", PASS, "Completion marker is fresh.", details=marker)
    return _check("completion_marker_freshness", WARN, "Completion marker is stale and will be ignored.", details=marker)


def _check_failure_registry(paths: WorkflowPaths) -> dict[str, Any]:
    path = paths.runtime_dir / "failure_registry.json"
    if not path.exists():
        return _check("failure_registry", WARN, "Failure registry is missing.", path=_relative(paths, path))
    data, error = _read_json_object(path)
    if error:
        return _check("failure_registry", FAIL, f"Failure registry is malformed: {error}.", severity=UNHEALTHY, path=_relative(paths, path))
    if data.get("schema_version") not in {None, SCHEMA_VERSION}:
        return _check("failure_registry", FAIL, f"Failure registry schema_version must be {SCHEMA_VERSION}.", path=_relative(paths, path))
    failures = data.get("failures")
    if not isinstance(failures, list):
        return _check("failure_registry", FAIL, "Failure registry must contain a failures array.", path=_relative(paths, path))

    problems: list[str] = []
    exhausted: list[str] = []
    needs_human: list[str] = []
    recoverable = 0
    allowed_statuses = {"unrecovered", "recovering", "recovered", "waived", "exhausted", "needs_human"}
    for index, failure in enumerate(failures):
        if not isinstance(failure, Mapping):
            problems.append(f"failures[{index}]: must be an object")
            continue
        label = str(failure.get("failure_id") or failure.get("id") or f"failures[{index}]")
        attempts = _maybe_int(failure.get("recovery_attempts", failure.get("attempts", 0)))
        budget = _maybe_int(failure.get("max_recovery_attempts", failure.get("max_attempts", 1)))
        if attempts is None or budget is None:
            problems.append(f"{label}: recovery attempts and budget must be integers")
            continue
        status = str(failure.get("status") or "unrecovered").lower()
        if status not in allowed_statuses:
            problems.append(f"{label}: status {status!r} is not allowed")
            continue
        if status == "unrecovered":
            recoverable += 1
            if attempts >= budget:
                exhausted.append(label)
        elif status == "exhausted":
            exhausted.append(label)
        elif status == "needs_human":
            needs_human.append(label)
    if problems:
        return _check("failure_registry", FAIL, "Failure registry budgets could not be computed.", details={"problems": problems})
    if needs_human:
        return _check("failure_registry", WARN, "One or more failures need human recovery input.", details={"needs_human": needs_human, "recoverable": recoverable})
    if exhausted:
        return _check("failure_registry", WARN, "One or more unresolved failures exhausted recovery budget.", details={"exhausted": exhausted, "recoverable": recoverable})
    return _check("failure_registry", PASS, "Failure registry is parseable and recovery budgets are computable.", details={"failures": len(failures), "recoverable": recoverable})


def _check_expansion_registry(paths: WorkflowPaths) -> dict[str, Any]:
    path = paths.runtime_dir / "expansion_registry.json"
    if not path.exists():
        return _check("expansion_registry", WARN, "Self-expansion registry is missing.", path=_relative(paths, path))
    data, error = _read_json_object(path)
    if error:
        return _check("expansion_registry", FAIL, f"Self-expansion registry is malformed: {error}.", severity=UNHEALTHY, path=_relative(paths, path))
    if data.get("schema_version") not in {None, SCHEMA_VERSION}:
        return _check("expansion_registry", FAIL, f"Self-expansion registry schema_version must be {SCHEMA_VERSION}.", path=_relative(paths, path))
    proposals = data.get("proposals")
    events = data.get("events")
    if not isinstance(proposals, list) or not isinstance(events, list):
        return _check("expansion_registry", FAIL, "Self-expansion registry must contain proposals and events arrays.", path=_relative(paths, path))
    malformed = [
        index
        for index, proposal in enumerate(proposals)
        if not isinstance(proposal, Mapping) or not str(proposal.get("proposal_id") or "")
    ]
    if malformed:
        return _check("expansion_registry", FAIL, "One or more self-expansion proposal records are malformed.", details={"malformed": malformed})
    return _check(
        "expansion_registry",
        PASS,
        "Self-expansion registry is parseable.",
        details={
            "cycle": data.get("cycle", 0),
            "proposals": len(proposals),
            "events": len(events),
        },
    )


def _check_git_checkpoints(project: Path, paths: WorkflowPaths) -> dict[str, Any]:
    doctor = run_git_doctor(project)
    if not doctor.get("ok"):
        return _check("git_checkpoints", FAIL, "Git checkpoint manager is unavailable.", details={"errors": doctor.get("errors", []), "warnings": doctor.get("warnings", [])})

    path = paths.runtime_dir / "git_checkpoints.jsonl"
    if not path.exists():
        return _check("git_checkpoints", WARN, "Git checkpoint log is missing.", path=_relative(paths, path))
    records, errors = _read_jsonl(path)
    if errors:
        return _check("git_checkpoints", FAIL, "Git checkpoint log contains malformed JSONL records.", details={"problems": errors})

    created = [record for record in records if str(record.get("status") or "") == "created"]
    if not created:
        return _check("git_checkpoints", PASS, "Git checkpoint manager is available; no checkpoint records exist yet.", count=0)

    runner = SubprocessGitCommandRunner()
    missing_refs: list[str] = []
    malformed: list[str] = []
    for record in created:
        checkpoint_id = _non_empty_string(record.get("checkpoint_id"))
        ref = _non_empty_string(record.get("ref"))
        commit = _non_empty_string(record.get("commit"))
        if checkpoint_id is None or ref is None or commit is None:
            malformed.append(str(checkpoint_id or ref or "<unknown>"))
            continue
        result = runner.run(project, ("show-ref", "--verify", "--quiet", ref))
        if result.returncode != 0:
            missing_refs.append(ref)
    if malformed:
        return _check("git_checkpoints", FAIL, "Git checkpoint records are missing required fields.", details={"malformed": malformed})
    if missing_refs:
        return _check("git_checkpoints", FAIL, "Required Git checkpoint refs are missing.", details={"missing_refs": missing_refs})
    return _check("git_checkpoints", PASS, f"{len(created)} Git checkpoint ref(s) are available.", count=len(created))


def _check_event_segments(paths: WorkflowPaths) -> dict[str, Any]:
    events_dir = paths.runtime_dir / "events"
    if not events_dir.exists():
        return _check("event_segments", FAIL, "Runtime events directory is missing.", path=_relative(paths, events_dir))
    segments = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    if not segments:
        return _check("event_segments", FAIL, "No runtime event segments are present.", path=_relative(paths, events_dir))

    errors: list[str] = []
    last_sequence: int | None = None
    last_event_id: str | None = None
    last_event_hash: str | None = None
    records = 0
    for segment in segments:
        segment_records, segment_errors = _read_jsonl(segment)
        errors.extend(segment_errors)
        for record in segment_records:
            sequence = _maybe_int(record.get("sequence", record.get("seq")))
            if sequence is None:
                errors.append(f"{_relative(paths, segment)}: event record missing integer sequence")
                continue
            if last_sequence is not None and sequence <= last_sequence:
                errors.append(f"{_relative(paths, segment)}: sequence {sequence} is not monotonic after {last_sequence}")
            event_id = _non_empty_string(record.get("event_id"))
            event_hash = _non_empty_string(record.get("event_hash"))
            if event_id is None:
                errors.append(f"{_relative(paths, segment)}: sequence {sequence} missing event_id")
            if event_hash is None:
                errors.append(f"{_relative(paths, segment)}: sequence {sequence} missing event_hash")
            elif event_hash != _event_record_hash(record):
                errors.append(f"{_relative(paths, segment)}: sequence {sequence} event_hash does not match record content")
            if last_sequence is not None:
                prev_event_id = record.get("prev_event_id")
                prev_event_hash = record.get("prev_event_hash")
                if event_id is not None and prev_event_id != last_event_id:
                    errors.append(f"{_relative(paths, segment)}: sequence {sequence} prev_event_id does not match previous event")
                if event_hash is not None and prev_event_hash != last_event_hash:
                    errors.append(f"{_relative(paths, segment)}: sequence {sequence} prev_event_hash does not match previous event")
            last_sequence = sequence
            last_event_id = event_id
            last_event_hash = event_hash
            records += 1
    if errors:
        return _check("event_segments", FAIL, "Runtime event segments are malformed, non-monotonic, or hash-chain invalid.", severity=UNHEALTHY, details={"problems": errors})
    return _check("event_segments", PASS, f"{len(segments)} event segment(s) are parseable, monotonic, and hash-chain valid.", details={"segments": len(segments), "records": records})


def _check_read_models(paths: WorkflowPaths) -> dict[str, Any]:
    read_models_dir = paths.read_models_dir
    if not read_models_dir.exists():
        return _check("read_models", WARN, "Read model directory is missing but rebuildable from authoritative runtime files.", path=_relative(paths, read_models_dir))

    warnings: list[str] = []
    parsed_times: list[datetime] = []
    for filename in READ_MODEL_FILES:
        path = read_models_dir / filename
        rel = _relative(paths, path)
        if not path.exists():
            warnings.append(f"{rel}: missing")
            continue
        if filename.endswith(".jsonl"):
            _records, errors = _read_jsonl(path)
            warnings.extend(errors)
            continue
        data, error = _read_json(path)
        if error:
            warnings.append(f"{rel}: {error}")
            continue
        if isinstance(data, Mapping):
            generated_at = _parse_timestamp(data.get("generated_at") or data.get("updated_at"))
            if generated_at is not None:
                parsed_times.append(generated_at)

    latest_event = _latest_event_timestamp(paths.runtime_dir / "events")
    if latest_event is not None and parsed_times and max(parsed_times) < latest_event:
        warnings.append("read models are older than the latest runtime event")

    if warnings:
        return _check("read_models", WARN, "Read models are stale, missing, or malformed but rebuildable.", details={"warnings": warnings})
    return _check("read_models", PASS, "Read models are parseable and appear fresh or rebuildable.")


def _overall_status(checks: Sequence[Mapping[str, Any]]) -> str:
    has_warning = False
    has_degraded_failure = False
    for check in checks:
        status = check.get("status")
        if status == FAIL and check.get("severity") == UNHEALTHY:
            return UNHEALTHY
        if status == FAIL:
            has_degraded_failure = True
        elif status == WARN:
            has_warning = True
    if has_degraded_failure:
        return DEGRADED
    if has_warning:
        return HEALTHY_WITH_WARNINGS
    return HEALTHY


def _requires_attention(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    attention: list[dict[str, str]] = []
    for check in checks:
        if check.get("status") in {WARN, FAIL}:
            attention.append(
                {
                    "name": str(check.get("name") or "unknown"),
                    "status": str(check.get("status") or "unknown"),
                    "message": str(check.get("message") or ""),
                }
            )
    return attention


def _check(
    name: str,
    status: str,
    message: str,
    *,
    severity: str = DEGRADED,
    path: str | None = None,
    count: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    check = {
        "name": name,
        "status": status,
        "message": message,
    }
    if status == FAIL:
        check["severity"] = severity
    if path is not None:
        check["path"] = path
    if count is not None:
        check["count"] = count
    if details is not None:
        check["details"] = _json_safe(details)
    return check


def _jobs_from_background_registry(data: object) -> list[Mapping[str, Any]] | None:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        jobs = data.get("jobs")
        if jobs is None:
            return []
        if isinstance(jobs, list):
            return [item for item in jobs if isinstance(item, Mapping)]
    return None


def _runner_lock_states(inspections: Sequence[Mapping[str, Any]], state: str) -> list[dict[str, Any]]:
    return [dict(inspection) for inspection in inspections if inspection.get("state") == state]


def _background_registry_identities(paths: WorkflowPaths) -> tuple[set[str], set[str]]:
    data, error = _read_json(paths.runtime_dir / "background_jobs.json")
    if error:
        return set(), set()
    jobs = _jobs_from_background_registry(data)
    if jobs is None:
        return set(), set()
    return (
        {
            str(job.get("run_id") or "")
            for job in jobs
            if isinstance(job, Mapping) and job.get("run_id")
        },
        {
            str(job.get("job_id") or "")
            for job in jobs
            if isinstance(job, Mapping) and job.get("job_id")
        },
    )


def _agent_status_background_job_ids(data: Mapping[str, Any]) -> set[str]:
    records: list[Mapping[str, Any]] = []
    for field in ("background_jobs", "background_job_records"):
        value = data.get(field)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            records.extend(item for item in value if isinstance(item, Mapping))
    background = data.get("background")
    if isinstance(background, Mapping):
        value = background.get("jobs")
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            records.extend(item for item in value if isinstance(item, Mapping))
    background_state = data.get("background_state")
    if isinstance(background_state, Mapping):
        for field in ("active_background_jobs", "background_jobs", "jobs"):
            value = background_state.get(field)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                records.extend(item for item in value if isinstance(item, Mapping))
    return {
        str(record.get("job_id") or "")
        for record in records
        if record.get("job_id")
    }


def _resolved_failure_agent_status_paths(paths: WorkflowPaths) -> set[str]:
    data, error = _read_json(paths.runtime_dir / "failure_registry.json")
    if error or not isinstance(data, Mapping):
        return set()
    failures = data.get("failures")
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        return set()
    paths_set: set[str] = set()
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("status") or "").lower() not in RESOLVED_FAILURE_STATUSES:
            continue
        agent_status_path = failure.get("agent_status_path")
        if isinstance(agent_status_path, str) and agent_status_path.strip():
            paths_set.add(agent_status_path.strip())
    return paths_set


def _agent_status_wake_next_agent_when(data: Mapping[str, Any]) -> bool:
    value = data.get("wake_next_agent_when")
    if isinstance(value, str) and value.strip():
        return True
    background = data.get("background")
    if isinstance(background, Mapping):
        nested = background.get("wake_next_agent_when")
        if isinstance(nested, str) and nested.strip():
            return True
    jobs = data.get("background_jobs")
    if isinstance(jobs, Sequence) and not isinstance(jobs, (str, bytes)):
        return any(
            isinstance(job, Mapping)
            and isinstance(job.get("wake_next_agent_when"), str)
            and bool(str(job.get("wake_next_agent_when")).strip())
            for job in jobs
        )
    return False


def _recent_files(root: Path, name: str) -> list[Path]:
    try:
        files = [path for path in root.glob(f"**/{name}") if path.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)[:RECENT_FILE_LIMIT]


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return None, f"invalid JSON at line {error.lineno} column {error.colno}"
    except OSError as error:
        return None, str(error)
    return data, None


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    data, error = _read_json(path)
    if error:
        return {}, error
    if not isinstance(data, Mapping):
        return {}, "expected JSON object"
    return dict(data), None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return records, [f"{path}: {error}"]
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"{path}: line {index}: invalid JSON at column {error.colno}")
            continue
        if not isinstance(record, Mapping):
            errors.append(f"{path}: line {index}: expected JSON object")
            continue
        records.append(dict(record))
    return records, errors


def _latest_event_timestamp(events_dir: Path) -> datetime | None:
    latest: datetime | None = None
    for segment in sorted(events_dir.glob("*.jsonl")):
        records, _errors = _read_jsonl(segment)
        for record in records:
            timestamp = _parse_timestamp(record.get("ts") or record.get("timestamp") or record.get("created_at"))
            if timestamp is not None and (latest is None or timestamp > latest):
                latest = timestamp
    return latest


def _event_record_hash(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("event_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(now: datetime, then: datetime) -> int:
    return max(0, int((now - then).total_seconds()))


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


def _maybe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, default: int) -> int:
    parsed = _maybe_int(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed


def _non_empty_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _relative(paths: WorkflowPaths, path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
