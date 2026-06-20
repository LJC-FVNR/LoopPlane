from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.approval import approval_record_status
from runtime.change_requests import (
    CHANGE_REQUEST_RESPONSES_FILENAME,
    CHANGE_REQUESTS_FILENAME,
)
from runtime.control import (
    CONTROL_REQUESTS_FILENAME,
    CONTROL_RESPONSES_FILENAME,
    control_request_statuses,
)
from runtime.human_summaries import (
    load_phase_human_summary,
    load_task_human_summary,
    phase_human_summary_source_hash,
    task_human_summary_source_hash,
)
from runtime.path_resolution import (
    PathSerializationError,
    WorkflowPathError,
    WorkflowPaths,
    load_workflow_config,
    serialize_project_path,
)
from runtime.plan_objectives import parse_plan_objectives
from runtime.reconciliation import PlanTask, parse_plan_tasks
from runtime.scheduler import SCHEMA_VERSION, completion_marker_status, load_event_log_projection
from runtime.version_control import run_git_doctor
from runtime.workspace_identity import relative_root_value
from runtime.workflow_lifecycle import WorkflowLifecycleError, ensure_compatibility_workflow_metadata


READ_MODEL_JSON_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "metrics.json",
    "version_control_status.json",
)
READ_MODEL_JSONL_FILES = (
    "dashboard_feed.jsonl",
    "run_summaries.jsonl",
)
READ_MODEL_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "dashboard_feed.jsonl",
    "run_summaries.jsonl",
    "metrics.json",
    "version_control_status.json",
)


def _read_model_limit_from_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return max(0, int(raw_value))
    except ValueError:
        return default


CHECKBOX_STATUSES = {
    "x": "done",
    " ": "pending",
    "~": "partial",
    "!": "blocked",
    "-": "skipped",
}
TERMINAL_FAILURE_STATUSES = frozenset({"recovered", "waived"})
NODE_DETAIL_TEXT_LIMIT = 4000
NODE_DETAIL_LOG_LIMIT = 2500
NODE_DETAIL_ITEM_LIMIT = 12
NODE_DETAIL_REDACTION_EXTRA_BYTES = 8192
IMAGE_PREVIEW_SUFFIXES = frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"})
DEFAULT_DASHBOARD_MAX_EVENTS = _read_model_limit_from_env("LOOPPLANE_MAX_DASHBOARD_EVENTS", 200)
DASHBOARD_EVENT_NODE_LIMIT = _read_model_limit_from_env("LOOPPLANE_DASHBOARD_EVENT_NODE_LIMIT", DEFAULT_DASHBOARD_MAX_EVENTS)
DASHBOARD_FEED_RECORD_LIMIT = _read_model_limit_from_env("LOOPPLANE_DASHBOARD_FEED_RECORD_LIMIT", DEFAULT_DASHBOARD_MAX_EVENTS)
NODE_DETAIL_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
PRIVATE_KEY_REDACTION_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
BEARER_REDACTION_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
SECRET_WORD_REDACTION_RE = re.compile(r"(?i)\b(access token|api key|password|secret|token)(\s+)([A-Za-z0-9._~+/=-]{8,})")
SECRET_ASSIGNMENT_REDACTION_RE = re.compile(r"(?i)\b(API_KEY|SECRET|TOKEN|PASSWORD)\b(\s*[:=]\s*)([^\s,;]+)")


@dataclass(frozen=True)
class TaskContext:
    task: PlanTask
    latest: Mapping[str, Any] | None
    latest_path: Path
    project_root: Path
    paths: WorkflowPaths
    validation: Mapping[str, Any] | None
    validation_path: Path | None


@dataclass(frozen=True)
class PlanSource:
    kind: str
    path: Path
    value: str
    text: str
    active_plan_value: str
    readiness_report: Mapping[str, Any] | None = None
    audit_report: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = ()


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def rebuild_read_models(
    project_root: Path | str,
    *,
    write: bool = True,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        ensure_compatibility_workflow_metadata(project, updated_by="loopplane read-models")
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
        plan_source = _select_plan_source(project, paths, workflow_config)
        plan_text = plan_source.text
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError) as error:
        return _failure(project=project, started_at=started_at, message=f"Unable to load workflow inputs: {error}")

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    dashboard_event_limit = _dashboard_event_limit(paths, explicit_limit=max_dashboard_events)
    generated_at = utc_timestamp()
    plan_metadata = _plan_metadata(plan_text)
    workflow_title = _workflow_title_from_metadata(plan_metadata, workflow_id=workflow_id)
    tasks = list(parse_plan_tasks(plan_text).values())
    objectives, objective_parse_errors = parse_plan_objectives(plan_text)
    objective_model = _objective_model(paths, objectives=objectives, parse_errors=objective_parse_errors)
    latest_contexts = _task_contexts(project, paths, tasks)
    validation_records = _collect_validation_records(paths)
    validation_manifest = _validation_manifest(project, latest_contexts, validation_records)
    events = _read_event_records(paths)
    event_projection = load_event_log_projection(paths)
    event_state = event_projection.get("state") if isinstance(event_projection, Mapping) else {}
    if not isinstance(event_state, Mapping):
        event_state = {}
    last_event = event_state.get("latest_event")
    if not isinstance(last_event, Mapping):
        last_event = _compact_event(events[-1]) if events else None
    last_event_seq = _event_sequence(last_event) if isinstance(last_event, Mapping) else None
    source_event_id = _event_id(last_event) if isinstance(last_event, Mapping) else None
    active_leases = _read_active_leases(paths)
    source_hashes = _source_hashes(
        paths,
        plan_source=plan_source,
        events=events,
        last_event=last_event,
        validation_manifest=validation_manifest,
        active_leases=active_leases,
    )
    plan_source_record = _plan_source_record(project, paths, plan_source)
    common = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "generated_at": generated_at,
        "source_hashes": source_hashes,
        "last_event_seq": last_event_seq,
        "source_event_id": source_event_id,
        "plan_source": plan_source_record,
    }
    if workflow_title:
        common["workflow_title"] = workflow_title

    runtime_state = _read_json_object(paths.runtime_dir / "state.json", default={})
    failure_registry = _read_failure_registry(paths, workflow_id=workflow_id)
    expansion_registry = _read_expansion_registry(paths, workflow_id=workflow_id)
    expansion_index = _expansion_provenance_index(expansion_registry)
    control_requests = _read_jsonl(paths.runtime_dir / CONTROL_REQUESTS_FILENAME)
    control_responses = _read_jsonl(paths.runtime_dir / CONTROL_RESPONSES_FILENAME)
    approvals = _read_jsonl(paths.runtime_dir / "human_approval_requests.jsonl")
    approval_responses = _read_jsonl(paths.runtime_dir / "human_approval_responses.jsonl")
    change_requests = _read_jsonl(paths.requests_dir / CHANGE_REQUESTS_FILENAME)
    change_request_responses = _read_jsonl(paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME)
    node_summaries = _collect_node_summaries(paths)
    agent_statuses = _collect_agent_statuses(paths)
    task_summaries = _task_summaries(
        latest_contexts,
        failures=failure_registry.get("failures", []),
        expansion_tasks=_mapping_or_empty(expansion_index.get("tasks")),
    )
    run_summaries = _build_run_summaries(
        common=common,
        paths=paths,
        events=events,
        node_summaries=node_summaries,
        agent_statuses=agent_statuses,
        validation_records=validation_records,
        active_leases=active_leases,
    )

    json_models = {
        "workflow_status.json": _workflow_status_model(
            common=common,
            runtime_state=runtime_state,
            task_summaries=task_summaries,
            active_leases=active_leases,
            control_requests=control_requests,
            control_responses=control_responses,
            approvals=approvals,
            approval_responses=approval_responses,
            change_requests=change_requests,
            change_request_responses=change_request_responses,
            failure_registry=failure_registry,
            expansion_registry=expansion_registry,
            objective_model=objective_model,
            completion_marker=_completion_marker_model(project, paths),
        ),
        "plan_index.json": _plan_index_model(
            common=common,
            paths=paths,
            plan_source=plan_source,
            tasks=tasks,
            task_summaries=task_summaries,
            objective_model=objective_model,
            expansion_phases=_mapping_or_empty(expansion_index.get("phases")),
        ),
        "workflow_graph.json": _workflow_graph_model(
            common=common,
            events=events,
            event_limit=dashboard_event_limit,
            node_summaries=node_summaries,
            agent_statuses=agent_statuses,
            validation_records=validation_records,
            active_leases=active_leases,
            task_summaries=task_summaries,
            objective_model=objective_model,
            event_total_count=len(events),
        ),
        "metrics.json": _metrics_model(
            common=common,
            project=project,
            workflow_config=workflow_config,
            runtime_state=runtime_state,
            task_summaries=task_summaries,
            run_summaries=run_summaries,
            validation_records=validation_records,
            failure_registry=failure_registry,
            event_projection=event_projection,
            expansion_registry=expansion_registry,
            objective_model=objective_model,
            change_requests=change_requests,
            change_request_responses=change_request_responses,
            approvals=approvals,
            approval_responses=approval_responses,
        ),
        "version_control_status.json": _version_control_status_model(
            common=common,
            project=project,
            paths=paths,
        ),
    }
    jsonl_models = {
        "dashboard_feed.jsonl": _dashboard_feed_records(common=common, events=events, event_limit=dashboard_event_limit),
        "run_summaries.jsonl": run_summaries,
    }
    validation = validate_read_models(json_models, jsonl_models)
    if validation["status"] != "pass":
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "schema_validation_failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "read_models_dir": _path_for_record(project, paths.read_models_dir),
            "event_replay": _event_replay_summary(event_projection, project=project),
            "max_dashboard_events": dashboard_event_limit,
            "schema_validation": validation,
            "written_files": [],
            "errors": list(validation["errors"]),
            "warnings": list(plan_source.warnings),
        }

    written: list[str] = []
    if write:
        paths.read_models_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in json_models.items():
            _atomic_write_json(paths.read_models_dir / filename, payload)
            written.append(_path_for_record(project, paths.read_models_dir / filename))
        for filename, records in jsonl_models.items():
            _atomic_write_jsonl(paths.read_models_dir / filename, records)
            written.append(_path_for_record(project, paths.read_models_dir / filename))

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "rebuilt",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "read_models_dir": _path_for_record(project, paths.read_models_dir),
        "read_model_files": list(READ_MODEL_FILES),
        "written_files": sorted(written),
        "event_replay": _event_replay_summary(event_projection, project=project),
        "max_dashboard_events": dashboard_event_limit,
        "schema_validation": validation,
        "source_hashes": source_hashes,
        "errors": [],
        "warnings": list(plan_source.warnings),
    }


def read_model_rebuild_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def format_read_model_rebuild_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane rebuild-read-models: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"read_models_dir: {result.get('read_models_dir') or 'unknown'}",
    ]
    event_replay = result.get("event_replay")
    if isinstance(event_replay, Mapping):
        lines.append(f"snapshot: {event_replay.get('snapshot_id')}")
        lines.append(f"events_replayed: {event_replay.get('events_replayed')}")
    written = result.get("written_files")
    if isinstance(written, Sequence) and not isinstance(written, (str, bytes)) and written:
        lines.append("written_files:")
        lines.extend(f"  - {path}" for path in written)
    schema_validation = result.get("schema_validation")
    if isinstance(schema_validation, Mapping):
        lines.append(f"schema_validation: {schema_validation.get('status', 'unknown')}")
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _select_plan_source(project: Path, paths: WorkflowPaths, workflow_config: Mapping[str, Any]) -> PlanSource:
    active_plan_value = paths.value("plan_file")
    active_text: str | None = None
    active_error: OSError | None = None
    try:
        active_text = paths.plan_file.read_text(encoding="utf-8")
    except OSError as error:
        active_error = error

    readiness_report = _read_json_object(paths.planning_dir / "plan_readiness_report.json", default={})
    audit_report = _read_json_object(paths.planning_dir / "audit_report.json", default={})
    draft_path = paths.planning_dir / "PLAN_DRAFT.md"
    draft_ready = _planning_draft_ready(
        workflow_config,
        draft_path=draft_path,
        readiness_report=readiness_report,
        audit_report=audit_report,
    )
    if draft_ready:
        try:
            draft_text = draft_path.read_text(encoding="utf-8")
        except OSError:
            draft_text = None
        if draft_text is not None and _should_use_planning_draft(active_text, draft_text):
            value = _path_for_record(project, draft_path) or draft_path.as_posix()
            return PlanSource(
                kind="planning_draft",
                path=draft_path,
                value=value,
                text=draft_text,
                active_plan_value=active_plan_value,
                readiness_report=readiness_report if isinstance(readiness_report, Mapping) else None,
                audit_report=audit_report if isinstance(audit_report, Mapping) and audit_report else None,
            )

    if active_text is None:
        if active_error is not None:
            raise active_error
        raise FileNotFoundError(paths.plan_file)
    return PlanSource(
        kind="active",
        path=paths.plan_file,
        value=active_plan_value,
        text=active_text,
        active_plan_value=active_plan_value,
    )


def _planning_draft_ready(
    workflow_config: Mapping[str, Any],
    *,
    draft_path: Path,
    readiness_report: Any,
    audit_report: Any,
) -> bool:
    if not draft_path.exists() or not isinstance(readiness_report, Mapping):
        return False
    ready_for_audit = readiness_report.get("ready_for_audit") is True or readiness_report.get("ready") is True
    ready_for_activation = (
        readiness_report.get("ready_for_activation") is True
        or str(readiness_report.get("status") or "") == "ready_for_activation"
    )
    if not (ready_for_audit or ready_for_activation):
        return False
    planning_config = workflow_config.get("planning")
    auditor_required = isinstance(planning_config, Mapping) and bool(planning_config.get("auditor_required", False))
    if not auditor_required:
        return True
    if not isinstance(audit_report, Mapping):
        return False
    return audit_report.get("passed") is True or audit_report.get("ready_for_activation") is True


def _should_use_planning_draft(active_text: str | None, draft_text: str) -> bool:
    if not draft_text.strip():
        return False
    if active_text is None:
        return True
    if not parse_plan_tasks(active_text):
        return True
    active_metadata = _plan_metadata(active_text)
    return str(active_metadata.get("active") or "").strip().lower() != "true"


