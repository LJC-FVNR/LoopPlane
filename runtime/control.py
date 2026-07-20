from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.workflow_lifecycle import ensure_compatibility_workflow_metadata


SCHEMA_VERSION = "1.5"
CONTROL_REQUESTS_FILENAME = "control_requests.jsonl"
CONTROL_RESPONSES_FILENAME = "control_responses.jsonl"
ATTACH_ACTIVE_SUPERVISOR_STATUSES = frozenset(
    {
        "launching",
        "running",
        "paused",
        "waiting_config",
        "waiting_approval",
        "waiting_background",
    }
)
CONTROL_REQUEST_TYPES = frozenset(
    {
        "start",
        "pause",
        "resume",
        "stop",
        "attach",
        "migrate",
        "cancel_run",
        "cancel_background_job",
        "rebuild_read_models",
        "run_final_verifier",
    }
)
MUTATING_CONTROL_REQUEST_TYPES = CONTROL_REQUEST_TYPES - frozenset({"attach"})
MISSING_CONTROL_RECORD_ID_PREFIX = "control_missing_id_"


def record_control_request(
    project_root: Path | str,
    request_type: str,
    *,
    source: str = "cli",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    normalized_type = str(request_type or "").strip().lower().replace("-", "_")
    if normalized_type not in CONTROL_REQUEST_TYPES:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="invalid_control_type",
            message=f"Control request type must be one of: {', '.join(sorted(CONTROL_REQUEST_TYPES))}.",
        )
    mutability_failure = _control_mutability_failure(project, workflow_id, normalized_type)
    if mutability_failure is not None:
        return mutability_failure

    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": new_control_request_id(),
        "created_at": utc_timestamp(),
        "type": normalized_type,
        "source": source or "cli",
        "workflow_id": workflow_id,
        "status": "pending",
    }
    if payload:
        request["payload"] = _json_safe_object(payload)
    _append_jsonl(paths.runtime_dir / CONTROL_REQUESTS_FILENAME, request)

    responses = read_control_responses(paths)
    requests = read_control_requests(paths)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "pending",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "requests_path": _path_for_record(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": _path_for_record(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "request": request,
        "pending_count": sum(
            1 for record in control_request_statuses(requests, responses) if record.get("status") == "pending"
        ),
        "errors": [],
        "warnings": [],
    }


def load_control_status(project_root: Path | str) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    state = _read_json_object(paths.runtime_dir / "state.json", default={})
    requests = read_control_requests(paths)
    responses = read_control_responses(paths)
    controls = control_request_statuses(requests, responses)
    latest_response = _latest_response(responses)
    runtime_status = str(state.get("status") or "unknown")
    completion_marker = _load_completion_marker_status(paths)
    supervisor = _load_supervisor_status(project)
    status = _completion_aware_status(runtime_status, completion_marker)
    scheduler = _completion_aware_scheduler(state.get("scheduler"), status=status)
    scheduler = _background_registry_aware_scheduler(paths, scheduler)
    warnings = _completion_marker_warnings(completion_marker)
    next_steps = _status_next_steps(state)
    if isinstance(supervisor, Mapping):
        supervisor_warnings = supervisor.get("warnings")
        if isinstance(supervisor_warnings, Sequence) and not isinstance(supervisor_warnings, (str, bytes)):
            warnings.extend(str(warning) for warning in supervisor_warnings if warning)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "runtime_status": runtime_status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "runtime_state": {**dict(state), "scheduler": scheduler} if isinstance(state, Mapping) else {"scheduler": scheduler},
        "scheduler": scheduler,
        "supervisor": supervisor,
        "completion_marker": completion_marker,
        "requests_path": _path_for_record(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": _path_for_record(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "pending_count": sum(1 for record in controls if record.get("status") == "pending"),
        "applied_count": sum(1 for record in controls if record.get("status") == "applied"),
        "rejected_count": sum(1 for record in controls if record.get("status") == "rejected"),
        "controls": controls,
        "latest_response": latest_response,
        "errors": [],
        "warnings": warnings,
        "next_steps": next_steps,
    }


def load_control_logs(project_root: Path | str, *, lines: int = 50) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    limit = max(1, int(lines))
    event_records = _tail_event_records(paths.runtime_dir / "events", limit=limit)
    control_requests = read_control_requests(paths)[-limit:]
    control_responses = read_control_responses(paths)[-limit:]
    supervisor_logs = _load_supervisor_logs(project, lines=limit)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "events_dir": _path_for_record(project, paths.runtime_dir / "events"),
        "requests_path": _path_for_record(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": _path_for_record(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "events": event_records,
        "control_requests": control_requests,
        "control_responses": control_responses,
        "supervisor": supervisor_logs.get("supervisor") if isinstance(supervisor_logs, Mapping) else {},
        "supervisor_logs": supervisor_logs,
        "errors": [],
        "warnings": [],
    }


def load_control_attach(
    project_root: Path | str,
    *,
    lines: int = 50,
    follow: bool = False,
    timeout_seconds: float = 0.0,
    poll_interval_seconds: float = 0.2,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    limit = max(1, int(lines))
    timeout = max(0.0, float(timeout_seconds))
    poll_interval = max(0.05, float(poll_interval_seconds))
    deadline = time.monotonic() + timeout if follow or timeout > 0 else time.monotonic()
    poll_count = 0
    result = _load_attach_snapshot(project, paths, workflow_id, lines=limit)

    while follow and result.get("active") and time.monotonic() < deadline:
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        poll_count += 1
        result = _load_attach_snapshot(project, paths, workflow_id, lines=limit)

    follow_info = {
        "enabled": bool(follow),
        "timeout_seconds": timeout,
        "poll_interval_seconds": poll_interval,
        "poll_count": poll_count,
    }
    result["follow"] = follow_info
    return result


def control_request_statuses(
    requests: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for request in requests:
        request_id = control_record_id(request)
        response = latest_control_response(responses, request_id)
        record = dict(request)
        if _record_id(request) is None:
            record["request_id"] = request_id
            record["synthetic_request_id"] = True
        if response is None:
            record["status"] = str(request.get("status") or "pending").lower()
        else:
            record["status"] = str(response.get("status") or "applied").lower()
            record["response"] = dict(response)
            record["handled_at"] = response.get("handled_at")
            record["resulting_workflow_status"] = response.get("resulting_workflow_status")
        records.append(record)
    return records


def latest_control_response(
    responses: Sequence[Mapping[str, Any]],
    request_id: str | None,
) -> dict[str, Any] | None:
    if not request_id:
        return None
    matches = [dict(response) for response in responses if control_record_id(response) == request_id]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("handled_at") or ""))[-1]


def read_control_requests(paths: WorkflowPaths) -> list[dict[str, Any]]:
    return _read_jsonl(paths.runtime_dir / CONTROL_REQUESTS_FILENAME)


def read_control_responses(paths: WorkflowPaths) -> list[dict[str, Any]]:
    return _read_jsonl(paths.runtime_dir / CONTROL_RESPONSES_FILENAME)


def new_control_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"ctrl_{stamp}_{uuid.uuid4().hex[:8]}"