def _plan_source_record(project: Path, paths: WorkflowPaths, plan_source: PlanSource) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": plan_source.kind,
        "path": plan_source.value,
        "active_plan_file": plan_source.active_plan_value,
    }
    if plan_source.kind != "active":
        record["selected_before_activation"] = True
    if plan_source.readiness_report is not None:
        record["readiness"] = _compact_plan_readiness(plan_source.readiness_report, project=project)
    if plan_source.audit_report is not None:
        record["audit"] = _compact_plan_audit(plan_source.audit_report, project=project)
    if plan_source.warnings:
        record["warnings"] = list(plan_source.warnings)
    active_path = paths.plan_file
    if active_path.resolve() != plan_source.path.resolve():
        record["active_path"] = paths.value("plan_file")
    return record


def _compact_plan_readiness(report: Mapping[str, Any], *, project: Path) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "ready_for_audit": report.get("ready_for_audit"),
        "ready_for_activation": report.get("ready_for_activation"),
        "activation_blocked_by": list(report.get("activation_blocked_by") or []),
        "plan_draft_path": _record_path_value(project, report.get("plan_draft_path")),
        "generated_at": report.get("generated_at"),
    }


def _compact_plan_audit(report: Mapping[str, Any], *, project: Path) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "passed": report.get("passed"),
        "ready_for_activation": report.get("ready_for_activation"),
        "blocking_findings": list(report.get("blocking_findings") or []),
        "audit_report_path": _record_path_value(project, report.get("audit_report_path")),
        "generated_at": report.get("generated_at"),
    }


def _record_path_value(project: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return _path_for_record(project, path) or path.as_posix()
    return path.as_posix()


def validate_read_models(
    json_models: Mapping[str, Mapping[str, Any]],
    jsonl_models: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    errors: list[str] = []
    checked: list[str] = []
    for filename in READ_MODEL_JSON_FILES:
        checked.append(filename)
        payload = json_models.get(filename)
        if not isinstance(payload, Mapping):
            errors.append(f"{filename}: missing JSON object payload")
            continue
        for field in ("schema_version", "workflow_id", "generated_at", "source_hashes"):
            if field not in payload:
                errors.append(f"{filename}: missing common field {field}")
        if "last_event_seq" not in payload and "source_event_id" not in payload:
            errors.append(f"{filename}: missing event reference field last_event_seq or source_event_id")
        if payload.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"{filename}: schema_version must be {SCHEMA_VERSION}")

    _require_mapping(json_models, "workflow_status.json", "progress", errors)
    _require_sequence(json_models, "workflow_status.json", "requires_attention", errors)
    _require_mapping(json_models, "plan_index.json", "summary", errors)
    _require_sequence(json_models, "plan_index.json", "phases", errors)
    _require_sequence(json_models, "workflow_graph.json", "nodes", errors)
    _require_sequence(json_models, "workflow_graph.json", "edges", errors)
    _require_mapping(json_models, "metrics.json", "counts", errors)
    _require_mapping(json_models, "version_control_status.json", "repository", errors)

    for filename in READ_MODEL_JSONL_FILES:
        checked.append(filename)
        records = jsonl_models.get(filename)
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            errors.append(f"{filename}: missing JSONL record sequence")
            continue
        for index, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                errors.append(f"{filename}:{index}: record must be a JSON object")
                continue
            for field in ("schema_version", "workflow_id", "generated_at", "source_hashes"):
                if field not in record:
                    errors.append(f"{filename}:{index}: missing common field {field}")
            if "source_event_seq" not in record and "source_event_id" not in record:
                errors.append(f"{filename}:{index}: missing event reference field source_event_seq or source_event_id")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not errors else "fail",
        "checked_files": checked,
        "errors": errors,
    }