def control_record_id(record: Mapping[str, Any] | None) -> str | None:
    explicit_id = _record_id(record)
    if explicit_id:
        return explicit_id
    if not isinstance(record, Mapping):
        return None
    encoded = json.dumps(
        _json_safe_object(record),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{MISSING_CONTROL_RECORD_ID_PREFIX}{sha256(encoded.encode('utf-8')).hexdigest()[:12]}"


def control_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def control_attach_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def format_control_request_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane control request: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    request = result.get("request")
    if isinstance(request, Mapping):
        lines.append(f"request_id: {request.get('request_id')}")
        lines.append(f"type: {request.get('type')}")
        lines.append(f"created_at: {request.get('created_at')}")
    if result.get("pending_count") is not None:
        lines.append(f"pending_controls: {result.get('pending_count')}")
    _append_problem_lines(lines, result)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    next_steps = result.get("next_steps")
    if isinstance(next_steps, Sequence) and not isinstance(next_steps, (str, bytes)) and next_steps:
        lines.append("next_steps:")
        lines.extend(f"  - {step}" for step in next_steps)
    return "\n".join(lines) + "\n"


def format_control_attach_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane attach: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    if result.get("message"):
        lines.append(f"message: {result.get('message')}")
    if result.get("runtime_status"):
        lines.append(f"runtime_status: {result.get('runtime_status')}")
    supervisor = result.get("supervisor")
    if isinstance(supervisor, Mapping):
        lines.append(f"supervisor_status: {supervisor.get('status', 'unknown')}")
        lines.append(f"supervisor_liveness: {supervisor.get('liveness', 'unknown')}")
        if supervisor.get("pid") is not None:
            lines.append(f"supervisor_pid: {supervisor.get('pid')}")
        if supervisor.get("metadata_path"):
            lines.append(f"supervisor_metadata: {supervisor.get('metadata_path')}")
    tail = result.get("tail")
    if isinstance(tail, Mapping):
        if tail.get("supervisor_stdout_path"):
            lines.append(f"supervisor_stdout: {tail.get('supervisor_stdout_path')}")
        if tail.get("supervisor_stderr_path"):
            lines.append(f"supervisor_stderr: {tail.get('supervisor_stderr_path')}")
        events = tail.get("events")
        if isinstance(events, Sequence) and not isinstance(events, (str, bytes)) and events:
            lines.append("events:")
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                lines.append(
                    "  - "
                    f"{event.get('sequence') or '?'} "
                    f"{event.get('event_type') or 'unknown'} "
                    f"{event.get('event_id') or ''}".rstrip()
                )
        stdout = tail.get("supervisor_stdout")
        if isinstance(stdout, Sequence) and not isinstance(stdout, (str, bytes)) and stdout:
            lines.append("supervisor_stdout_tail:")
            lines.extend(f"  - {line}" for line in stdout[-5:])
        stderr = tail.get("supervisor_stderr")
        if isinstance(stderr, Sequence) and not isinstance(stderr, (str, bytes)) and stderr:
            lines.append("supervisor_stderr_tail:")
            lines.extend(f"  - {line}" for line in stderr[-5:])
    follow = result.get("follow")
    if isinstance(follow, Mapping) and follow.get("enabled"):
        lines.append(
            "follow: "
            f"timeout={follow.get('timeout_seconds')}s "
            f"polls={follow.get('poll_count')}"
        )
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    next_steps = result.get("next_steps")
    if isinstance(next_steps, Sequence) and not isinstance(next_steps, (str, bytes)) and next_steps:
        lines.append("next_steps:")
        lines.extend(f"  - {step}" for step in next_steps)
    return "\n".join(lines) + "\n"


def format_control_status_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane status: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"pending_controls: {result.get('pending_count', 0)}",
        f"applied_controls: {result.get('applied_count', 0)}",
        f"rejected_controls: {result.get('rejected_count', 0)}",
    ]
    runtime_status = result.get("runtime_status")
    if runtime_status and runtime_status != result.get("status"):
        lines.append(f"runtime_status: {runtime_status}")
    scheduler = result.get("scheduler")
    if isinstance(scheduler, Mapping):
        if scheduler.get("last_action"):
            lines.append(f"last_action: {scheduler.get('last_action')}")
        if scheduler.get("last_control_request_id"):
            lines.append(f"last_control_request_id: {scheduler.get('last_control_request_id')}")
        if scheduler.get("paused") is not None:
            lines.append(f"paused: {str(bool(scheduler.get('paused'))).lower()}")
        if scheduler.get("stop_requested") is not None:
            lines.append(f"stop_requested: {str(bool(scheduler.get('stop_requested'))).lower()}")
    supervisor = result.get("supervisor")
    if isinstance(supervisor, Mapping):
        lines.append(f"supervisor_status: {supervisor.get('status', 'unknown')}")
        lines.append(f"supervisor_liveness: {supervisor.get('liveness', 'unknown')}")
        if supervisor.get("pid") is not None:
            lines.append(f"supervisor_pid: {supervisor.get('pid')}")
        if supervisor.get("metadata_path"):
            lines.append(f"supervisor_metadata: {supervisor.get('metadata_path')}")
    latest = result.get("latest_response")
    if isinstance(latest, Mapping):
        lines.append(f"latest_control_response: {latest.get('request_id')} {latest.get('status')}")
    marker = result.get("completion_marker")
    if isinstance(marker, Mapping):
        if marker.get("exists"):
            marker_state = "fresh" if marker.get("fresh") else "stale"
            lines.append(f"completion_marker: {marker_state}")
            if marker.get("path"):
                lines.append(f"completion_marker_path: {marker.get('path')}")
            stale_reasons = marker.get("stale_reasons") or []
            if stale_reasons:
                lines.append("completion_marker_stale_reasons:")
                for reason in stale_reasons:
                    lines.append(f"  - {reason}")
        else:
            lines.append("completion_marker: absent")
    _append_problem_lines(lines, result)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def format_control_logs_text(result: Mapping[str, Any]) -> str:
    lines = [
        "loopplane logs: ok" if result.get("ok") else "loopplane logs: failed",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    events = result.get("events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        lines.append("events:")
        for event in events:
            if not isinstance(event, Mapping):
                continue
            lines.append(
                "  - "
                f"{event.get('sequence') or '?'} "
                f"{event.get('event_type') or 'unknown'} "
                f"{event.get('event_id') or ''}".rstrip()
            )
    requests = result.get("control_requests")
    if isinstance(requests, Sequence) and not isinstance(requests, (str, bytes)):
        lines.append("control_requests:")
        for request in requests:
            if isinstance(request, Mapping):
                lines.append(f"  - {request.get('request_id')} {request.get('type')} {request.get('status')}")
    responses = result.get("control_responses")
    if isinstance(responses, Sequence) and not isinstance(responses, (str, bytes)):
        lines.append("control_responses:")
        for response in responses:
            if isinstance(response, Mapping):
                lines.append(f"  - {response.get('request_id')} {response.get('type')} {response.get('status')}")
    supervisor = result.get("supervisor")
    if isinstance(supervisor, Mapping) and supervisor.get("exists"):
        lines.append(
            "supervisor: "
            f"{supervisor.get('status', 'unknown')} "
            f"pid={supervisor.get('pid', 'unknown')} "
            f"liveness={supervisor.get('liveness', 'unknown')}"
        )
    supervisor_logs = result.get("supervisor_logs")
    if isinstance(supervisor_logs, Mapping):
        if supervisor_logs.get("stdout_path"):
            lines.append(f"supervisor_stdout: {supervisor_logs.get('stdout_path')}")
        if supervisor_logs.get("stderr_path"):
            lines.append(f"supervisor_stderr: {supervisor_logs.get('stderr_path')}")
        stderr = supervisor_logs.get("stderr")
        if isinstance(stderr, Sequence) and not isinstance(stderr, (str, bytes)) and stderr:
            lines.append("supervisor_stderr_tail:")
            lines.extend(f"  - {line}" for line in stderr[-5:])
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def _load_project_paths(project_root: Path | str) -> tuple[Path, WorkflowPaths, str]:
    project = Path(project_root).expanduser().resolve()
    ensure_compatibility_workflow_metadata(project, updated_by="loopplane control")
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    workflow_id = str(workflow.get("workflow_id") or "unknown_workflow")
    return project, paths, workflow_id


def _control_mutability_failure(project: Path, workflow_id: str, request_type: str) -> dict[str, Any] | None:
    if request_type not in MUTATING_CONTROL_REQUEST_TYPES:
        return None
    registry = _read_json_object(project / ".loopplane" / "workflow_registry.json", default={})
    workflows = registry.get("workflows") if isinstance(registry, Mapping) else None
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        return None
    selected = None
    for record in workflows:
        if isinstance(record, Mapping) and str(record.get("workflow_id") or "") == workflow_id:
            selected = record
            break
    if selected is None:
        return None
    status = str(selected.get("status") or "").strip()
    read_only = bool(selected.get("read_only")) or status == "read_only_imported"
    archived = bool(selected.get("archived")) or status == "archived"
    if read_only:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="read_only_workflow",
            message=(
                f"Cannot record {request_type} control request for read-only imported workflow "
                f"{workflow_id!r}; fork it before mutation or resume."
            ),
            recovery_actions=[
                "Use loopplane workflow show <workflow_id> for read-only inspection.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating imported history.",
            ],
            extra={"request_type": request_type},
        )
    if archived:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="archived_workflow",
            message=(
                f"Cannot record {request_type} control request for archived workflow {workflow_id!r}; "
                "restore or fork it before mutation."
            ),
            recovery_actions=[
                "Use loopplane workflow restore <workflow_id> before selecting an archived workflow.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating archived history.",
            ],
            extra={"request_type": request_type},
        )
    return None


def _load_completion_marker_status(paths: WorkflowPaths) -> dict[str, Any]:
    from runtime.scheduler import completion_marker_status

    return completion_marker_status(paths)


def _load_supervisor_status(project: Path) -> dict[str, Any]:
    from runtime.detached import load_supervisor_status

    return load_supervisor_status(project)


def _load_supervisor_logs(project: Path, *, lines: int) -> dict[str, Any]:
    from runtime.detached import load_supervisor_logs

    return load_supervisor_logs(project, lines=lines)


def _load_attach_snapshot(project: Path, paths: WorkflowPaths, workflow_id: str, *, lines: int) -> dict[str, Any]:
    state = _read_json_object(paths.runtime_dir / "state.json", default={})
    scheduler = dict(state.get("scheduler") or {}) if isinstance(state.get("scheduler"), Mapping) else {}
    supervisor = _load_supervisor_status(project)
    supervisor_logs = _load_supervisor_logs(project, lines=lines)
    event_records = _tail_event_records(paths.runtime_dir / "events", limit=lines)
    status, ok, message = _attach_status(supervisor)
    warnings: list[str] = []
    supervisor_warnings = supervisor.get("warnings") if isinstance(supervisor, Mapping) else None
    if isinstance(supervisor_warnings, Sequence) and not isinstance(supervisor_warnings, (str, bytes)):
        warnings.extend(str(warning) for warning in supervisor_warnings if warning)
    errors: list[str] = [] if ok else [message]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "active": ok,
        "message": message,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "observed_at": utc_timestamp(),
        "runtime_status": str(state.get("status") or "unknown"),
        "runtime_state": state,
        "scheduler": scheduler,
        "supervisor": supervisor,
        "tail": {
            "events": event_records,
            "supervisor_stdout": supervisor_logs.get("stdout") if isinstance(supervisor_logs, Mapping) else [],
            "supervisor_stderr": supervisor_logs.get("stderr") if isinstance(supervisor_logs, Mapping) else [],
            "supervisor_stdout_path": supervisor_logs.get("stdout_path") if isinstance(supervisor_logs, Mapping) else None,
            "supervisor_stderr_path": supervisor_logs.get("stderr_path") if isinstance(supervisor_logs, Mapping) else None,
        },
        "errors": errors,
        "warnings": warnings,
    }