def _plan_metadata(plan_text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    in_metadata = False
    for raw_line in plan_text.splitlines():
        line = raw_line.strip()
        if line == "## Metadata":
            in_metadata = True
            continue
        if in_metadata and line.startswith("## "):
            break
        if not in_metadata:
            continue
        match = re.match(r"^-\s+([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key and value:
            metadata[key] = value
    return metadata


def _workflow_title_from_metadata(metadata: Mapping[str, str], *, workflow_id: str) -> str | None:
    title = str(metadata.get("workflow_title") or "").strip()
    if not title or title == workflow_id:
        return None
    return re.sub(r"\s+", " ", title)[:120]


def _workflow_status_model(
    *,
    common: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
    task_summaries: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
    control_requests: Sequence[Mapping[str, Any]],
    control_responses: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
    change_requests: Sequence[Mapping[str, Any]],
    change_request_responses: Sequence[Mapping[str, Any]],
    failure_registry: Mapping[str, Any],
    expansion_registry: Mapping[str, Any],
    objective_model: Mapping[str, Any],
    completion_marker: Mapping[str, Any],
) -> dict[str, Any]:
    progress = _progress(task_summaries)
    state_status = str(runtime_state.get("status") or "").strip()
    base_status = state_status or ("completed" if progress["total_tasks"] and progress["completed_tasks"] == progress["total_tasks"] else "running")
    terminal = _workflow_status_blocks_active_lease(base_status)
    active_lease = None if terminal else _primary_active_lease(active_leases)
    status, status_source = _dashboard_workflow_status(base_status, active_lease)
    terminal = _workflow_status_blocks_active_lease(status)
    active_task_id = None if terminal else _active_task_id(runtime_state, task_summaries, active_lease)
    active_run_id = None if terminal else _first_string(
        _mapping_value(active_lease, "run_id"),
        _nested_mapping_value(runtime_state, "scheduler", "active_run_id"),
        runtime_state.get("active_run_id"),
    )
    current_activity = _current_activity(status, active_task_id, active_run_id, active_lease)
    return {
        **dict(common),
        "status": status,
        "raw_runtime_status": state_status or None,
        "status_source": status_source,
        "phase": _workflow_phase(runtime_state, task_summaries),
        "active_task_id": active_task_id,
        "active_run_id": active_run_id,
        "progress": progress,
        "current_activity": current_activity,
        "control": _control_summary(control_requests, control_responses),
        "change_requests": _change_request_summary(
            change_requests,
            change_request_responses,
            approvals,
            approval_responses,
        ),
        "requires_attention": _requires_attention(runtime_state, approvals, approval_responses, failure_registry),
        "self_expansion": _self_expansion_summary(expansion_registry, objective_model=objective_model),
        "objective_progress": dict(objective_model.get("summary") or {}),
        "objectives": list(objective_model.get("objectives") or []),
        "completion_marker": dict(completion_marker),
    }


def _workflow_status_blocks_active_lease(status: str) -> bool:
    return str(status or "").strip().lower() in {
        "completed",
        "failed",
        "stopped",
        "paused",
        "archived",
        "read_only_imported",
    }


def _primary_active_lease(active_leases: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not active_leases:
        return None
    return max(active_leases, key=_active_lease_sort_key)


def _active_lease_sort_key(lease: Mapping[str, Any]) -> tuple[str, str]:
    timestamp = _first_string(
        lease.get("heartbeat_at"),
        lease.get("started_at"),
        lease.get("prepared_at"),
        lease.get("created_at"),
    )
    return timestamp or "", str(lease.get("run_id") or "")


def _dashboard_workflow_status(base_status: str, active_lease: Mapping[str, Any] | None) -> tuple[str, str]:
    if active_lease is not None and _status_like_active(active_lease.get("status") or "running"):
        return "running", "active_run_lease"
    return base_status, "runtime_state" if base_status else "derived"


def _completion_marker_model(project: Path, paths: WorkflowPaths) -> dict[str, Any]:
    try:
        marker = dict(completion_marker_status(paths))
    except Exception as error:
        return {
            "exists": False,
            "fresh": False,
            "path": None,
            "stale_reasons": [f"status_error:{error.__class__.__name__}"],
        }
    path_value = marker.get("path")
    if isinstance(path_value, str) and path_value:
        path = Path(path_value)
        if path.is_absolute():
            marker["path"] = _path_for_record(project, path)
    return marker


def _objective_model(paths: WorkflowPaths, *, objectives: Sequence[Any], parse_errors: Sequence[str]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    reports: dict[str, dict[str, Any]] = {}
    try:
        from runtime.objective_verification import objective_report_path, objective_result_is_closed, objective_result_is_expandable, objective_results_by_id
    except Exception:
        objective_report_path = None  # type: ignore[assignment]
        objective_result_is_closed = lambda result: False  # type: ignore[assignment]
        objective_result_is_expandable = lambda result: False  # type: ignore[assignment]
        objective_results_by_id = lambda report: {}  # type: ignore[assignment]
    for objective in objectives:
        objective_dict = objective.to_dict()
        report_path = None
        report = {}
        result = None
        if objective_report_path is not None:
            report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id)
            report = _read_json_object(report_path, default={})
            report_key = report_path.as_posix()
            if report_key not in reports:
                reports[report_key] = {
                    "path": _safe_dashboard_path(paths, report_path),
                    "exists": report_path.exists(),
                    "scope": objective.scope,
                    "phase_id": objective.phase_id,
                    "status": report.get("status") if isinstance(report, Mapping) else None,
                    "verified_at": report.get("verified_at") if isinstance(report, Mapping) else None,
                }
            if isinstance(report, Mapping):
                result = objective_results_by_id(report).get(objective.objective_id)
        closed = bool(result and objective_result_is_closed(result)) or objective.status in {"x", "-"}
        expandable = bool(result and objective_result_is_expandable(result)) and objective.status != "!"
        result_status = str(_mapping_value(result, "status") or "").lower() if isinstance(result, Mapping) else ""
        result_verdict = str(_mapping_value(result, "verdict") or "").lower() if isinstance(result, Mapping) else ""
        if closed:
            status = "closed"
        elif objective.status == "!" or result_status == "blocked" or result_verdict == "blocked_external":
            status = "objective_unresolved"
        elif expandable:
            status = "needs_expansion"
        else:
            status = "needs_verification"
        item = {
            **objective_dict,
            "objective_id": objective.objective_id,
            "status": status,
            "plan_status": objective.status_name,
            "closed": closed,
            "expandable": expandable,
            "report_path": _safe_dashboard_path(paths, report_path) if isinstance(report_path, Path) else None,
            "report_status": report.get("status") if isinstance(report, Mapping) else None,
            "verified_at": report.get("verified_at") if isinstance(report, Mapping) else None,
            "result": dict(result) if isinstance(result, Mapping) else None,
        }
        items.append(item)
    closed_count = sum(1 for item in items if item.get("closed"))
    expandable_count = sum(1 for item in items if item.get("expandable") and not item.get("closed"))
    return {
        "schema_version": SCHEMA_VERSION,
        "parse_errors": list(parse_errors),
        "objectives": items,
        "reports": list(reports.values()),
        "summary": {
            "total": len(items),
            "closed": closed_count,
            "open": len(items) - closed_count,
            "needs_expansion": expandable_count,
            "needs_verification": sum(1 for item in items if item.get("status") == "needs_verification"),
            "objective_unresolved": sum(1 for item in items if item.get("status") == "objective_unresolved"),
            "parse_error_count": len(parse_errors),
        },
    }


def _objectives_for_phase_model(objective_model: Mapping[str, Any], phase_title: str) -> list[dict[str, Any]]:
    phase_id = _phase_id(phase_title)
    result: list[dict[str, Any]] = []
    for objective in objective_model.get("objectives") or []:
        if not isinstance(objective, Mapping):
            continue
        if objective.get("scope") != "phase":
            continue
        if str(objective.get("phase_title") or "") == phase_title or str(objective.get("phase_id") or "") == phase_id:
            result.append(dict(objective))
    return result


def _plan_index_model(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    plan_source: PlanSource,
    tasks: Sequence[PlanTask],
    task_summaries: Sequence[Mapping[str, Any]],
    objective_model: Mapping[str, Any],
    expansion_phases: Mapping[str, Any],
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    by_phase: dict[str, list[Mapping[str, Any]]] = {}
    plan_tasks_by_phase: dict[str, list[PlanTask]] = {}
    for task in tasks:
        plan_tasks_by_phase.setdefault(task.phase or "Unphased", []).append(task)
    for task in task_summaries:
        by_phase.setdefault(str(task.get("phase") or "Unphased"), []).append(task)
    for phase_title, phase_tasks in by_phase.items():
        phase_progress = _progress(phase_tasks)
        source_phase_tasks = plan_tasks_by_phase.get(phase_title, [])
        phase_id = _phase_id(phase_title)
        phase_expansion = _mapping_or_empty(expansion_phases.get(phase_id))
        phase_record = {
            "phase_id": phase_id,
            "title": phase_title,
            "status": _phase_status(phase_progress),
            "human_summary": _fresh_human_summary(
                load_phase_human_summary(paths.project_root, paths, phase_title),
                expected_source_hash=phase_human_summary_source_hash(paths.project_root, paths, phase_title, source_phase_tasks),
            ),
            "objectives": _objectives_for_phase_model(objective_model, phase_title),
            "tasks": [_plan_index_task(task) for task in phase_tasks],
        }
        if phase_expansion:
            phase_record.update(_expanded_fields(phase_expansion))
        phases.append(
            phase_record
        )
    return {
        **dict(common),
        "plan_file": plan_source.value,
        "active_plan_file": paths.value("plan_file"),
        "summary": {
            "total": len(task_summaries),
            "done": sum(1 for task in task_summaries if task.get("status") == "done"),
            "partial": sum(1 for task in task_summaries if task.get("status") == "partial"),
            "pending": sum(1 for task in task_summaries if task.get("status") == "pending"),
            "blocked": sum(1 for task in task_summaries if task.get("status") == "blocked"),
            "skipped": sum(1 for task in task_summaries if task.get("status") == "skipped"),
            "progress_percent": _progress_percent(sum(1 for task in task_summaries if task.get("status") == "done"), len(task_summaries)),
            "objectives": dict(objective_model.get("summary") or {}),
        },
        "objectives": list(objective_model.get("objectives") or []),
        "objective_reports": list(objective_model.get("reports") or []),
        "phases": phases,
        "tasks": [_plan_index_task(task) for task in task_summaries],
    }


def _workflow_graph_model(
    *,
    common: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    event_limit: int,
    node_summaries: Sequence[Mapping[str, Any]],
    agent_statuses: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
    task_summaries: Sequence[Mapping[str, Any]],
    objective_model: Mapping[str, Any],
    event_total_count: int,
) -> dict[str, Any]:
    graph_events = _recent_dashboard_events(events, limit=event_limit)
    nodes_by_id: dict[str, dict[str, Any]] = {}
    summary_run_ids = {str(summary.get("run_id") or "") for summary in node_summaries if summary.get("run_id")}
    validation_keys = {
        (
            str(validation.get("run_id") or ""),
            str(validation.get("primary_task_id") or validation.get("task_id") or ""),
        )
        for validation in validation_records
    }
    for summary in node_summaries:
        node = _node_from_summary(summary)
        nodes_by_id.setdefault(node["node_id"], node)
    for status in agent_statuses:
        run_id = str(status.get("run_id") or status.get("_derived_run_id") or "")
        role = str(status.get("role") or status.get("_derived_role") or "").strip().lower()
        task_id = str(status.get("task_id") or status.get("primary_task_id") or status.get("_derived_task_id") or "")
        parent_run_id = str(status.get("_parent_run_id") or "")
        if not run_id:
            continue
        if role in {"", "worker", "recovery_worker"} and run_id in summary_run_ids:
            continue
        if role == "validator" and ((parent_run_id, task_id) in validation_keys or (parent_run_id, "") in validation_keys):
            continue
        node = _node_from_agent_status(status)
        nodes_by_id.setdefault(node["node_id"], node)
    for validation in validation_records:
        node = _node_from_validation(validation)
        nodes_by_id.setdefault(node["node_id"], node)
    for event in graph_events:
        node = _node_from_event(event)
        nodes_by_id.setdefault(node["node_id"], node)
    for lease in active_leases:
        node = _node_from_active_lease(lease)
        existing = nodes_by_id.get(node["node_id"])
        if existing:
            merged = dict(existing)
            merged.update({key: value for key, value in node.items() if value not in (None, "", [])})
            node = merged
        nodes_by_id[node["node_id"]] = node
    for objective in objective_model.get("objectives") or []:
        if not isinstance(objective, Mapping):
            continue
        node = _node_from_objective(objective)
        nodes_by_id.setdefault(node["node_id"], node)
    _propagate_run_graph_metadata(nodes_by_id)

    edges: list[dict[str, Any]] = []
    for validation in validation_records:
        run_id = str(validation.get("run_id") or "")
        if not run_id:
            continue
        source = _preferred_run_node_id(nodes_by_id, run_id)
        target = _validation_node_id(run_id)
        if source not in nodes_by_id:
            nodes_by_id[source] = {
                "node_id": source,
                "type": "run",
                "status": "unknown",
                "title": f"Run {run_id}",
                "run_id": run_id,
                "summary": {"one_line": "Run inferred from validation.", "highlights": [], "risks": []},
            }
        edges.append({"edge_id": f"edge_{source}_to_{target}", "source": source, "target": target, "type": "validated_by"})
    previous_event_node: str | None = None
    for event in graph_events:
        node_id = _event_node_id(event)
        if previous_event_node is not None:
            edges.append(
                {
                    "edge_id": f"edge_{previous_event_node}_to_{node_id}",
                    "source": previous_event_node,
                    "target": node_id,
                    "type": "event_sequence",
                }
            )
        previous_event_node = node_id

    task_lookup = {
        str(task.get("task_id") or ""): task
        for task in task_summaries
        if str(task.get("task_id") or "")
    }
    nodes = [_enrich_node_with_task(node, task_lookup) for node in nodes_by_id.values()]
    return {
        **dict(common),
        "event_window": {
            "total_events": event_total_count,
            "visible_event_nodes": len(graph_events),
            "limit": event_limit,
            "truncated": event_total_count > len(graph_events),
        },
        "nodes": sorted(nodes, key=_graph_node_sort_key),
        "edges": _dedupe_edges(edges),
    }


def _preferred_run_node_id(nodes_by_id: Mapping[str, Mapping[str, Any]], run_id: str) -> str:
    for node_id, node in nodes_by_id.items():
        if str(node.get("run_id") or "") != run_id:
            continue
        node_type = str(node.get("type") or "").strip().lower()
        if node_type not in {"event", "validation"}:
            return str(node_id)
    return _run_node_id(run_id)


def _propagate_run_graph_metadata(nodes_by_id: Mapping[str, dict[str, Any]]) -> None:
    metadata_by_run: dict[str, dict[str, Any]] = {}
    metadata_keys = ("phase_id", "phase", "objective_phase_id", "objective_scope")
    for node in nodes_by_id.values():
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        metadata = metadata_by_run.setdefault(run_id, {})
        for key in metadata_keys:
            value = node.get(key)
            if value not in (None, "", []):
                metadata.setdefault(key, value)
        target_objective_ids = node.get("target_objective_ids")
        if isinstance(target_objective_ids, Sequence) and not isinstance(target_objective_ids, (str, bytes)) and target_objective_ids:
            metadata.setdefault("target_objective_ids", list(target_objective_ids))
    for node in nodes_by_id.values():
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        metadata = metadata_by_run.get(run_id) or {}
        for key, value in metadata.items():
            if node.get(key) in (None, "", []):
                node[key] = value


def _metrics_model(
    *,
    common: Mapping[str, Any],
    project: Path,
    workflow_config: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
    task_summaries: Sequence[Mapping[str, Any]],
    run_summaries: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    failure_registry: Mapping[str, Any],
    event_projection: Mapping[str, Any],
    expansion_registry: Mapping[str, Any],
    objective_model: Mapping[str, Any],
    change_requests: Sequence[Mapping[str, Any]],
    change_request_responses: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    done = sum(1 for task in task_summaries if task.get("status") == "done")
    failures = failure_registry.get("failures") if isinstance(failure_registry.get("failures"), Sequence) else []
    raw_proposals = expansion_registry.get("proposals") if isinstance(expansion_registry.get("proposals"), Sequence) else []
    objective_statuses = _objective_statuses_by_id(objective_model)
    proposals = [
        _proposal_with_current_objective_resolution(proposal, objective_statuses=objective_statuses)
        for proposal in raw_proposals
        if isinstance(proposal, Mapping)
    ]
    change_request_records = _change_request_statuses_for_read_model(
        change_requests,
        change_request_responses,
        approvals,
        approval_responses,
    )
    return {
        **dict(common),
        "counts": {
            "tasks_total": len(task_summaries),
            "tasks_done": done,
            "tasks_partial": sum(1 for task in task_summaries if task.get("status") == "partial"),
            "tasks_blocked": sum(1 for task in task_summaries if task.get("status") == "blocked"),
            "tasks_skipped": sum(1 for task in task_summaries if task.get("status") == "skipped"),
            "runs_total": len({str(record.get("run_id")) for record in run_summaries if record.get("run_id")}),
            "recoveries_total": sum(1 for record in run_summaries if str(record.get("role") or "") == "recovery_worker"),
            "validations_failed": sum(1 for record in validation_records if str(record.get("status") or "") in {"fail", "blocked"}),
            "failures_total": len([failure for failure in failures if isinstance(failure, Mapping)]),
            "self_expansion_proposals_total": len(proposals),
            "self_expansion_applied": sum(
                1 for proposal in proposals if proposal.get("status") == "applied"
            ),
            "self_expansion_pending_resolution": sum(
                1
                for proposal in proposals
                if proposal.get("failure_resolution_status") == "pending_evidence"
                or proposal.get("objective_resolution_status") == "followup_pending"
            ),
            "change_requests_total": len(change_request_records),
            "change_requests_pending": sum(
                1
                for record in change_request_records
                if record.get("status") in {"pending_review", "needs_user_approval", "approved"}
            ),
            "change_requests_applied": sum(1 for record in change_request_records if record.get("status") == "applied"),
        },
        "durations": {
            "workflow_elapsed_seconds": _elapsed_seconds(workflow_config.get("created_at") or runtime_state.get("initialized_at")),
            "active_worker_seconds": _active_worker_seconds(run_summaries),
        },
        "event_replay": _event_replay_summary(event_projection, project=project),
    }


def _version_control_status_model(
    *,
    common: Mapping[str, Any],
    project: Path,
    paths: WorkflowPaths,
) -> dict[str, Any]:
    doctor = run_git_doctor(project)
    latest_checkpoint = _latest_checkpoint(paths.runtime_dir / "git_checkpoints.jsonl")
    return {
        **dict(common),
        "status": doctor.get("status"),
        "ok": bool(doctor.get("ok")),
        "provider": "git",
        "git_available": bool(_mapping_value(doctor.get("git"), "available")),
        "repository": _sanitized_repository(doctor.get("repository"), project=project),
        "configuration": _sanitized_version_control_configuration(doctor.get("configuration"), project=project),
        "latest_checkpoint": latest_checkpoint,
        "problem": _version_control_problem(doctor),
        "warnings": list(doctor.get("warnings") or []),
        "errors": list(doctor.get("errors") or []),
    }


def _dashboard_feed_records(
    *,
    common: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    event_limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in _recent_dashboard_events(events, limit=event_limit):
        payload = _event_payload(event)
        subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
        ui = event.get("ui") if isinstance(event.get("ui"), Mapping) else {}
        record = {
            "schema_version": common["schema_version"],
            "workflow_id": common["workflow_id"],
            "generated_at": common["generated_at"],
            "source_hashes": dict(common.get("source_hashes") or {}),
            "ts": event.get("ts") or event.get("timestamp"),
            "source_event_id": _event_id(event),
            "source_event_seq": _event_sequence(event),
            "event": str(event.get("event_type") or "unknown"),
            "message": str(ui.get("summary") or payload.get("message") or _event_message(event)),
            "severity": str(ui.get("severity") or payload.get("severity") or "info"),
        }
        for key in ("task_id", "run_id", "failure_id", "request_id", "approval_id"):
            value = event.get(key) or subject.get(key) or payload.get(key)
            if value is not None:
                record[key] = value
        records.append(record)
    return records


def _dashboard_event_limit(paths: WorkflowPaths, *, explicit_limit: int | None) -> int:
    if explicit_limit is not None:
        return max(1, int(explicit_limit))
    dashboard_config = _read_json_object(paths.config_file("dashboard.json"), default={})
    configured = dashboard_config.get("max_dashboard_events") if isinstance(dashboard_config, Mapping) else None
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    return max(1, min(DASHBOARD_EVENT_NODE_LIMIT, DASHBOARD_FEED_RECORD_LIMIT))


def _recent_dashboard_events(
    events: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> Sequence[Mapping[str, Any]]:
    if limit <= 0 or len(events) <= limit:
        return events
    return events[-limit:]


def _build_run_summaries(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    events: Sequence[Mapping[str, Any]],
    node_summaries: Sequence[Mapping[str, Any]],
    agent_statuses: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_run: dict[str, dict[str, Any]] = {}
    statuses_by_run = {str(status.get("run_id") or ""): status for status in agent_statuses if status.get("run_id")}
    summaries_by_run = {str(node.get("run_id") or ""): node for node in node_summaries if node.get("run_id")}
    validations_by_run = {
        str(validation.get("run_id") or ""): validation for validation in validation_records if validation.get("run_id")
    }
    active_leases_by_run: dict[str, Mapping[str, Any]] = {}
    for lease in active_leases:
        lease_run_id = str(lease.get("run_id") or "")
        if not lease_run_id:
            continue
        current = active_leases_by_run.get(lease_run_id)
        if current is None or _active_lease_sort_key(lease) >= _active_lease_sort_key(current):
            active_leases_by_run[lease_run_id] = lease
    for status in agent_statuses:
        run_id = str(status.get("run_id") or "")
        if not run_id:
            continue
        summary = _summary_from_agent_status(status)
        by_run[run_id] = {
            "schema_version": common["schema_version"],
            "workflow_id": common["workflow_id"],
            "generated_at": common["generated_at"],
            "source_hashes": dict(common.get("source_hashes") or {}),
            "source_event_id": common.get("source_event_id"),
            "source_event_seq": common.get("last_event_seq"),
            "run_id": run_id,
            "node_id": _run_node_id(run_id),
            "role": str(status.get("role") or "worker"),
            "task_id": status.get("task_id") or status.get("primary_task_id"),
            "status": status.get("status"),
            "started_at": status.get("started_at"),
            "ended_at": status.get("ended_at"),
            "summary": summary,
        }
    for node in node_summaries:
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        record = by_run.setdefault(
            run_id,
            {
                "schema_version": common["schema_version"],
                "workflow_id": common["workflow_id"],
                "generated_at": common["generated_at"],
                "source_hashes": dict(common.get("source_hashes") or {}),
                "source_event_id": common.get("source_event_id"),
                "source_event_seq": common.get("last_event_seq"),
                "run_id": run_id,
                "node_id": str(node.get("node_id") or _run_node_id(run_id)),
                "role": node.get("role") or node.get("node_type"),
                "task_id": node.get("task_id"),
                "status": node.get("status"),
                "started_at": node.get("started_at"),
                "ended_at": node.get("ended_at") or node.get("updated_at"),
                "summary": {"one_line": str(node.get("message") or node.get("status") or "Run summary."), "highlights": [], "risks": []},
            },
        )
        record["node_id"] = str(node.get("node_id") or record.get("node_id") or _run_node_id(run_id))
        record["status"] = node.get("status") or record.get("status")
        record["role"] = node.get("role") or node.get("node_type") or record.get("role")
        record["task_id"] = node.get("task_id") or record.get("task_id")
        record["started_at"] = node.get("started_at") or record.get("started_at")
        record["ended_at"] = node.get("ended_at") or node.get("updated_at") or record.get("ended_at")
    for validation in validation_records:
        run_id = str(validation.get("run_id") or "")
        if not run_id:
            continue
        record = by_run.setdefault(
            run_id,
            {
                "schema_version": common["schema_version"],
                "workflow_id": common["workflow_id"],
                "generated_at": common["generated_at"],
                "source_hashes": dict(common.get("source_hashes") or {}),
                "source_event_id": common.get("source_event_id"),
                "source_event_seq": common.get("last_event_seq"),
                "run_id": run_id,
                "node_id": _run_node_id(run_id),
                "role": "worker",
                "task_id": validation.get("primary_task_id") or validation.get("task_id"),
                "status": validation.get("status"),
                "started_at": None,
                "ended_at": validation.get("validated_at"),
                "summary": {"one_line": str(validation.get("summary") or "Validation recorded."), "highlights": [], "risks": []},
            },
        )
        record["validation_status"] = validation.get("status")
        record["validation_node_id"] = _validation_node_id(run_id)
        record["ended_at"] = record.get("ended_at") or validation.get("validated_at")
    for lease in active_leases:
        run_id = str(lease.get("run_id") or "")
        if not run_id:
            continue
        task_id = lease.get("task_id")
        role = str(lease.get("role") or "worker")
        status = str(lease.get("status") or "running")
        record = by_run.setdefault(
            run_id,
            {
                "schema_version": common["schema_version"],
                "workflow_id": common["workflow_id"],
                "generated_at": common["generated_at"],
                "source_hashes": dict(common.get("source_hashes") or {}),
                "source_event_id": common.get("source_event_id"),
                "source_event_seq": common.get("last_event_seq"),
                "run_id": run_id,
                "node_id": str(lease.get("node_id") or _run_node_id(run_id)),
                "role": role,
                "task_id": task_id,
                "status": status,
                "started_at": lease.get("started_at") or lease.get("prepared_at") or lease.get("heartbeat_at"),
                "ended_at": None,
                "summary": _summary_from_active_lease(lease),
            },
        )
        record.update(
            {
                "node_id": str(lease.get("node_id") or record.get("node_id") or _run_node_id(run_id)),
                "role": role,
                "task_id": task_id or record.get("task_id"),
                "status": status,
                "started_at": record.get("started_at") or lease.get("started_at") or lease.get("prepared_at") or lease.get("heartbeat_at"),
                "ended_at": None,
                "summary": _summary_from_active_lease(lease),
                "active": True,
                "heartbeat_at": lease.get("heartbeat_at"),
                "lease_expires_at": lease.get("lease_expires_at"),
                "active_run_lease_path": lease.get("active_run_lease_path"),
            }
        )

    for run_id, record in by_run.items():
        task_id = _first_string(record.get("task_id"), _mapping_value(statuses_by_run.get(run_id), "task_id"))
        record["details"] = _run_detail_sections(
            paths,
            run_id=run_id,
            task_id=task_id,
            agent_status=statuses_by_run.get(run_id),
            node_summary=summaries_by_run.get(run_id),
            validation=validations_by_run.get(run_id),
            active_lease=active_leases_by_run.get(run_id),
        )
    return sorted(by_run.values(), key=lambda item: str(item.get("run_id")))


def _run_detail_sections(
    paths: WorkflowPaths,
    *,
    run_id: str,
    task_id: str | None,
    agent_status: Mapping[str, Any] | None,
    node_summary: Mapping[str, Any] | None,
    validation: Mapping[str, Any] | None,
    active_lease: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run_dirs = _run_candidate_dirs(paths, run_id=run_id, task_id=task_id, active_lease=active_lease)
    sections = [
        _text_file_section(
            paths,
            "prompt",
            "Prompt",
            _candidate_files(run_dirs, ("prompt.md", "planner_prompt.md", "auditor_prompt.md", "change_request_planner_prompt.md")),
            "No prompt evidence was recorded for this run.",
            limit=NODE_DETAIL_TEXT_LIMIT,
        ),
        _text_file_section(
            paths,
            "final_output",
            "Final Response",
            _candidate_files(run_dirs, ("final.md", "final_output.md")),
            "No final response file was recorded for this run.",
            limit=NODE_DETAIL_TEXT_LIMIT,
        ),
        _text_file_section(
            paths,
            "report",
            "Report",
            _candidate_files(run_dirs, ("report.md",)),
            "No worker report was recorded for this run.",
            limit=NODE_DETAIL_TEXT_LIMIT,
        ),
        _human_summary_section(paths, task_id),
        _logs_section(paths, run_dirs, extra_files=_active_lease_log_files(paths, active_lease)),
        _validation_section(paths, run_dirs, validation),
        _artifacts_section(paths, run_dirs),
        _project_changes_section(paths, agent_status, validation),
        _git_checkpoint_section(paths, run_id=run_id, task_id=task_id),
        _diff_summary_section(paths, run_dirs),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "node_summary_path": _safe_dashboard_path(paths, _mapping_value(node_summary, "_path")),
        "agent_status_path": _safe_dashboard_path(paths, _mapping_value(agent_status, "_path")),
        "validation_path": _safe_dashboard_path(paths, _mapping_value(validation, "_path")),
        "run_dirs": [_safe_dashboard_path(paths, path) for path in run_dirs],
        "sections": sections,
        "available_sections": [section["key"] for section in sections if section.get("available")],
        "missing_sections": [section["key"] for section in sections if not section.get("available")],
    }


def _run_candidate_dirs(
    paths: WorkflowPaths,
    *,
    run_id: str,
    task_id: str | None,
    active_lease: Mapping[str, Any] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen or not resolved.is_dir() or not _is_dashboard_safe_path(paths, resolved):
            return
        seen.add(resolved)
        candidates.append(resolved)

    if task_id:
        add(paths.results_dir / task_id / "runs" / run_id)
    add(paths.runtime_dir / "runs" / run_id)
    add(paths.planning_dir / "runs" / run_id)
    if isinstance(active_lease, Mapping):
        for key in ("scheduler_run_dir", "role_output_dir", "task_evidence_run_dir"):
            add(_project_path(paths, _first_string(active_lease.get(key))))
    if paths.results_dir.exists():
        for path in sorted(paths.results_dir.glob(f"*/runs/{run_id}")):
            add(path)
    if paths.planning_dir.exists():
        for path in sorted(paths.planning_dir.glob(f"**/{run_id}")):
            add(path)
    return candidates


def _candidate_files(run_dirs: Sequence[Path], names: Sequence[str]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for run_dir in run_dirs:
        for name in names:
            path = run_dir / name
            if path.is_file() and path not in seen:
                seen.add(path)
                candidates.append(path)
    return candidates


def _text_file_section(
    paths: WorkflowPaths,
    key: str,
    title: str,
    candidates: Sequence[Path],
    empty_message: str,
    *,
    limit: int,
) -> dict[str, Any]:
    for path in candidates:
        record = _text_file_record(paths, path, limit=limit)
        if record is not None:
            return {"key": key, "title": title, "available": True, **record}
    return {"key": key, "title": title, "available": False, "empty_message": empty_message}


def _human_summary_section(paths: WorkflowPaths, task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {
            "key": "human_summary",
            "title": "Human Summary",
            "available": False,
            "empty_message": "No related task human summary is associated with this run.",
        }
    return _text_file_section(
        paths,
        "human_summary",
        "Human Summary",
        (paths.results_dir / task_id / "human_summary.md",),
        "No related task human summary has been generated for this run.",
        limit=NODE_DETAIL_TEXT_LIMIT,
    )


def _active_lease_log_files(paths: WorkflowPaths, active_lease: Mapping[str, Any] | None) -> list[Path]:
    if not isinstance(active_lease, Mapping):
        return []
    files: list[Path] = []
    for key in ("stdout_path", "stderr_path"):
        path = _project_path(paths, _first_string(active_lease.get(key)))
        if path is not None and _is_dashboard_safe_path(paths, path):
            files.append(path)
    return files


def _logs_section(
    paths: WorkflowPaths,
    run_dirs: Sequence[Path],
    *,
    extra_files: Sequence[Path] = (),
) -> dict[str, Any]:
    files: list[tuple[Path, bool]] = []
    seen: set[Path] = set()

    def add(path: Path, *, allow_missing: bool) -> None:
        if not _is_dashboard_safe_path(paths, path):
            return
        if not allow_missing and not path.is_file():
            return
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen:
            return
        seen.add(resolved)
        files.append((resolved, allow_missing))

    for run_dir in run_dirs:
        for path in (run_dir / "stdout.log", run_dir / "stderr.log"):
            add(path, allow_missing=False)
        logs_dir = run_dir / "logs"
        if logs_dir.is_dir():
            for path in sorted(logs_dir.glob("*")):
                add(path, allow_missing=False)
    for path in extra_files:
        add(path, allow_missing=True)
    items = [
        record
        for record in (
            _text_file_record(paths, path, limit=NODE_DETAIL_LOG_LIMIT)
            or (_pending_log_file_record(paths, path) if allow_missing else None)
            for path, allow_missing in files[:NODE_DETAIL_ITEM_LIMIT]
        )
        if record is not None
    ]
    if not items:
        return {"key": "logs", "title": "Logs", "available": False, "empty_message": "No log files were recorded for this run."}
    return {"key": "logs", "title": "Logs", "available": True, "items": items, "truncated": len(files) > len(items)}


def _pending_log_file_record(paths: WorkflowPaths, path: Path) -> dict[str, Any] | None:
    if not _is_dashboard_safe_path(paths, path):
        return None
    safe_path = _safe_dashboard_path(paths, path)
    if not safe_path or safe_path == "[redacted path]":
        return None
    return {
        "path": safe_path,
        "size_bytes": 0,
        "render_mode": _file_render_mode(path),
        "content": "",
        "truncated": False,
        "pending": True,
    }


def _validation_section(
    paths: WorkflowPaths,
    run_dirs: Sequence[Path],
    validation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = validation if isinstance(validation, Mapping) else None
    path = None
    if record and record.get("_path"):
        path = _project_path(paths, str(record["_path"]))
    if path is None:
        candidates = _candidate_files(run_dirs, ("validation.json",))
        path = candidates[0] if candidates else None
        record = _read_json_object(path, default=None) if path is not None else None
    if not isinstance(record, Mapping):
        return {"key": "validation", "title": "Validation", "available": False, "empty_message": "No validation record is available for this run."}
    display_record = _sanitize_detail_value(paths, record)
    content = _truncate_text(
        _redact_detail_text(json.dumps(_json_safe(display_record), indent=2, sort_keys=True)),
        NODE_DETAIL_TEXT_LIMIT,
    )
    return {
        "key": "validation",
        "title": "Validation",
        "available": True,
        "path": _safe_dashboard_path(paths, path),
        "status": record.get("status"),
        "summary": record.get("summary"),
        "content": content["content"],
        "truncated": content["truncated"],
    }


def _artifacts_section(paths: WorkflowPaths, run_dirs: Sequence[Path]) -> dict[str, Any]:
    artifacts: list[Path] = []
    seen: set[Path] = set()
    for run_dir in run_dirs:
        artifacts_dir = run_dir / "artifacts"
        if not artifacts_dir.is_dir():
            continue
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file() and path not in seen and _is_dashboard_safe_path(paths, path):
                seen.add(path)
                artifacts.append(path)
    items = [_artifact_record(paths, path) for path in artifacts[:NODE_DETAIL_ITEM_LIMIT]]
    if not items:
        return {"key": "artifacts", "title": "Artifacts", "available": False, "empty_message": "No artifact files were recorded for this run."}
    return {"key": "artifacts", "title": "Artifacts", "available": True, "items": items, "truncated": len(artifacts) > len(items)}


def _project_changes_section(
    paths: WorkflowPaths,
    agent_status: Mapping[str, Any] | None,
    validation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    items: list[Any] = []
    for source in (agent_status, validation):
        if not isinstance(source, Mapping):
            continue
        for key in ("project_changes", "changed_files", "key_outputs"):
            raw_items = source.get(key)
            if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
                items.extend(raw_items)
            elif raw_items:
                items.append(raw_items)
    sanitized = [_sanitize_detail_value(paths, item) for item in items[:NODE_DETAIL_ITEM_LIMIT]]
    if not sanitized:
        return {
            "key": "project_changes",
            "title": "Project Changes",
            "available": False,
            "empty_message": "No project change summary was recorded for this run.",
        }
    return {
        "key": "project_changes",
        "title": "Project Changes",
        "available": True,
        "items": sanitized,
        "truncated": len(items) > len(sanitized),
    }


def _git_checkpoint_section(paths: WorkflowPaths, *, run_id: str, task_id: str | None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in _read_jsonl(paths.runtime_dir / "git_checkpoints.jsonl"):
        if str(record.get("run_id") or "") != run_id:
            continue
        if task_id and str(record.get("task_id") or "") not in {"", task_id}:
            continue
        sanitized = _checkpoint_detail_record(record)
        if sanitized:
            records.append(sanitized)
    if not records:
        return {
            "key": "git_checkpoint",
            "title": "Git Checkpoint",
            "available": False,
            "empty_message": "No checkpoint metadata is associated with this run.",
        }
    before = next((record for record in records if record.get("reason") == "before_worker_run"), None)
    after = next((record for record in reversed(records) if record.get("reason") != "before_worker_run"), None)
    return {
        "key": "git_checkpoint",
        "title": "Git Checkpoint",
        "available": True,
        "before": before,
        "after": after,
        "items": records[-NODE_DETAIL_ITEM_LIMIT:],
        "truncated": len(records) > NODE_DETAIL_ITEM_LIMIT,
    }


def _diff_summary_section(paths: WorkflowPaths, run_dirs: Sequence[Path]) -> dict[str, Any]:
    for run_dir in run_dirs:
        git_dir = run_dir / "git"
        changed_files_path = git_dir / "changed_files.json"
        if not changed_files_path.is_file():
            continue
        payload = _read_json_object(changed_files_path, default={})
        if not isinstance(payload, Mapping):
            continue
        changed_files = _sanitize_changed_files_for_dashboard(paths, payload.get("changed_files"))
        patch_path = git_dir / "project_diff.patch"
        patch = _artifact_record(paths, patch_path) if patch_path.is_file() else {"available": False, "path": _safe_dashboard_path(paths, patch_path)}
        return {
            "key": "diff_summary",
            "title": "Diff Summary",
            "available": True,
            "path": _safe_dashboard_path(paths, changed_files_path),
            "base_commit": _redact_detail_text(str(payload.get("base_commit") or "")) or None,
            "current_tree": _redact_detail_text(str(payload.get("current_tree") or "")) or None,
            "changed_files_count": len(changed_files),
            "changed_files": changed_files[:NODE_DETAIL_ITEM_LIMIT],
            "summary": _changed_files_dashboard_summary(changed_files),
            "patch": patch,
            "truncated": len(changed_files) > NODE_DETAIL_ITEM_LIMIT,
        }
    return {"key": "diff_summary", "title": "Diff Summary", "available": False, "empty_message": "No captured diff metadata is available for this run."}


def _text_file_record(paths: WorkflowPaths, path: Path, *, limit: int) -> dict[str, Any] | None:
    if not path.is_file() or not _is_dashboard_safe_path(paths, path):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    preview_limit = max(limit + NODE_DETAIL_REDACTION_EXTRA_BYTES, limit * 4, 8192)
    preview_data = data[:preview_limit]
    decoded = preview_data.decode("utf-8", errors="replace")
    content = _truncate_text(_redact_detail_text(decoded), limit)
    if len(data) > len(preview_data):
        content["truncated"] = True
    return {
        "path": _safe_dashboard_path(paths, path),
        "size_bytes": len(data),
        "sha256": "sha256:" + sha256(data).hexdigest(),
        "render_mode": _file_render_mode(path),
        "content": content["content"],
        "truncated": content["truncated"],
    }


def _file_render_mode(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_PREVIEW_SUFFIXES:
        return "image"
    return "markdown" if suffix in {".md", ".markdown", ".mdown", ".mkd"} else "text"


def _artifact_record(paths: WorkflowPaths, path: Path) -> dict[str, Any]:
    if not path.is_file() or not _is_dashboard_safe_path(paths, path):
        return {"available": False, "path": _safe_dashboard_path(paths, path)}
    try:
        data = path.read_bytes()
    except OSError:
        return {"available": False, "path": _safe_dashboard_path(paths, path)}
    record: dict[str, Any] = {
        "available": True,
        "path": _safe_dashboard_path(paths, path),
        "size_bytes": len(data),
        "sha256": "sha256:" + sha256(data).hexdigest(),
        "render_mode": _file_render_mode(path),
    }
    if _looks_textual(path, data):
        content = _truncate_text(_redact_detail_text(data.decode("utf-8", errors="replace")), 1200)
        record["content"] = content["content"]
        record["truncated"] = content["truncated"]
    return record


def _looks_textual(path: Path, data: bytes) -> bool:
    if b"\0" in data[:1024]:
        return False
    if path.suffix.lower() in {".md", ".txt", ".log", ".json", ".jsonl", ".patch", ".diff", ".csv", ".yaml", ".yml", ".sh", ".py"}:
        return True
    try:
        data[:2048].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _checkpoint_detail_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    checkpoint_id = _safe_detail_text(record.get("checkpoint_id"))
    if checkpoint_id is None:
        return None
    return {
        "checkpoint_id": checkpoint_id,
        "created_at": _safe_detail_text(record.get("created_at")),
        "reason": _safe_detail_text(record.get("reason")),
        "task_id": _safe_detail_text(record.get("task_id")),
        "run_id": _safe_detail_text(record.get("run_id")),
        "status": _safe_detail_text(record.get("status")) or "created",
        "provider": _safe_detail_text(record.get("provider")) or "git",
        "backend": _safe_detail_text(record.get("backend")) or _safe_detail_text(record.get("checkpoint_backend")),
        "commit": _safe_detail_text(record.get("commit")) or _safe_detail_text(record.get("commit_sha")),
    }


def _sanitize_changed_files_for_dashboard(paths: WorkflowPaths, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    changed_files: list[dict[str, Any]] = []
    for raw_entry in value:
        if not isinstance(raw_entry, Mapping):
            continue
        path = _safe_dashboard_path(paths, raw_entry.get("path"))
        if path in {None, "[redacted path]"}:
            continue
        entry: dict[str, Any] = {
            "path": path,
            "change_type": _safe_detail_text(raw_entry.get("change_type")) or "modified",
        }
        old_path = _safe_dashboard_path(paths, raw_entry.get("old_path"))
        if old_path not in {None, "[redacted path]"}:
            entry["old_path"] = old_path
        for key in ("lines_added", "lines_deleted", "line_delta"):
            parsed = _coerce_detail_int(raw_entry.get(key))
            if parsed is not None:
                entry[key] = parsed
        changed_files.append(entry)
    return changed_files


def _changed_files_dashboard_summary(changed_files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lines_added = 0
    lines_deleted = 0
    by_change_type: dict[str, int] = {}
    for entry in changed_files:
        change_type = str(entry.get("change_type") or "modified")
        by_change_type[change_type] = by_change_type.get(change_type, 0) + 1
        lines_added += int(entry.get("lines_added") or 0)
        lines_deleted += int(entry.get("lines_deleted") or 0)
    return {
        "files_changed": len(changed_files),
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "line_delta": lines_added - lines_deleted,
        "by_change_type": by_change_type,
    }


def _sanitize_detail_value(paths: WorkflowPaths, value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in value.items():
            item_key_text = str(item_key)
            if item_key_text.startswith("_"):
                continue
            if item_key_text.lower() in NODE_DETAIL_SENSITIVE_KEYS:
                sanitized[item_key_text] = "[REDACTED]"
                continue
            sanitized[item_key_text] = _sanitize_detail_value(paths, item_value, key=item_key_text)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_sanitize_detail_value(paths, item) for item in list(value)[:NODE_DETAIL_ITEM_LIMIT]]
    if isinstance(value, str):
        if key and _is_pathish_detail_key(key):
            return _safe_dashboard_path(paths, value) or "[redacted path]"
        return _redact_detail_text(value)
    return value


def _is_pathish_detail_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in ("path", "file", "dir", "artifact", "log", "output"))


def _safe_dashboard_path(paths: WorkflowPaths, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Path):
        path = value
    else:
        text = str(value).strip()
        if not text:
            return None
        path = Path(text)
    if path.is_absolute():
        try:
            resolved = path.resolve()
            resolved.relative_to(paths.project_root.resolve())
        except (OSError, ValueError):
            return "[redacted path]"
        label = resolved.relative_to(paths.project_root.resolve()).as_posix()
    else:
        label = PurePosixPath(path.as_posix()).as_posix()
    parts = PurePosixPath(label.replace("\\", "/")).parts
    if not parts or any(part in {"", ".", "..", ".git"} for part in parts):
        return "[redacted path]"
    return "/".join(parts)


def _project_path(paths: WorkflowPaths, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = paths.project_root / path
    return path


def _is_dashboard_safe_path(paths: WorkflowPaths, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(paths.project_root.resolve())
    except (OSError, ValueError):
        return False
    return ".git" not in relative.parts


def _safe_detail_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    if normalized == ".git" or normalized.startswith(".git/") or normalized.endswith("/.git") or "/.git/" in normalized:
        return None
    return _redact_detail_text(text)


def _redact_detail_text(text: str) -> str:
    redacted = str(text)
    redacted = PRIVATE_KEY_REDACTION_RE.sub("[REDACTED]", redacted)
    redacted = BEARER_REDACTION_RE.sub("Bearer [REDACTED]", redacted)
    redacted = SECRET_WORD_REDACTION_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = SECRET_ASSIGNMENT_REDACTION_RE.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def _truncate_text(text: str, limit: int) -> dict[str, Any]:
    if len(text) <= limit:
        return {"content": text, "truncated": False}
    return {"content": text[:limit].rstrip() + "\n[truncated]", "truncated": True}


def _coerce_detail_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_contexts(project: Path, paths: WorkflowPaths, tasks: Sequence[PlanTask]) -> list[TaskContext]:
    contexts: list[TaskContext] = []
    for task in tasks:
        latest_path = _latest_path(project, paths, task)
        latest = _read_json_object(latest_path, default=None)
        validation_path = _validation_path_from_latest(project, latest)
        validation = _read_json_object(validation_path, default=None) if validation_path is not None else None
        contexts.append(
            TaskContext(
                task=task,
                latest=latest if isinstance(latest, Mapping) else None,
                latest_path=latest_path,
                project_root=project,
                paths=paths,
                validation=validation if isinstance(validation, Mapping) else None,
                validation_path=validation_path,
            )
        )
    return contexts


def _task_summaries(
    contexts: Sequence[TaskContext],
    *,
    failures: Sequence[Any],
    expansion_tasks: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_task_failures: dict[str, list[Mapping[str, Any]]] = {}
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        task_id = str(failure.get("task_id") or "")
        if task_id:
            by_task_failures.setdefault(task_id, []).append(failure)
    summaries: list[dict[str, Any]] = []
    for context in contexts:
        task = context.task
        status = CHECKBOX_STATUSES.get(task.status, "unknown")
        validation_status = _validation_status(context.validation, context.latest, by_task_failures.get(task.task_id, []))
        latest_run_id = _first_string(
            _mapping_value(context.latest, "latest_run_id"),
            _mapping_value(context.validation, "run_id"),
        )
        summary = {
            "task_id": task.task_id,
            "title": task.title,
            "status": status,
            "checkbox": f"[{task.status}]",
            "phase": task.phase,
            "acceptance": _first_field(task, "acceptance"),
            "acceptance_criteria": list(task.fields.get("acceptance", ())),
            "deliverables": _first_field(task, "deliverables") or _first_field(task, "final_deliverables"),
            "evidence_root": _first_field(task, "evidence"),
            "latest_path": _task_latest_display(context),
            "latest_run_id": latest_run_id,
            "latest_run_dir": _mapping_value(context.latest, "latest_run_dir"),
            "validation_path": _validation_display_path(context),
            "validation_status": validation_status,
            "human_summary": _fresh_human_summary(
                load_task_human_summary(context.project_root, context.paths, task.task_id),
                expected_source_hash=task_human_summary_source_hash(context.project_root, context.paths, task),
            ),
            "last_updated_at": _first_string(
                _mapping_value(context.latest, "updated_at"),
                _mapping_value(context.validation, "validated_at"),
                _latest_failure_timestamp(by_task_failures.get(task.task_id, [])),
            ),
            "dependencies": _parse_dependency_list(_first_field(task, "depends_on") or "[]"),
            "risk_level": _first_field(task, "risk"),
            "approval": _first_field(task, "approval"),
            "display": _task_display(status, validation_status, latest_run_id, by_task_failures.get(task.task_id, [])),
            "failures": [_compact_failure(failure) for failure in by_task_failures.get(task.task_id, [])],
        }
        expansion = _mapping_or_empty(expansion_tasks.get(task.task_id))
        if expansion:
            summary.update(_expanded_fields(expansion))
        summaries.append(summary)
    return summaries


def _fresh_human_summary(record: Mapping[str, Any], *, expected_source_hash: str) -> dict[str, Any]:
    summary = dict(record)
    if summary.get("status") != "ready":
        return summary
    actual_hash = _mapping_value(summary.get("source_hashes"), "summary_source")
    if actual_hash == expected_source_hash:
        summary["fresh"] = True
        return summary
    return {
        "status": "stale",
        "fresh": False,
        "stale_reason": "summary_source_changed",
        "expected_summary_source": expected_source_hash,
        "actual_summary_source": actual_hash,
        "kind": summary.get("kind"),
        "task_id": summary.get("task_id"),
        "phase_id": summary.get("phase_id"),
        "phase_title": summary.get("phase_title"),
        "markdown_path": summary.get("markdown_path"),
        "json_path": summary.get("json_path"),
        "generated_at": summary.get("generated_at"),
    }


def _latest_path(project: Path, paths: WorkflowPaths, task: PlanTask) -> Path:
    latest = task.latest_path or f"{paths.value('results_dir').rstrip('/')}/{task.task_id}/latest.json"
    path = Path(latest)
    return path if path.is_absolute() else project / latest


def _validation_path_from_latest(project: Path, latest: Any) -> Path | None:
    if not isinstance(latest, Mapping):
        return None
    value = latest.get("validation_path")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else project / value


def _collect_validation_records(paths: WorkflowPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.results_dir.exists():
        return records
    for path in sorted(paths.results_dir.glob("**/validation.json")):
        data = _read_json_object(path, default=None)
        if not isinstance(data, Mapping):
            continue
        record = dict(data)
        record["_path"] = _path_for_record(paths.project_root, path)
        records.append(record)
    return records


def _validation_manifest(
    project: Path,
    contexts: Sequence[TaskContext],
    validation_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for context in contexts:
        for path in (context.latest_path, context.validation_path):
            if path is None or not path.exists():
                continue
            rel = _path_for_record(project, path)
            if rel in seen:
                continue
            seen.add(rel)
            entries.append({"path": rel, "sha256": _sha256_file(path)})
    for record in validation_records:
        path = record.get("_path")
        if not isinstance(path, str) or path in seen:
            continue
        absolute = project / path
        if not absolute.exists():
            continue
        seen.add(path)
        entries.append({"path": path, "sha256": _sha256_file(absolute)})
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "entries": sorted(entries, key=lambda item: item["path"]),
        "sha256": "sha256:" + sha256(encoded).hexdigest(),
    }


def _source_hashes(
    paths: WorkflowPaths,
    *,
    plan_source: PlanSource,
    events: Sequence[Mapping[str, Any]],
    last_event: Mapping[str, Any] | None,
    validation_manifest: Mapping[str, Any],
    active_leases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "plan": _sha256_file(plan_source.path),
        "plan_source": plan_source.kind,
        "plan_file": plan_source.value,
        "active_plan": _sha256_file(paths.plan_file),
        "events_head": _event_id(last_event) if last_event is not None else None,
        "events_sha256": _events_sha256(paths.runtime_dir / "events"),
        "state": _sha256_file(paths.runtime_dir / "state.json"),
        "expansion_registry": _sha256_file(paths.runtime_dir / "expansion_registry.json"),
        "validations_manifest": validation_manifest.get("sha256"),
        "active_leases": _active_leases_sha256(active_leases),
        "events_count": len(events),
    }


def active_leases_source_hash(paths: WorkflowPaths) -> str:
    return _active_leases_sha256(_read_active_leases(paths))


def _active_leases_sha256(active_leases: Sequence[Mapping[str, Any]]) -> str:
    entries: list[dict[str, Any]] = []
    for lease in active_leases:
        entries.append(
            {
                key: lease.get(key)
                for key in (
                    "workflow_id",
                    "run_id",
                    "node_id",
                    "task_id",
                    "role",
                    "runner_id",
                    "status",
                    "prepared_at",
                    "started_at",
                    "scheduler_run_dir",
                    "role_output_dir",
                    "prompt_path",
                    "active_run_lease_path",
                )
                if lease.get(key) not in (None, "", [])
            }
        )
    encoded = json.dumps(sorted(entries, key=lambda item: str(item.get("run_id") or "")), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _progress(task_summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(task_summaries)
    completed = sum(1 for task in task_summaries if task.get("status") == "done")
    return {
        "total_tasks": total,
        "completed_tasks": completed,
        "partial_tasks": sum(1 for task in task_summaries if task.get("status") == "partial"),
        "blocked_tasks": sum(1 for task in task_summaries if task.get("status") == "blocked"),
        "skipped_tasks": sum(1 for task in task_summaries if task.get("status") == "skipped"),
        "progress_percent": _progress_percent(completed, total),
    }


def _progress_percent(completed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((completed / total) * 100, 1)


def _plan_index_task(task: Mapping[str, Any]) -> dict[str, Any]:
    record = {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "checkbox": task.get("checkbox"),
        "acceptance": task.get("acceptance"),
        "acceptance_criteria": list(task.get("acceptance_criteria") or []),
        "deliverables": task.get("deliverables"),
        "evidence_root": task.get("evidence_root"),
        "latest_path": task.get("latest_path"),
        "latest_run_id": task.get("latest_run_id"),
        "latest_run_dir": task.get("latest_run_dir"),
        "validation_status": task.get("validation_status"),
        "human_summary": dict(task.get("human_summary") or {}),
        "last_updated_at": task.get("last_updated_at"),
        "dependencies": list(task.get("dependencies") or []),
        "risk_level": task.get("risk_level"),
        "approval": task.get("approval"),
        "display": dict(task.get("display") or {}),
    }
    if task.get("expanded") is True:
        record.update(_expanded_fields(_mapping_or_empty(task.get("expansion"))))
    return record


def _phase_id(title: str) -> str:
    parts = title.split(":", 1)[0].split()
    return parts[1] if len(parts) > 1 and parts[0] == "Phase" else title


def _phase_status(progress: Mapping[str, Any]) -> str:
    total = int(progress.get("total_tasks") or 0)
    if total == 0:
        return "empty"
    if int(progress.get("blocked_tasks") or 0) > 0:
        return "blocked"
    if int(progress.get("completed_tasks") or 0) == total:
        return "done"
    if int(progress.get("completed_tasks") or 0) > 0 or int(progress.get("partial_tasks") or 0) > 0:
        return "in_progress"
    return "pending"


def _workflow_phase(runtime_state: Mapping[str, Any], task_summaries: Sequence[Mapping[str, Any]]) -> str:
    status = str(runtime_state.get("status") or "").lower()
    if status in {"initialized", "waiting_config"}:
        return "planning"
    if task_summaries and all(task.get("status") in {"done", "skipped"} for task in task_summaries):
        return "completion"
    return "execution"


def _active_task_id(
    runtime_state: Mapping[str, Any],
    task_summaries: Sequence[Mapping[str, Any]],
    active_lease: Mapping[str, Any] | None,
) -> str | None:
    return _first_string(
        _mapping_value(active_lease, "task_id"),
        _nested_mapping_value(runtime_state, "scheduler", "active_task_id"),
        runtime_state.get("active_task_id"),
        _first_pending_task(task_summaries),
    )


def _current_activity(
    status: str,
    active_task_id: str | None,
    active_run_id: str | None,
    active_lease: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if active_run_id:
        return {
            "type": str(_mapping_value(active_lease, "role") or "worker"),
            "title": f"Running {active_task_id or active_run_id}",
            "started_at": _mapping_value(active_lease, "started_at") or _mapping_value(active_lease, "heartbeat_at"),
        }
    return {
        "type": "status",
        "title": status,
        "started_at": None,
    }


def _control_summary(
    control_requests: Sequence[Mapping[str, Any]],
    control_responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records = control_request_statuses(control_requests, control_responses)
    recent = records[-8:]
    latest = recent[-1] if recent else None
    return {
        "pending_count": sum(1 for record in records if record.get("status") == "pending"),
        "applied_count": sum(1 for record in records if record.get("status") == "applied"),
        "rejected_count": sum(1 for record in records if record.get("status") == "rejected"),
        "latest_request_id": latest.get("request_id") if isinstance(latest, Mapping) else None,
        "latest_type": latest.get("type") if isinstance(latest, Mapping) else None,
        "latest_status": latest.get("status") if isinstance(latest, Mapping) else None,
        "recent": [
            {
                "request_id": record.get("request_id"),
                "type": record.get("type"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
                "handled_at": record.get("handled_at"),
                "resulting_workflow_status": record.get("resulting_workflow_status"),
            }
            for record in recent
        ],
        "commands": [
            "loopplane start --detach --project <project>",
            "loopplane pause --project <project>",
            "loopplane resume --project <project>",
            "loopplane stop --project <project>",
            "loopplane status --project <project>",
            "loopplane logs --project <project>",
            "loopplane attach --project <project>",
            "loopplane migrate --project <project>",
        ],
    }


def _change_request_summary(
    change_requests: Sequence[Mapping[str, Any]],
    change_request_responses: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records = _change_request_statuses_for_read_model(
        change_requests,
        change_request_responses,
        approvals,
        approval_responses,
    )
    recent = records[-8:]
    latest = recent[-1] if recent else None
    return {
        "pending_review_count": sum(1 for record in records if record.get("status") == "pending_review"),
        "needs_user_approval_count": sum(1 for record in records if record.get("status") == "needs_user_approval"),
        "approved_count": sum(1 for record in records if record.get("status") == "approved"),
        "applied_count": sum(1 for record in records if record.get("status") == "applied"),
        "failed_count": sum(1 for record in records if record.get("status") == "failed"),
        "latest_change_request_id": latest.get("change_request_id") if isinstance(latest, Mapping) else None,
        "latest_status": latest.get("status") if isinstance(latest, Mapping) else None,
        "recent": recent,
        "commands": [
            "loopplane change-request submit \"<request>\" --project <project>",
            "loopplane change-request review <change_request_id> --project <project>",
            "loopplane approvals --project <project>",
            "loopplane change-request apply <change_request_id> --project <project>",
            "loopplane change-request status --project <project>",
        ],
    }


def _change_request_statuses_for_read_model(
    change_requests: Sequence[Mapping[str, Any]],
    change_request_responses: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    responses_by_request: dict[str, list[Mapping[str, Any]]] = {}
    for response in change_request_responses:
        change_request_id = str(response.get("change_request_id") or "")
        if change_request_id:
            responses_by_request.setdefault(change_request_id, []).append(response)

    records: list[dict[str, Any]] = []
    for request in change_requests:
        change_request_id = str(request.get("change_request_id") or "")
        if not change_request_id:
            continue
        responses = responses_by_request.get(change_request_id, [])
        latest = _latest_change_request_response(responses)
        status = str(request.get("status") or "pending_review")
        approval_id = None
        patch_file = None
        if latest is not None:
            latest_status = str(latest.get("change_request_status") or latest.get("status") or "")
            approval_id = latest.get("approval_request_id")
            patch = latest.get("plan_patch")
            if isinstance(patch, Mapping):
                patch_file = patch.get("patch_file")
            if latest_status == "applied":
                status = "applied"
            elif latest_status == "failed":
                status = "failed"
            elif patch_file:
                requires_approval = bool(_mapping(latest.get("impact")).get("requires_approval"))
                if requires_approval:
                    approval = _approval_status_for_change_request(approvals, approval_responses, str(approval_id or ""))
                    approval_status = str(_mapping(approval).get("status") or "")
                    if approval_status == "approved":
                        status = "approved"
                    elif approval_status == "superseded":
                        status = "superseded"
                    elif approval_status in {"rejected", "expired"}:
                        status = "rejected"
                    else:
                        status = "needs_user_approval"
                else:
                    status = "approved"
        records.append(
            {
                "change_request_id": change_request_id,
                "status": status,
                "created_at": request.get("created_at"),
                "source": request.get("source"),
                "user_request": request.get("user_request"),
                "response_id": latest.get("response_id") if latest is not None else None,
                "approval_request_id": approval_id,
                "plan_patch_file": patch_file,
            }
        )
    return sorted(records, key=lambda item: str(item.get("created_at") or ""))


def _latest_change_request_response(responses: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not responses:
        return None
    return dict(sorted(responses, key=lambda item: str(item.get("created_at") or ""))[-1])


def _approval_status_for_change_request(
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
    approval_id: str,
) -> dict[str, Any] | None:
    if not approval_id:
        return None
    for approval in approvals:
        if str(approval.get("approval_id") or approval.get("request_id") or "") == approval_id:
            return approval_record_status(approval, responses=approval_responses, now=utc_timestamp())
    return None


def _requires_attention(
    runtime_state: Mapping[str, Any],
    approvals: Sequence[Mapping[str, Any]],
    approval_responses: Sequence[Mapping[str, Any]],
    failure_registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    attention: list[dict[str, Any]] = []
    for item in runtime_state.get("requires_attention") or []:
        if not isinstance(item, Mapping):
            continue
        attention.append(
            {
                "type": item.get("type") or "requires_attention",
                "request_id": item.get("request_id") or item.get("approval_id") or item.get("task_id"),
                "task_id": item.get("task_id"),
                "approval_id": item.get("approval_id"),
                "status": item.get("status") or "requires_attention",
                "message": item.get("message") or item.get("reason") or "Runtime requires attention.",
                "reason": item.get("reason"),
            }
        )
    for problem in runtime_state.get("configuration_problems") or []:
        if isinstance(problem, Mapping):
            attention.append(
                {
                    "type": "configuration",
                    "request_id": problem.get("code"),
                    "message": problem.get("message") or problem.get("reason") or "Configuration requires attention.",
                }
            )
    for approval in approvals:
        status_record = approval_record_status(approval, responses=approval_responses, now=utc_timestamp())
        status = str(status_record.get("status") or "pending").lower()
        if status != "pending":
            continue
        approval_id = approval.get("approval_id") or approval.get("request_id")
        attention.append(
            {
                "type": "approval",
                "request_id": approval_id,
                "approval_id": approval_id,
                "task_id": approval.get("task_id"),
                "status": "pending",
                "message": approval.get("message") or "Approval required.",
                "commands": [
                    "loopplane approvals --project <project>",
                    f"loopplane approve {approval_id} --project <project>",
                    f"loopplane reject {approval_id} --project <project>",
                ],
            }
        )
    failures = failure_registry.get("failures") if isinstance(failure_registry.get("failures"), Sequence) else []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("status") or "") in TERMINAL_FAILURE_STATUSES:
            continue
        attention.append(
            {
                "type": "failure",
                "request_id": failure.get("failure_id"),
                "message": failure.get("summary") or failure.get("failure_signature") or "Failure requires recovery.",
            }
        )
    return attention


def _self_expansion_summary(registry: Mapping[str, Any], *, objective_model: Mapping[str, Any] | None = None) -> dict[str, Any]:
    proposals = registry.get("proposals") if isinstance(registry.get("proposals"), Sequence) else []
    objective_statuses = _objective_statuses_by_id(objective_model or {})
    proposal_records = [
        _proposal_with_current_objective_resolution(proposal, objective_statuses=objective_statuses)
        for proposal in proposals
        if isinstance(proposal, Mapping)
    ]
    applied = [proposal for proposal in proposal_records if proposal.get("status") == "applied"]
    pending_resolution = [
        proposal
        for proposal in proposal_records
        if proposal.get("failure_resolution_status") == "pending_evidence"
        or proposal.get("objective_resolution_status") == "followup_pending"
    ]
    latest = proposal_records[-1] if proposal_records else None
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle": registry.get("cycle", len(applied)),
        "proposal_count": len(proposal_records),
        "applied_count": len(applied),
        "pending_resolution_count": len(pending_resolution),
        "latest_proposal": _compact_expansion_proposal(latest) if latest is not None else None,
    }


def _expansion_provenance_index(registry: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    tasks: dict[str, dict[str, Any]] = {}
    phases: dict[str, dict[str, Any]] = {}
    proposals = registry.get("proposals") if isinstance(registry.get("proposals"), Sequence) else []
    for raw_proposal in proposals:
        if not isinstance(raw_proposal, Mapping) or raw_proposal.get("status") != "applied":
            continue
        proposal = _proposal_with_current_objective_resolution(
            raw_proposal,
            objective_statuses={},
        )
        for task_id in [str(item).strip() for item in proposal.get("added_task_ids") or [] if str(item).strip()]:
            tasks[task_id] = _expansion_provenance_record(proposal, entity_type="task", entity_id=task_id)
        for phase_id in [str(item).strip() for item in proposal.get("added_phase_ids") or [] if str(item).strip()]:
            phases[phase_id] = _expansion_provenance_record(proposal, entity_type="phase", entity_id=phase_id)
    return {"tasks": tasks, "phases": phases}


def _expansion_provenance_record(proposal: Mapping[str, Any], *, entity_type: str, entity_id: str) -> dict[str, Any]:
    return {
        "source": "self_expansion",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "proposal_id": proposal.get("proposal_id"),
        "trigger": proposal.get("trigger"),
        "expansion_type": proposal.get("expansion_type"),
        "resolution_strategy": proposal.get("resolution_strategy"),
        "plan_patch_operation": proposal.get("plan_patch_operation"),
        "target_objective_ids": list(proposal.get("target_objective_ids") or []),
        "target_task_ids": list(proposal.get("target_task_ids") or []),
        "target_failure_ids": list(proposal.get("target_failure_ids") or []),
        "objective_resolution_status": proposal.get("objective_resolution_status"),
        "failure_resolution_status": proposal.get("failure_resolution_status"),
        "created_at": proposal.get("created_at"),
        "run_id": proposal.get("run_id"),
    }


def _expanded_fields(expansion: Mapping[str, Any]) -> dict[str, Any]:
    record = {key: value for key, value in dict(expansion).items() if value not in (None, "", [])}
    return {
        "expanded": True,
        "expansion_marker": "+",
        "expansion": record,
    }


def _compact_expansion_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: proposal.get(key)
        for key in (
            "proposal_id",
            "status",
            "trigger",
            "expansion_type",
            "resolution_strategy",
            "risk",
            "approval_required",
            "failure_resolution_status",
            "objective_resolution_status",
            "created_at",
            "validated_at",
            "run_id",
            "runner_id",
        )
        if proposal.get(key) not in (None, "", [])
    } | {
        "target_task_ids": list(proposal.get("target_task_ids") or []),
        "target_failure_ids": list(proposal.get("target_failure_ids") or []),
        "target_objective_ids": list(proposal.get("target_objective_ids") or []),
        "added_task_ids": list(proposal.get("added_task_ids") or []),
        "added_phase_ids": list(proposal.get("added_phase_ids") or []),
    }


def _objective_statuses_by_id(objective_model: Mapping[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    objectives = objective_model.get("objectives")
    if not isinstance(objectives, Sequence) or isinstance(objectives, (str, bytes)):
        return statuses
    for objective in objectives:
        if not isinstance(objective, Mapping):
            continue
        objective_id = str(objective.get("objective_id") or "").strip()
        status = str(objective.get("status") or "").strip()
        if objective_id:
            statuses[objective_id] = status
    return statuses


def _proposal_with_current_objective_resolution(
    proposal: Mapping[str, Any],
    *,
    objective_statuses: Mapping[str, str],
) -> dict[str, Any]:
    record = dict(proposal)
    if record.get("status") != "applied" or record.get("expansion_type") != "objective_gap":
        return record
    objective_ids = [str(item).strip() for item in record.get("target_objective_ids") or [] if str(item).strip()]
    if objective_ids and all(objective_statuses.get(objective_id) == "closed" for objective_id in objective_ids):
        record["objective_resolution_status"] = "resolved"
    elif not record.get("objective_resolution_status"):
        record["objective_resolution_status"] = "followup_pending"
    return record


def _node_from_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(summary.get("run_id") or "")
    node_id = str(summary.get("node_id") or (_run_node_id(run_id) if run_id else f"node_summary_{_stable_suffix(summary)}"))
    phase_id = summary.get("phase_id") or summary.get("objective_phase_id")
    return _node_with_timing({
        "node_id": node_id,
        "type": str(summary.get("node_type") or summary.get("role") or "run"),
        "status": summary.get("status"),
        "title": str(summary.get("message") or summary.get("task_id") or summary.get("run_id") or node_id),
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at") or summary.get("completed_at") or summary.get("updated_at"),
        "task_id": summary.get("task_id"),
        "run_id": summary.get("run_id"),
        "phase_id": phase_id,
        "phase": summary.get("phase") or phase_id,
        "objective_phase_id": summary.get("objective_phase_id"),
        "objective_scope": summary.get("objective_scope") or summary.get("scope"),
        "target_objective_ids": list(summary.get("target_objective_ids") or []),
        "input_refs": _present_refs(summary.get("prompt_path"), summary.get("agent_status_path")),
        "output_refs": _present_refs(summary.get("run_execution_path"), summary.get("adapter_result_path")),
        "summary": {"one_line": str(summary.get("message") or summary.get("status") or "Run summary."), "highlights": [], "risks": []},
    })


def _node_from_objective(objective: Mapping[str, Any]) -> dict[str, Any]:
    objective_id = str(objective.get("objective_id") or _stable_suffix(objective))
    status = str(objective.get("status") or "unknown")
    result = objective.get("result") if isinstance(objective.get("result"), Mapping) else {}
    return _node_with_timing({
        "node_id": f"objective_{_safe_id(objective_id)}",
        "type": "objective",
        "status": status,
        "title": str(objective.get("text") or objective_id),
        "task_id": None,
        "run_id": None,
        "objective_id": objective_id,
        "phase": objective.get("phase_title"),
        "context_label": _event_context_label(task_id=None, phase=objective.get("phase_title"), actor_label="objective", runner_id=None),
        "input_refs": _present_refs(objective.get("report_path")),
        "output_refs": _present_refs(objective.get("report_path")),
        "summary": {
            "one_line": f"Objective {objective_id}: {status}",
            "highlights": [str(objective.get("text") or "")],
            "risks": list(result.get("warnings") or []) if isinstance(result, Mapping) else [],
        },
    })


def _node_from_agent_status(status: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(status.get("run_id") or status.get("_derived_run_id") or "")
    role = str(status.get("role") or status.get("_derived_role") or "worker")
    task_id = status.get("task_id") or status.get("primary_task_id") or status.get("_derived_task_id")
    node_type = "validation" if role.strip().lower() == "validator" else role
    node_id = str(status.get("node_id") or "")
    if not node_id:
        if node_type == "validation":
            node_id = "node_validator_" + _safe_id(run_id)
        else:
            node_id = _run_node_id(run_id)
    summary = _summary_from_agent_status(status)
    return _node_with_timing({
        "node_id": node_id,
        "type": node_type,
        "status": status.get("status"),
        "title": str(task_id or run_id),
        "started_at": status.get("started_at"),
        "ended_at": status.get("ended_at") or status.get("completed_at") or status.get("updated_at"),
        "task_id": task_id,
        "run_id": run_id,
        "parent_run_id": status.get("_parent_run_id"),
        "phase_id": status.get("phase_id") or status.get("_derived_phase_id"),
        "phase": status.get("phase") or status.get("_derived_phase_id"),
        "input_refs": [],
        "output_refs": _present_refs(status.get("_path")),
        "summary": summary,
    })


def _node_from_validation(validation: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(validation.get("run_id") or "")
    return _node_with_timing({
        "node_id": _validation_node_id(run_id),
        "type": "validation",
        "status": validation.get("status"),
        "title": f"Validation for {validation.get('primary_task_id') or validation.get('task_id') or run_id}",
        "started_at": validation.get("started_at") or validation.get("validated_at"),
        "ended_at": validation.get("validated_at"),
        "task_id": validation.get("primary_task_id") or validation.get("task_id"),
        "run_id": run_id,
        "input_refs": [],
        "output_refs": _present_refs(validation.get("_path")),
        "summary": {"one_line": str(validation.get("summary") or "Validation recorded."), "highlights": [], "risks": list(validation.get("warnings") or [])},
    })


def _node_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "event")
    payload = _event_payload(event)
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    task_id = payload.get("task_id") or _mapping_value(subject, "task_id")
    run_id = event.get("run_id") or payload.get("run_id") or _mapping_value(subject, "run_id")
    runner_id = payload.get("runner_id") or _mapping_value(subject, "runner_id")
    role = payload.get("role") or _mapping_value(subject, "role")
    phase_id = payload.get("phase_id") or payload.get("objective_phase_id") or _mapping_value(subject, "phase_id")
    actor_label = _event_actor_label(event)
    sequence = _event_sequence(event)
    return _node_with_timing({
        "node_id": _event_node_id(event),
        "type": "event",
        "status": event_type,
        "title": _event_title(event),
        "started_at": event.get("ts") or event.get("timestamp"),
        "ended_at": event.get("ts") or event.get("timestamp"),
        "task_id": task_id,
        "run_id": run_id,
        "runner_id": runner_id,
        "agent_role": role,
        "phase_id": phase_id,
        "phase": phase_id,
        "objective_phase_id": payload.get("objective_phase_id") or payload.get("phase_id"),
        "objective_scope": payload.get("scope"),
        "target_objective_ids": list(payload.get("target_objective_ids") or []),
        "actor_label": actor_label,
        "context_label": _event_context_label(task_id=task_id, phase=None, actor_label=actor_label, runner_id=runner_id),
        "event_sequence": sequence,
        "event_id": _event_id(event),
        "event_type": event_type,
        "input_refs": [],
        "output_refs": [],
        "summary": {"one_line": _event_message(event), "highlights": [], "risks": []},
    })


def _node_from_active_lease(lease: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(lease.get("run_id") or "")
    node_id = str(lease.get("node_id") or (_run_node_id(run_id) if run_id else f"node_active_{_stable_suffix(lease)}"))
    status = str(lease.get("status") or "running")
    return _node_with_timing({
        "node_id": node_id,
        "type": str(lease.get("role") or "worker"),
        "status": status,
        "title": str(lease.get("task_id") or lease.get("run_id") or "Active run"),
        "started_at": lease.get("started_at") or lease.get("prepared_at") or lease.get("heartbeat_at"),
        "ended_at": None,
        "task_id": lease.get("task_id"),
        "run_id": run_id,
        "input_refs": _present_refs(lease.get("prompt_path")),
        "output_refs": _present_refs(
            lease.get("active_run_lease_path"),
            lease.get("scheduler_run_dir"),
            lease.get("role_output_dir"),
        ),
        "summary": _summary_from_active_lease(lease),
        "active": True,
        "heartbeat_at": lease.get("heartbeat_at"),
        "lease_expires_at": lease.get("lease_expires_at"),
    })


def _node_with_timing(node: dict[str, Any]) -> dict[str, Any]:
    started = _parse_timestamp(node.get("started_at"))
    ended = _parse_timestamp(node.get("ended_at"))
    heartbeat = _parse_timestamp(node.get("heartbeat_at"))
    if started is None and ended is not None:
        node["started_at"] = node.get("ended_at")
        started = ended
    if ended is None and started is not None and _is_terminal_node_status(node.get("status")):
        node["ended_at"] = node.get("started_at")
        ended = started
    elapsed_end = ended or (heartbeat if node.get("active") is True else None)
    if started is not None and elapsed_end is not None:
        node["elapsed_seconds"] = max(0, int((elapsed_end - started).total_seconds()))
    return node


def _is_terminal_node_status(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in {
        "pass",
        "ok",
        "current",
        "completed",
        "completed_with_warnings",
        "complete",
        "done",
        "succeeded",
        "success",
        "satisfied",
        "failed",
        "fail",
        "failure",
        "error",
        "blocked",
        "rejected",
        "invalid",
        "archived_view",
        "stopped",
        "cancelled",
        "aborted",
        "released",
    }


def _summary_from_active_lease(lease: Mapping[str, Any]) -> dict[str, Any]:
    status = str(lease.get("status") or "running")
    role = str(lease.get("role") or "worker")
    task_id = str(lease.get("task_id") or "")
    run_id = str(lease.get("run_id") or "")
    target = task_id or run_id or "this run"
    heartbeat = lease.get("heartbeat_at")
    suffix = f" Last heartbeat: {heartbeat}." if heartbeat else ""
    return {
        "one_line": f"{role} is {status} for {target}.{suffix}",
        "highlights": ["Active run lease is present."],
        "risks": [],
    }


def _enrich_node_with_task(node: Mapping[str, Any], task_lookup: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    enriched = dict(node)
    task_id = str(enriched.get("task_id") or "")
    task = task_lookup.get(task_id)
    if not task:
        return enriched
    for source_key, target_key in (
        ("title", "task_title"),
        ("deliverables", "deliverables"),
        ("acceptance", "acceptance"),
        ("evidence_root", "evidence_root"),
        ("latest_path", "latest_path"),
        ("phase", "phase"),
        ("expanded", "expanded"),
        ("expansion_marker", "expansion_marker"),
        ("expansion", "expansion"),
    ):
        value = task.get(source_key)
        if value not in (None, "", []):
            enriched.setdefault(target_key, value)
    if enriched.get("type") == "event":
        enriched["context_label"] = _event_context_label(
            task_id=enriched.get("task_id"),
            phase=enriched.get("phase"),
            actor_label=enriched.get("actor_label"),
            runner_id=enriched.get("runner_id"),
        )
    return enriched


def _graph_node_sort_key(node: Mapping[str, Any]) -> tuple[int, str, str]:
    status_rank = 0 if _status_like_active(node.get("status")) else 1
    ts = _latest_node_timestamp(node)
    return (status_rank, _reverse_lexic_sort_value(ts), str(node.get("node_id") or ""))


def _latest_node_timestamp(node: Mapping[str, Any]) -> str:
    for key in ("ended_at", "heartbeat_at", "started_at"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _reverse_lexic_sort_value(value: str) -> str:
    return "".join(chr(0x10FFFF - ord(char)) for char in value)


def _status_like_active(value: Any) -> bool:
    status = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return status in {"starting", "running", "active", "serving", "started", "resumed", "in_progress", "processing"}


def _node_from_agent_status_path(path: Path, paths: WorkflowPaths) -> dict[str, Any] | None:
    data = _read_json_object(path, default=None)
    if not isinstance(data, Mapping):
        return None
    record = dict(data)
    record["_path"] = _path_for_record(paths.project_root, path)
    record.update(_agent_status_path_context(path, paths))
    return record


def _agent_status_path_context(path: Path, paths: WorkflowPaths) -> dict[str, Any]:
    context: dict[str, Any] = {}
    try:
        relative = path.resolve().relative_to(paths.results_dir.resolve())
        parts = relative.parts
        if len(parts) >= 4 and parts[1] == "runs":
            context["_derived_task_id"] = parts[0]
            context["_derived_run_id"] = parts[2]
            if "validator_agent" in parts:
                validator_index = parts.index("validator_agent")
                context["_derived_role"] = "validator"
                context["_parent_run_id"] = parts[2]
                if validator_index + 1 < len(parts):
                    context["_derived_run_id"] = parts[validator_index + 1]
            else:
                context["_derived_role"] = "worker"
            phase = _phase_id_from_task_id(parts[0])
            if phase:
                context["_derived_phase_id"] = phase
            return context
    except (OSError, ValueError):
        pass
    try:
        relative = path.resolve().relative_to((paths.runtime_dir / "runs").resolve())
        parts = relative.parts
        if parts:
            context["_derived_run_id"] = parts[0]
    except (OSError, ValueError):
        pass
    return context


def _phase_id_from_task_id(task_id: str) -> str:
    if "." not in task_id:
        return ""
    phase_id, _task = task_id.split(".", 1)
    return phase_id.strip()


def _collect_agent_statuses(paths: WorkflowPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in (paths.results_dir, paths.runtime_dir / "runs"):
        if not root.exists():
            continue
        for path in sorted(root.glob("**/agent_status.json")):
            record = _node_from_agent_status_path(path, paths)
            if record is not None:
                records.append(record)
    return records


def _collect_node_summaries(paths: WorkflowPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in (paths.results_dir, paths.runtime_dir / "runs", paths.planning_dir / "runs"):
        if not root.exists():
            continue
        for path in sorted(root.glob("**/node_summary.json")):
            data = _read_json_object(path, default=None)
            if not isinstance(data, Mapping):
                continue
            record = dict(data)
            record["_path"] = _path_for_record(paths.project_root, path)
            records.append(record)
    return records


def _read_active_leases(paths: WorkflowPaths) -> list[dict[str, Any]]:
    leases: list[dict[str, Any]] = []
    leases_dir = paths.runtime_dir / "active_run_leases"
    if not leases_dir.exists():
        return leases
    for path in sorted(leases_dir.glob("*.json")):
        data = _read_json_object(path, default=None)
        if not isinstance(data, Mapping):
            continue
        if not _lease_blocks_scheduler(data):
            continue
        status = str(data.get("status") or "").lower()
        if status in {"completed", "succeeded", "failed", "cancelled", "aborted", "released"}:
            continue
        record = dict(data)
        record.setdefault("active_run_lease_path", _path_for_record(paths.project_root, path))
        leases.append(record)
    return leases


def _lease_blocks_scheduler(lease: Mapping[str, Any]) -> bool:
    if lease.get("blocks_scheduler") is False:
        return False
    return str(lease.get("role") or "").strip().lower() != "inspector"


def _read_failure_registry(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    data = _read_json_object(paths.runtime_dir / "failure_registry.json", default={})
    failures = data.get("failures") if isinstance(data, Mapping) else []
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        failures = []
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(data.get("workflow_id") or workflow_id) if isinstance(data, Mapping) else workflow_id,
        "failures": [dict(failure) for failure in failures if isinstance(failure, Mapping)],
    }


def _read_expansion_registry(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    data = _read_json_object(paths.runtime_dir / "expansion_registry.json", default={})
    proposals = data.get("proposals") if isinstance(data, Mapping) else []
    events = data.get("events") if isinstance(data, Mapping) else []
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        proposals = []
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        events = []
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(data.get("workflow_id") or workflow_id) if isinstance(data, Mapping) else workflow_id,
        "cycle": data.get("cycle", 0) if isinstance(data, Mapping) else 0,
        "proposals": [dict(proposal) for proposal in proposals if isinstance(proposal, Mapping)],
        "events": [dict(event) for event in events if isinstance(event, Mapping)],
    }


def _read_event_records(paths: WorkflowPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    events_dir = paths.runtime_dir / "events"
    if not events_dir.exists():
        return records
    for path in sorted(events_dir.glob("*.jsonl")):
        for record in _read_jsonl(path):
            record["_segment"] = _path_for_record(paths.project_root, path)
            records.append(record)
    return sorted(records, key=lambda record: (_event_sequence(record) or 0, str(record.get("event_id") or "")))


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


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _atomic_write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    text = "".join(json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _events_sha256(events_dir: Path) -> str:
    digest = sha256()
    for path in sorted(events_dir.glob("*.jsonl")):
        if not path.is_file():
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _event_replay_summary(event_projection: Mapping[str, Any], *, project: Path | None = None) -> dict[str, Any]:
    snapshot = event_projection.get("snapshot") if isinstance(event_projection.get("snapshot"), Mapping) else {}
    state = event_projection.get("state") if isinstance(event_projection.get("state"), Mapping) else {}
    latest_event = state.get("latest_event") if isinstance(state, Mapping) else None
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_path": _path_for_record(project, Path(str(snapshot.get("path")))) if snapshot.get("path") else None,
        "events_through_sequence": snapshot.get("events_through_sequence"),
        "events_replayed": int(event_projection.get("events_replayed") or 0),
        "latest_event_seq": _event_sequence(latest_event) if isinstance(latest_event, Mapping) else None,
        "latest_event_id": _event_id(latest_event) if isinstance(latest_event, Mapping) else None,
    }


def _validation_status(latest_validation: Mapping[str, Any] | None, latest: Mapping[str, Any] | None, failures: Sequence[Mapping[str, Any]]) -> str | None:
    explicit = _first_string(_mapping_value(latest_validation, "status"), _mapping_value(latest, "validation_status"))
    if explicit:
        return explicit
    unrecovered = [failure for failure in failures if str(failure.get("status") or "") not in TERMINAL_FAILURE_STATUSES]
    if unrecovered:
        return "fail"
    return None


def _task_display(status: str, validation_status: str | None, latest_run_id: str | None, failures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    badge = validation_status if validation_status else status
    subtitle = "No validation recorded."
    if validation_status in {"pass", "pass_with_warnings"}:
        subtitle = "Validation passed."
    elif validation_status:
        subtitle = f"Validation status: {validation_status}."
    elif latest_run_id:
        subtitle = f"Latest run: {latest_run_id}."
    highlight = ""
    if failures:
        highlight = str(failures[-1].get("summary") or failures[-1].get("failure_signature") or "")
    return {"badge": badge, "subtitle": subtitle, "highlight": highlight}


def _summary_from_agent_status(status: Mapping[str, Any]) -> dict[str, Any]:
    candidate = status.get("summary_candidate")
    if isinstance(candidate, Mapping):
        return {
            "one_line": str(candidate.get("one_line") or candidate.get("summary") or status.get("status") or "Run summary."),
            "highlights": list(candidate.get("highlights") or []),
            "risks": list(candidate.get("warnings") or candidate.get("blockers") or []),
        }
    return {"one_line": str(status.get("status") or "Run summary."), "highlights": [], "risks": []}


def _event_message(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type") or "event")
    payload = _event_payload(event)
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    task_id = payload.get("task_id") or _mapping_value(subject, "task_id")
    run_id = event.get("run_id") or payload.get("run_id") or _mapping_value(subject, "run_id")
    runner_id = payload.get("runner_id") or _mapping_value(subject, "runner_id")
    action = _humanize_event_type(event_type)
    actor = _event_actor_label(event)
    parts: list[str] = []
    if task_id:
        parts.append(f"task {task_id}")
    if actor:
        parts.append(actor)
    if runner_id:
        parts.append(f"runner {runner_id}")
    if run_id:
        parts.append(f"run {run_id}")
    sequence = _event_sequence(event)
    prefix = f"Event {sequence}: " if sequence is not None else ""
    if parts:
        return f"{prefix}{action} for {', '.join(parts)}."
    return f"{prefix}{action}."


def _event_title(event: Mapping[str, Any]) -> str:
    sequence = _event_sequence(event)
    action = _humanize_event_type(str(event.get("event_type") or "event"))
    if sequence is None:
        return action
    return f"Event {sequence}: {action}"


def _event_actor_label(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type") or "").strip().lower()
    payload = _event_payload(event)
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    role = str(payload.get("role") or _mapping_value(subject, "role") or "").strip()
    if role:
        return _humanize_event_type(role)
    if event_type.startswith("inspector_"):
        return "Inspector Agent"
    if event_type.startswith("validation_") or event_type.startswith("validator_"):
        return "Validation Agent"
    if event_type.startswith("recovery_"):
        return "Recovery Worker"
    if event_type.startswith("worker_"):
        return "Worker Agent"
    if event_type.startswith("planner_") or "planning" in event_type:
        return "Planner Agent"
    if event_type.startswith("auditor_") or "audit" in event_type:
        return "Auditor Agent"
    if event_type.startswith("final_verifier_") or "final_verification" in event_type:
        return "Final Verifier"
    if event_type.startswith("scheduler_") or event_type.startswith("control_"):
        return "Scheduler"
    if event_type.startswith("approval_"):
        return "Approval Flow"
    return "Workflow"


def _event_context_label(
    *,
    task_id: Any,
    phase: Any,
    actor_label: Any,
    runner_id: Any,
) -> str:
    parts = [str(value).strip() for value in (phase, task_id, actor_label, runner_id) if str(value or "").strip()]
    return " · ".join(parts) if parts else "Workflow"


def _humanize_event_type(value: str) -> str:
    words = str(value or "event").strip().replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:].lower() for word in words) or "Event"


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return payload
    data = event.get("data")
    if isinstance(data, Mapping):
        return data
    return {}


def _event_sequence(event: Mapping[str, Any] | None) -> int | None:
    if not isinstance(event, Mapping):
        return None
    value = event.get("seq", event.get("sequence"))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_id(event: Mapping[str, Any] | None) -> str | None:
    if not isinstance(event, Mapping):
        return None
    value = event.get("event_id") or event.get("id")
    return str(value) if isinstance(value, str) and value else None


def _compact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": _event_sequence(event),
        "event_id": _event_id(event),
        "event_hash": event.get("event_hash"),
        "event_type": event.get("event_type"),
        "ts": event.get("ts") or event.get("timestamp"),
        "workflow_id": event.get("workflow_id"),
        "run_id": event.get("run_id"),
    }


def _event_node_id(event: Mapping[str, Any]) -> str:
    sequence = _event_sequence(event)
    if sequence is not None:
        return f"node_event_{sequence:012d}"
    event_id = _event_id(event)
    return f"node_event_{event_id}" if event_id else f"node_event_{_stable_suffix(event)}"


def _run_node_id(run_id: str) -> str:
    return "node_run_" + _safe_id(run_id)


def _validation_node_id(run_id: str) -> str:
    return "node_validation_" + _safe_id(run_id)


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)


def _stable_suffix(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()[:12]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_dependency_list(value: str) -> list[str]:
    stripped = value.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return []
    inner = stripped[1:-1].strip()
    if not inner:
        return []
    return [item.strip() for item in inner.split(",") if item.strip()]


def _first_pending_task(task_summaries: Sequence[Mapping[str, Any]]) -> str | None:
    terminal = {str(task.get("task_id")) for task in task_summaries if task.get("status") in {"done", "skipped"}}
    for task in task_summaries:
        if task.get("status") not in {"pending", "partial"}:
            continue
        dependencies = [str(dep) for dep in task.get("dependencies") or []]
        if all(dep in terminal for dep in dependencies):
            task_id = task.get("task_id")
            return str(task_id) if task_id else None
    return None


def _first_field(task: PlanTask, field: str) -> str | None:
    values = task.fields.get(field) or ()
    return values[0] if values else None


def _task_latest_display(context: TaskContext) -> str:
    explicit = context.task.latest_path
    if explicit:
        return explicit
    return _path_for_record(context.project_root, context.latest_path)


def _validation_display_path(context: TaskContext) -> str | None:
    if context.validation_path is None:
        return None
    return _first_string(_mapping_value(context.latest, "validation_path"), context.validation_path.as_posix())


def _latest_failure_timestamp(failures: Sequence[Mapping[str, Any]]) -> str | None:
    values = [str(failure.get("last_seen_at") or failure.get("first_seen_at") or "") for failure in failures]
    values = [value for value in values if value]
    return sorted(values)[-1] if values else None


def _compact_failure(failure: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "failure_id": failure.get("failure_id"),
        "status": failure.get("status"),
        "failure_class": failure.get("failure_class"),
        "failure_signature": failure.get("failure_signature"),
        "budget_remaining": failure.get("budget_remaining"),
    }


def _latest_checkpoint(path: Path) -> dict[str, Any] | None:
    records = _read_jsonl(path)
    for record in reversed(records):
        if str(record.get("status") or "") != "created":
            continue
        return {
            "checkpoint_id": record.get("checkpoint_id"),
            "created_at": record.get("created_at"),
            "reason": record.get("reason"),
            "ref": record.get("ref"),
            "commit": record.get("commit"),
            "active_branch_unchanged": record.get("active_branch_unchanged"),
            "head_unchanged": record.get("head_unchanged"),
            "user_index_unchanged": record.get("user_index_unchanged"),
        }
    return None


def _sanitized_repository(repository: Any, *, project: Path | None = None) -> dict[str, Any]:
    if not isinstance(repository, Mapping):
        return {"inside_work_tree": False, "root": None, "head_commit": None, "dirty": False, "dirty_files_count": 0}
    root = None
    if repository.get("root"):
        root = relative_root_value(project, Path(str(repository.get("root")))) if project is not None else str(repository.get("root"))
    return {
        "inside_work_tree": bool(repository.get("inside_work_tree")),
        "root": root,
        "head_commit": repository.get("head_commit"),
        "dirty": bool(repository.get("dirty")),
        "dirty_files_count": int(repository.get("dirty_files_count") or 0),
    }


def _sanitized_version_control_configuration(configuration: Any, *, project: Path | None = None) -> dict[str, Any]:
    if not isinstance(configuration, Mapping):
        return {}
    allowed = (
        "config_found",
        "config_path",
        "enabled",
        "provider",
        "repository_mode",
        "checkpoint_backend",
        "refs_namespace",
        "resolved_refs_namespace",
        "no_remote_push",
        "do_not_switch_user_branch",
        "do_not_modify_user_index",
    )
    result = {key: configuration.get(key) for key in allowed if key in configuration}
    if result.get("config_path"):
        result["config_path"] = _path_for_record(project, Path(str(result["config_path"])))
    return result


def _version_control_problem(doctor: Mapping[str, Any]) -> dict[str, str] | None:
    errors = doctor.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        return {"code": "version_control_unavailable", "reason": "doctor_errors", "message": str(errors[0])}
    return None


def _elapsed_seconds(started_at: Any) -> int | None:
    start = _parse_timestamp(started_at)
    if start is None:
        return None
    return max(0, int((datetime.now(UTC) - start).total_seconds()))


def _active_worker_seconds(run_summaries: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for record in run_summaries:
        start = _parse_timestamp(record.get("started_at"))
        end = _parse_timestamp(record.get("ended_at"))
        if start is not None and end is not None and end >= start:
            total += int((end - start).total_seconds())
    return total


def _parse_timestamp(value: Any) -> datetime | None:
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


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _nested_mapping_value(value: Mapping[str, Any], outer: str, inner: str) -> Any:
    nested = value.get(outer)
    return nested.get(inner) if isinstance(nested, Mapping) else None


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
        if value is not None and not isinstance(value, (Mapping, Sequence)):
            return str(value)
    return None


def _present_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            refs.append(value)
    return refs


def _dedupe_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edge in edges:
        edge_id = str(edge.get("edge_id") or "")
        if edge_id in seen:
            continue
        seen.add(edge_id)
        result.append(dict(edge))
    return sorted(result, key=lambda item: str(item.get("edge_id")))


def _require_mapping(json_models: Mapping[str, Mapping[str, Any]], filename: str, field: str, errors: list[str]) -> None:
    payload = json_models.get(filename)
    if not isinstance(payload, Mapping) or not isinstance(payload.get(field), Mapping):
        errors.append(f"{filename}: missing object field {field}")


def _require_sequence(json_models: Mapping[str, Mapping[str, Any]], filename: str, field: str, errors: list[str]) -> None:
    payload = json_models.get(filename)
    if not isinstance(payload, Mapping) or not isinstance(payload.get(field), Sequence) or isinstance(payload.get(field), (str, bytes)):
        errors.append(f"{filename}: missing array field {field}")


def _path_for_record(project: Path | None, path: Path | None) -> str | None:
    if path is None:
        return None
    if project is not None:
        try:
            return serialize_project_path(project, path)
        except PathSerializationError:
            return None
    return path.as_posix()


def _failure(*, project: Path, started_at: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "failed",
        "project_root": project.as_posix(),
        "workflow_id": None,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "read_models_dir": None,
        "written_files": [],
        "errors": [message],
        "warnings": [],
    }