def _attach_status(supervisor: Mapping[str, Any]) -> tuple[str, bool, str]:
    if not supervisor.get("ok"):
        return "supervisor_unavailable", False, "Unable to inspect detached supervisor metadata."
    if not supervisor.get("exists"):
        return "no_active_supervisor", False, "No detached supervisor metadata exists for this workflow."
    supervisor_status = str(supervisor.get("status") or "unknown")
    liveness = str(supervisor.get("liveness") or "unknown")
    if supervisor_status == "stale" or liveness == "dead":
        return "stale_supervisor", False, "Detached supervisor metadata is stale; no active runtime can be attached."
    if supervisor_status in ATTACH_ACTIVE_SUPERVISOR_STATUSES and liveness == "alive":
        return "attached", True, "Attached to active detached supervisor; showing recent runtime tails."
    return (
        "supervisor_not_active",
        False,
        f"Detached supervisor is {supervisor_status} with liveness {liveness}; no active runtime can be attached.",
    )


def _completion_aware_status(runtime_status: str, marker: Mapping[str, Any]) -> str:
    if runtime_status != "completed":
        return runtime_status
    if marker.get("fresh") is True:
        return "completed"
    if marker.get("exists"):
        return "completion_marker_stale"
    return "completion_marker_missing"


def _completion_aware_scheduler(value: object, *, status: str) -> dict[str, Any]:
    scheduler = dict(value) if isinstance(value, Mapping) else {}
    if status == "completed":
        scheduler["running"] = False
        scheduler["paused"] = False
        scheduler["stop_requested"] = False
        scheduler["active_run_id"] = None
        scheduler["active_node_id"] = None
        scheduler["active_task_id"] = None
    return scheduler


def _background_registry_aware_scheduler(
    paths: WorkflowPaths, scheduler: Mapping[str, Any]
) -> dict[str, Any]:
    """Overlay a cached scheduler job status with the authoritative registry.

    A paused supervisor intentionally does not mutate scheduler state, while a
    separately supervised background command may already have completed or
    been cancelled.  Status inspection must not continue to call that job
    running merely because the scheduler cache has not resumed.
    """

    result = dict(scheduler)
    active_job_id = result.get("active_background_job_id")
    if not isinstance(active_job_id, str) or not active_job_id.strip():
        return result
    registry = _read_json_object(
        paths.runtime_dir / "background_jobs.json", default={}
    )
    jobs = registry.get("jobs") if isinstance(registry, Mapping) else None
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        return result
    matching = next(
        (
            job
            for job in jobs
            if isinstance(job, Mapping) and job.get("job_id") == active_job_id
        ),
        None,
    )
    if not isinstance(matching, Mapping):
        return result
    current_status = matching.get("status")
    if isinstance(current_status, str) and current_status.strip():
        result["active_background_job_status"] = current_status.strip()
    return result


def _completion_marker_warnings(marker: Mapping[str, Any]) -> list[str]:
    if marker.get("exists") and marker.get("fresh") is not True:
        reasons = marker.get("stale_reasons")
        if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)) and reasons:
            return ["Completion marker is stale and will be ignored: " + ", ".join(str(reason) for reason in reasons) + "."]
        return ["Completion marker is stale and will be ignored."]
    return []


def _status_next_steps(state: Mapping[str, Any]) -> list[str]:
    steps: list[str] = []
    problems = state.get("configuration_problems")
    if isinstance(problems, Sequence) and not isinstance(problems, (str, bytes)):
        if any(
            isinstance(problem, Mapping) and str(problem.get("code") or "") == "manual_plan_change_detected"
            for problem in problems
        ):
            steps.append(
                "Review the active PLAN.md edit, then run `loopplane acknowledge-plan --project <project>` "
                "or submit an approved change request."
            )
    scheduler = state.get("scheduler")
    if isinstance(scheduler, Mapping) and str(scheduler.get("last_action") or "") == "wait_config":
        selected = scheduler.get("selected")
        if "runner" in json.dumps(selected, sort_keys=True).lower():
            steps.append("Run `loopplane doctor-agent --all` and configure an available runner with `loopplane configure-agent`.")
    return _dedupe_text(steps)


def _dedupe_text(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


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


def _failure(
    *,
    project: Path,
    workflow_id: str,
    status: str,
    message: str,
    recovery_actions: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "message": message,
        "errors": [message],
        "warnings": [],
    }
    if recovery_actions:
        result["recovery_actions"] = list(recovery_actions)
    if extra:
        result.update(dict(extra))
    return result


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, Mapping):
            records.append(dict(data))
    return records


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _tail_event_records(events_dir: Path, *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("*.jsonl")):
        for record in _read_jsonl(path):
            record["_segment"] = path.name
            records.append(record)
    return records[-limit:]


def _latest_response(responses: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not responses:
        return None
    return sorted((dict(response) for response in responses), key=lambda item: str(item.get("handled_at") or ""))[-1]


def _record_id(record: Mapping[str, Any] | None) -> str | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("request_id") or record.get("control_request_id") or record.get("id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _json_safe_object(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_object(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_object(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
