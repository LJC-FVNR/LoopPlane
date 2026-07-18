from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
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
from runtime.scheduler import (
    AtomicOwnerLock,
    EVENTS_MANIFEST_FILENAME,
    EVENT_PAYLOAD_SIDECAR_DIR,
    SCHEMA_VERSION,
    SchedulerLockError,
    _event_hash,
    completion_marker_status,
    load_event_log_projection,
    load_event_segment_manifest,
)
from runtime.version_control import run_git_doctor
from runtime.workspace_identity import relative_root_value
from runtime.workflow_lifecycle import WorkflowLifecycleError, ensure_compatibility_workflow_metadata


READ_MODEL_JSON_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "metrics.json",
    "version_control_status.json",
    "run_details_manifest.json",
    "build_manifest.json",
)
READ_MODEL_JSONL_FILES = (
    "dashboard_feed.jsonl",
    "run_index.jsonl",
    "run_summaries.jsonl",
)
READ_MODEL_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "dashboard_feed.jsonl",
    "run_index.jsonl",
    "run_summaries.jsonl",
    "metrics.json",
    "version_control_status.json",
    "run_details_manifest.json",
    "build_manifest.json",
)
READ_MODEL_COMPAT_OPTIONAL_FILES = frozenset({"run_index.jsonl", "run_details_manifest.json", "build_manifest.json"})
READ_MODEL_DETAIL_DIR = "run_details"
READ_MODEL_BUILDER_VERSION = "dashboard-performance-2026-06-24"


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
RUN_DETAIL_BUILD_LIMIT = _read_model_limit_from_env("LOOPPLANE_RUN_DETAIL_BUILD_LIMIT", 200)
READ_MODEL_REBUILD_LOCK_TTL_SECONDS = _read_model_limit_from_env("LOOPPLANE_READ_MODEL_REBUILD_LOCK_TTL_SECONDS", 120)
READ_MODEL_REBUILD_LOCK_WAIT_SECONDS = _read_model_limit_from_env("LOOPPLANE_READ_MODEL_REBUILD_LOCK_WAIT_SECONDS", 30)
SELF_EXPANSION_GRAPH_DETAIL_LIMIT = _read_model_limit_from_env("LOOPPLANE_SELF_EXPANSION_GRAPH_DETAIL_LIMIT", 50)
SELF_EXPANSION_GRAPH_SAMPLE_LIMIT = _read_model_limit_from_env("LOOPPLANE_SELF_EXPANSION_GRAPH_SAMPLE_LIMIT", 8)
OBJECTIVE_VERIFIER_GRAPH_DETAIL_LIMIT = _read_model_limit_from_env("LOOPPLANE_OBJECTIVE_VERIFIER_GRAPH_DETAIL_LIMIT", 80)
OBJECTIVE_VERIFIER_GRAPH_SAMPLE_LIMIT = _read_model_limit_from_env("LOOPPLANE_OBJECTIVE_VERIFIER_GRAPH_SAMPLE_LIMIT", 8)
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
GRAPH_CONTEXT_TASK_RE = re.compile(r"(?i)(?<![A-Z0-9])P(?P<phase>\d+)[._-]?T(?P<task>\d{1,4})(?![A-Z0-9])")
GRAPH_CONTEXT_OBJECTIVE_RE = re.compile(r"(?i)(?<![A-Z0-9])P(?P<phase>\d+)[._-]O(?P<objective>\d{1,4})(?![A-Z0-9])")
GRAPH_CONTEXT_PHASE_RE = re.compile(r"(?i)(?<![A-Z0-9])(?:PHASE[_-]?)?P(?P<phase>\d+)(?![A-Z0-9])")
GRAPH_CONTEXT_SIDECAR_EVENT_TYPES = frozenset(
    {
        "objective_verifier_adapter_started",
        "objective_verifier_run_classified",
        "expansion_planner_adapter_started",
        "expansion_planner_run_classified",
        "self_expansion_requires_attention",
    }
)


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


class _TimingTrace:
    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._spans: list[dict[str, Any]] = []

    @contextmanager
    def span(self, name: str) -> Any:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._spans.append(
                {
                    "name": name,
                    "duration_ms": _duration_ms_since(started),
                }
            )

    def summary(self, *, counts: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "total_duration_ms": _duration_ms_since(self._started),
            "spans": list(self._spans),
            "counts": dict(counts or {}),
        }


def _duration_ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def rebuild_read_models(
    project_root: Path | str,
    *,
    write: bool = True,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    telemetry = _TimingTrace()
    try:
        with telemetry.span("input_loading"):
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
    with telemetry.span("plan_parsing"):
        plan_metadata = _plan_metadata(plan_text)
        workflow_title = _workflow_title_from_metadata(plan_metadata, workflow_id=workflow_id)
        tasks = list(parse_plan_tasks(plan_text).values())
        objectives, objective_parse_errors = parse_plan_objectives(plan_text)
        objective_model = _objective_model(paths, objectives=objectives, parse_errors=objective_parse_errors)
    with telemetry.span("validation_collection"):
        latest_contexts = _task_contexts(project, paths, tasks)
        validation_records = _collect_validation_records(paths)
        validation_manifest = _validation_manifest(project, latest_contexts, validation_records)
    with telemetry.span("event_read"):
        events = _read_event_records(paths, limit=dashboard_event_limit)
        event_total_count = _event_log_record_count(paths, fallback=len(events))
    with telemetry.span("event_projection"):
        event_projection = load_event_log_projection(paths)
    event_state = event_projection.get("state") if isinstance(event_projection, Mapping) else {}
    if not isinstance(event_state, Mapping):
        event_state = {}
    last_event = event_state.get("latest_event")
    if not isinstance(last_event, Mapping):
        last_event = _compact_event(events[-1]) if events else None
    last_event_seq = _event_sequence(last_event) if isinstance(last_event, Mapping) else None
    source_event_id = _event_id(last_event) if isinstance(last_event, Mapping) else None
    graph_context_events = events
    with telemetry.span("graph_context_event_read"):
        if event_total_count is None or event_total_count > len(events) or len(events) >= dashboard_event_limit:
            graph_context_events = _read_event_records(paths, limit=None)
        graph_context_events = _hydrate_graph_context_event_payloads(paths, graph_context_events)
    with telemetry.span("active_lease_read"):
        active_leases = _read_active_leases(paths)
    with telemetry.span("source_hash_construction"):
        source_hashes = _source_hashes(
            paths,
            plan_source=plan_source,
            events=events,
            event_total_count=event_total_count,
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

    with telemetry.span("runtime_control_input_read"):
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
    with telemetry.span("node_summary_collection"):
        node_summaries = _collect_node_summaries(paths)
        agent_statuses = _collect_agent_statuses(paths)
    with telemetry.span("task_summary_construction"):
        detail_run_ids = _run_detail_build_run_ids(
            node_summaries=node_summaries,
            agent_statuses=agent_statuses,
            validation_records=validation_records,
            active_leases=active_leases,
            limit=RUN_DETAIL_BUILD_LIMIT,
        )
        task_summaries = _task_summaries(
            latest_contexts,
            failures=failure_registry.get("failures", []),
            expansion_tasks=_mapping_or_empty(expansion_index.get("tasks")),
        )
    with telemetry.span("previous_build_manifest_read"):
        previous_build = _previous_build_manifest(paths, workflow_id=workflow_id)
        previous_details = _previous_run_detail_cache(paths, previous_build)
    with telemetry.span("run_summary_construction"):
        run_summaries = _build_run_summaries(
            common=common,
            paths=paths,
            events=events,
            node_summaries=node_summaries,
            agent_statuses=agent_statuses,
            validation_records=validation_records,
            active_leases=active_leases,
            detail_run_ids=detail_run_ids,
            previous_details=previous_details,
        )
        run_index = [_run_index_record(paths, record) for record in run_summaries]

    with telemetry.span("workflow_status_model"):
        workflow_status_model = _workflow_status_model(
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
        )
    with telemetry.span("plan_index_model"):
        plan_index_model = _plan_index_model(
            common=common,
            paths=paths,
            plan_source=plan_source,
            tasks=tasks,
            task_summaries=task_summaries,
            objective_model=objective_model,
            expansion_phases=_mapping_or_empty(expansion_index.get("phases")),
        )
    with telemetry.span("workflow_graph_model"):
        workflow_graph_model = _workflow_graph_model(
            common=common,
            plan_phases=_sequence(plan_index_model.get("phases")),
            events=events,
            context_events=graph_context_events,
            event_limit=dashboard_event_limit,
            node_summaries=node_summaries,
            agent_statuses=agent_statuses,
            validation_records=validation_records,
            active_leases=active_leases,
            task_summaries=task_summaries,
            objective_model=objective_model,
            expansion_registry=expansion_registry,
            event_total_count=event_total_count,
        )
    with telemetry.span("metrics_model"):
        metrics_model = _metrics_model(
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
        )
    with telemetry.span("version_control_status_model"):
        version_control_status_model = _version_control_status_model(
            common=common,
            project=project,
            paths=paths,
        )
    with telemetry.span("run_details_manifest_model"):
        run_details_manifest_model = _run_details_manifest_model(common=common, paths=paths, run_summaries=run_summaries)
    with telemetry.span("build_manifest_model"):
        build_manifest_model = _build_manifest_model(
            common=common,
            paths=paths,
            source_hashes=source_hashes,
            run_summaries=run_summaries,
            event_projection=event_projection,
        )
    json_models = {
        "workflow_status.json": workflow_status_model,
        "plan_index.json": plan_index_model,
        "workflow_graph.json": workflow_graph_model,
        "metrics.json": metrics_model,
        "version_control_status.json": version_control_status_model,
        "run_details_manifest.json": run_details_manifest_model,
        "build_manifest.json": build_manifest_model,
    }
    with telemetry.span("dashboard_feed_model"):
        dashboard_feed = _dashboard_feed_records(common=common, events=events, event_limit=dashboard_event_limit)
    jsonl_models = {
        "dashboard_feed.jsonl": dashboard_feed,
        "run_index.jsonl": run_index,
        "run_summaries.jsonl": [_legacy_run_summary_record(paths, record) for record in run_summaries],
    }
    with telemetry.span("schema_validation"):
        validation = validate_read_models(json_models, jsonl_models)
    diagnostics_counts = _read_model_rebuild_diagnostic_counts(
        paths,
        events=events,
        graph_context_events=graph_context_events,
        event_total_count=event_total_count,
        run_summaries=run_summaries,
    )
    diagnostics_counts["run_detail_records_reused"] = sum(1 for record in run_summaries if record.get("detail_reused") is True)
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
            "diagnostics": telemetry.summary(counts=diagnostics_counts),
            "errors": list(validation["errors"]),
            "warnings": list(plan_source.warnings),
        }

    written: list[str] = []
    if write:
        with telemetry.span("write_lock"):
            try:
                held_lock, lock_counts = _acquire_read_model_rebuild_lock(paths)
            except SchedulerLockError as error:
                diagnostics_counts["write_lock_acquired"] = False
                return {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "status": "read_model_rebuild_locked",
                    "project_root": project.as_posix(),
                    "workflow_id": workflow_id,
                    "started_at": started_at,
                    "ended_at": utc_timestamp(),
                    "read_models_dir": _path_for_record(project, paths.read_models_dir),
                    "read_model_files": list(READ_MODEL_FILES),
                    "written_files": [],
                    "event_replay": _event_replay_summary(event_projection, project=project),
                    "max_dashboard_events": dashboard_event_limit,
                    "schema_validation": validation,
                    "source_hashes": source_hashes,
                    "diagnostics": telemetry.summary(counts=diagnostics_counts),
                    "errors": [str(error)],
                    "warnings": list(plan_source.warnings),
                }
            diagnostics_counts.update(lock_counts)
        with held_lock:
            with telemetry.span("writes"):
                paths.read_models_dir.mkdir(parents=True, exist_ok=True)
                model_write_counts = {
                    "read_model_files_written": 0,
                    "read_model_files_unchanged": 0,
                }
                for filename, payload in json_models.items():
                    model_path = paths.read_models_dir / filename
                    if _atomic_write_json_if_changed(model_path, payload):
                        written.append(_path_for_record(project, model_path))
                        model_write_counts["read_model_files_written"] += 1
                    else:
                        model_write_counts["read_model_files_unchanged"] += 1
                for filename, records in jsonl_models.items():
                    model_path = paths.read_models_dir / filename
                    if _atomic_write_jsonl_if_changed(model_path, records):
                        written.append(_path_for_record(project, model_path))
                        model_write_counts["read_model_files_written"] += 1
                    else:
                        model_write_counts["read_model_files_unchanged"] += 1
                diagnostics_counts.update(model_write_counts)
                detail_write_result = _write_run_detail_models(paths, run_summaries=run_summaries)
                written.extend(detail_write_result["written_files"])
                diagnostics_counts.update(detail_write_result["counts"])
    no_write_comparison: dict[str, Any] | None = None
    if not write:
        with telemetry.span("no_write_comparison"):
            no_write_comparison = _read_model_no_write_comparison(
                paths,
                json_models=json_models,
                jsonl_models=jsonl_models,
                run_summaries=run_summaries,
            )
            diagnostics_counts["no_write_compare_changed_files"] = len(no_write_comparison["changed_files"])
            diagnostics_counts["no_write_compare_missing_files"] = len(no_write_comparison["missing_files"])
            diagnostics_counts["no_write_compare_invalid_files"] = len(no_write_comparison["invalid_files"])
            diagnostics_counts["no_write_compare_extra_run_detail_files"] = len(no_write_comparison["extra_run_detail_files"])
    diagnostics_counts["written_files"] = len(written)
    diagnostics_counts["written_bytes"] = _written_file_bytes(project, written)

    result = {
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
        "diagnostics": telemetry.summary(counts=diagnostics_counts),
        "errors": [],
        "warnings": list(plan_source.warnings),
    }
    if no_write_comparison is not None:
        result["no_write_comparison"] = no_write_comparison
    return result


def strict_read_model_diagnostics(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    compare_rebuild: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    started = time.perf_counter()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, WorkflowPathError, ValueError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "strict": True,
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "read_models_dir": None,
            "checks": {},
            "errors": [str(error)],
            "warnings": [],
            "diagnostics": {"total_duration_ms": _duration_ms_since(started)},
        }

    json_models, jsonl_models, load_check = _strict_load_read_models(paths)
    errors.extend(str(error) for error in load_check["errors"])
    warnings.extend(str(warning) for warning in load_check["warnings"])

    schema_validation = validate_read_models(json_models, jsonl_models)
    if schema_validation.get("status") != "pass":
        errors.extend(f"read-model schema: {error}" for error in schema_validation.get("errors", []))

    events_dir = paths.runtime_dir / "events"
    events = _read_event_records(paths, limit=None)
    event_manifest = load_event_segment_manifest(events_dir)
    event_chain = _strict_event_chain_diagnostics(events)
    if event_chain["hash_mismatches"] or event_chain["prev_hash_mismatches"] or event_chain["sequence_gaps"]:
        errors.append("event chain integrity check failed")

    event_checks: dict[str, Any] = {
        "events_sha256": _events_sha256(events_dir),
        "event_count": len(events),
        "segment_count": _event_segment_count(events_dir),
        "manifest_available": isinstance(event_manifest, Mapping),
        "manifest_path": _path_for_record(project, events_dir / EVENTS_MANIFEST_FILENAME),
        "manifest_event_count": event_manifest.get("event_count") if isinstance(event_manifest, Mapping) else None,
        "manifest_matches_event_count": (
            int(event_manifest.get("event_count") or -1) == len(events) if isinstance(event_manifest, Mapping) else None
        ),
        "chain": event_chain,
    }
    if event_checks["manifest_matches_event_count"] is False:
        errors.append("event segment manifest event_count does not match event log")

    sidecars = _strict_event_payload_sidecar_diagnostics(project, paths, events)
    if sidecars["missing"] or sidecars["mismatch"] or sidecars["invalid"]:
        errors.append("event payload sidecar integrity check failed")

    read_model_hashes = _strict_read_model_source_hash_diagnostics(json_models, jsonl_models, event_checks["events_sha256"])
    if read_model_hashes["events_sha256_mismatches"]:
        errors.append("read-model source_hashes events_sha256 mismatch")

    run_details = _strict_run_detail_diagnostics(project, paths, json_models.get("run_details_manifest.json"))
    if run_details["status"] != "pass":
        errors.append("run detail manifest integrity check failed")
    warnings.extend(str(warning) for warning in run_details.get("warnings", []))

    no_write_rebuild: dict[str, Any] | None = None
    if compare_rebuild:
        no_write_rebuild = _strict_no_write_rebuild_comparison(project, workflow_id=paths.workflow_id)
        if no_write_rebuild.get("status") != "pass":
            errors.append("strict no-write rebuild comparison failed")

    ok = not errors
    checks: dict[str, Any] = {
        "read_model_load": load_check,
        "read_model_schema": schema_validation,
        "events": event_checks,
        "payload_sidecars": sidecars,
        "read_model_source_hashes": read_model_hashes,
        "run_details": run_details,
    }
    if no_write_rebuild is not None:
        checks["no_write_rebuild_comparison"] = no_write_rebuild
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "strict": True,
        "project_root": project.as_posix(),
        "workflow_id": paths.workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "read_models_dir": _path_for_record(project, paths.read_models_dir),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "diagnostics": {"total_duration_ms": _duration_ms_since(started)},
    }


def split_legacy_run_summaries(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError, ValueError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "read_models_dir": None,
            "modified_files": [],
            "errors": [str(error)],
            "warnings": [],
        }
    legacy_path = paths.read_models_dir / "run_summaries.jsonl"
    if not legacy_path.is_file():
        return _split_legacy_run_summaries_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="legacy_missing",
            dry_run=dry_run,
            errors=[f"{_path_for_record(project, legacy_path)} is missing."],
        )
    if (paths.read_models_dir / "run_index.jsonl").is_file() and (paths.read_models_dir / "run_details_manifest.json").is_file():
        return _split_legacy_run_summaries_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="current",
            dry_run=dry_run,
        )
    records = _read_jsonl_strict(legacy_path)
    if records is None:
        return _split_legacy_run_summaries_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="invalid_legacy_run_summaries",
            dry_run=dry_run,
            errors=[f"{_path_for_record(project, legacy_path)} is not valid JSONL."],
        )
    normalized = [_legacy_run_summary_for_split(record, paths=paths, generated_at=started_at) for record in records]
    invalid_run_indexes = [index for index, record in enumerate(normalized, start=1) if not str(record.get("run_id") or "")]
    duplicate_run_ids = _duplicate_legacy_run_ids(normalized)
    if invalid_run_indexes or duplicate_run_ids:
        errors = []
        if invalid_run_indexes:
            errors.append("run_summaries.jsonl records missing run_id at line(s): " + ", ".join(map(str, invalid_run_indexes)))
        if duplicate_run_ids:
            errors.append("run_summaries.jsonl contains duplicate run_id value(s): " + ", ".join(duplicate_run_ids))
        return _split_legacy_run_summaries_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="invalid_legacy_run_summaries",
            dry_run=dry_run,
            errors=errors,
        )
    run_index = [_run_index_record(paths, record) for record in normalized]
    common = _legacy_split_common(paths, normalized, generated_at=started_at)
    run_details_manifest = _run_details_manifest_model(common=common, paths=paths, run_summaries=normalized)
    build_manifest = _legacy_split_build_manifest(common=common, paths=paths, run_summaries=normalized)
    detail_records = [record for record in normalized if record.get("detail_status") == "available"]
    modified_files = [
        _path_for_record(project, paths.read_models_dir / "run_index.jsonl"),
        _path_for_record(project, paths.read_models_dir / "run_details_manifest.json"),
        _path_for_record(project, paths.read_models_dir / "build_manifest.json"),
        *(_path_for_record(project, _run_detail_path(paths, str(record.get("run_id") or ""))) for record in detail_records),
    ]
    modified_files = [path for path in modified_files if path is not None]
    if not dry_run:
        paths.read_models_dir.mkdir(parents=True, exist_ok=True)
        (paths.read_models_dir / READ_MODEL_DETAIL_DIR).mkdir(parents=True, exist_ok=True)
        _atomic_write_jsonl(paths.read_models_dir / "run_index.jsonl", run_index)
        _atomic_write_json(paths.read_models_dir / "run_details_manifest.json", run_details_manifest)
        _atomic_write_json(paths.read_models_dir / "build_manifest.json", build_manifest)
        for record in detail_records:
            _atomic_write_json(_run_detail_path(paths, str(record.get("run_id") or "")), _run_detail_record(record))
    return _split_legacy_run_summaries_result(
        project=project,
        paths=paths,
        started_at=started_at,
        status="planned" if dry_run else "split",
        dry_run=dry_run,
        run_count=len(run_index),
        detail_count=len(detail_records),
        modified_files=modified_files if not dry_run else [],
        planned_files=modified_files,
    )


def _legacy_run_summary_for_split(record: Mapping[str, Any], *, paths: WorkflowPaths, generated_at: str) -> dict[str, Any]:
    run_id = str(record.get("run_id") or "")
    normalized = dict(record)
    normalized.setdefault("schema_version", SCHEMA_VERSION)
    normalized.setdefault("workflow_id", paths.workflow_id)
    normalized.setdefault("generated_at", generated_at)
    normalized.setdefault("source_hashes", {})
    normalized.setdefault("node_id", _run_node_id(run_id))
    details = normalized.get("details")
    if isinstance(details, Mapping):
        normalized["details"] = _legacy_detail_payload_for_split(details, run_id=run_id, task_id=normalized.get("task_id"))
        normalized["detail_status"] = "available"
        if not _valid_legacy_detail_source(normalized.get("detail_source")):
            normalized["detail_source"] = _legacy_detail_source(normalized)
    else:
        normalized["detail_status"] = str(normalized.get("detail_status") or "not_built")
    return normalized


def _valid_legacy_detail_source(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("schema_version") == SCHEMA_VERSION
        and isinstance(value.get("fingerprint"), str)
        and str(value.get("fingerprint") or "").startswith("sha256:")
        and isinstance(value.get("files"), Sequence)
        and not isinstance(value.get("files"), (str, bytes, bytearray))
    )


def _duplicate_legacy_run_ids(records: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for record in records:
        run_id = str(record.get("run_id") or "")
        if not run_id:
            continue
        if run_id in seen:
            duplicates.add(run_id)
        seen.add(run_id)
    return sorted(duplicates)


def _legacy_detail_payload_for_split(details: Mapping[str, Any], *, run_id: str, task_id: Any) -> dict[str, Any]:
    payload = dict(details)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("run_id", run_id)
    if task_id is not None:
        payload.setdefault("task_id", task_id)
    payload.setdefault("sections", [])
    payload.setdefault("available_sections", [])
    payload.setdefault("missing_sections", [])
    return payload


def _legacy_detail_source(record: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        str(key): value
        for key, value in record.items()
        if str(key) not in {"details", "generated_at", "detail_source", "detail_reused"}
    }
    digest = sha256(json.dumps(compact, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "fingerprint": "sha256:" + digest, "files": []}


def _legacy_split_common(paths: WorkflowPaths, records: Sequence[Mapping[str, Any]], *, generated_at: str) -> dict[str, Any]:
    first = records[0] if records else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(first.get("workflow_id") or paths.workflow_id),
        "generated_at": generated_at,
        "source_hashes": dict(_mapping_or_empty(first.get("source_hashes"))),
        "last_event_seq": first.get("source_event_seq") or first.get("last_event_seq"),
        "source_event_id": first.get("source_event_id"),
    }


def _legacy_split_build_manifest(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run_details = []
    for record in run_summaries:
        run_id = str(record.get("run_id") or "")
        if not run_id or record.get("detail_status") != "available":
            continue
        run_details.append(
            {
                "run_id": run_id,
                "path": _path_for_record(paths.project_root, _run_detail_path(paths, run_id)),
                "detail_source": dict(_mapping_or_empty(record.get("detail_source"))),
                "detail_reused": False,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": common["workflow_id"],
        "generated_at": common["generated_at"],
        "last_event_seq": common.get("last_event_seq"),
        "source_event_id": common.get("source_event_id"),
        "builder_version": READ_MODEL_BUILDER_VERSION,
        "source_hashes": dict(_mapping_or_empty(common.get("source_hashes"))),
        "event_replay": {},
        "event_segment_manifest": _event_segment_manifest_ref(paths),
        "run_details": run_details,
    }


def _split_legacy_run_summaries_result(
    *,
    project: Path,
    paths: WorkflowPaths,
    started_at: str,
    status: str,
    dry_run: bool,
    run_count: int = 0,
    detail_count: int = 0,
    modified_files: Sequence[str] | None = None,
    planned_files: Sequence[str] | None = None,
    errors: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": paths.workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "read_models_dir": _path_for_record(project, paths.read_models_dir),
        "dry_run": dry_run,
        "run_count": run_count,
        "detail_count": detail_count,
        "planned_files": list(planned_files or []),
        "modified_files": list(modified_files or []),
        "errors": list(errors or []),
        "warnings": [],
    }


def read_model_rebuild_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def _acquire_read_model_rebuild_lock(paths: WorkflowPaths) -> tuple[Any, dict[str, Any]]:
    owner = f"read-models:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(
        paths.runtime_dir / "lock" / "read_model_rebuild_lock",
        owner,
        ttl_seconds=READ_MODEL_REBUILD_LOCK_TTL_SECONDS,
    )
    started = time.perf_counter()
    attempts = 0
    wait_seconds = max(0, READ_MODEL_REBUILD_LOCK_WAIT_SECONDS)
    while True:
        attempts += 1
        try:
            held = lock.acquire()
            return held, {
                "write_lock_acquired": True,
                "write_lock_attempts": attempts,
                "write_lock_wait_ms": _duration_ms_since(started),
            }
        except SchedulerLockError:
            if (time.perf_counter() - started) >= wait_seconds:
                raise
            time.sleep(min(0.1, max(0.01, wait_seconds / 10 if wait_seconds else 0.01)))


def _write_run_detail_models(
    paths: WorkflowPaths,
    *,
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run_details_dir = paths.read_models_dir / READ_MODEL_DETAIL_DIR
    run_details_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    current_paths: set[Path] = set()
    counts = {
        "run_detail_files_written": 0,
        "run_detail_files_reused_on_disk": 0,
        "run_detail_files_removed": 0,
    }
    for record in run_summaries:
        run_id = str(record.get("run_id") or "")
        if not run_id or record.get("detail_status") != "available":
            continue
        detail_path = _run_detail_path(paths, run_id)
        try:
            current_paths.add(detail_path.resolve())
        except OSError:
            current_paths.add(detail_path.absolute())
        detail_record = _run_detail_record(record)
        if record.get("detail_reused") is True and detail_path.is_file():
            existing = _read_json_object(detail_path, default=None)
            if (
                isinstance(existing, Mapping)
                and _read_model_comparison_value(existing)
                == _read_model_comparison_value(detail_record)
            ):
                counts["run_detail_files_reused_on_disk"] += 1
                continue
        if _atomic_write_json_if_changed(detail_path, detail_record):
            written.append(_path_for_record(paths.project_root, detail_path))
            counts["run_detail_files_written"] += 1
        else:
            counts["run_detail_files_reused_on_disk"] += 1
    for existing in sorted(run_details_dir.glob("*.json")):
        try:
            existing_key = existing.resolve()
        except OSError:
            existing_key = existing.absolute()
        if existing_key in current_paths:
            continue
        try:
            existing.unlink()
        except OSError:
            continue
        counts["run_detail_files_removed"] += 1
    return {"written_files": written, "counts": counts}


def _read_model_rebuild_diagnostic_counts(
    paths: WorkflowPaths,
    *,
    events: Sequence[Mapping[str, Any]],
    graph_context_events: Sequence[Mapping[str, Any]],
    event_total_count: int | None,
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    preview_stats = _run_detail_preview_stats(run_summaries)
    event_stats = _event_segment_file_stats(paths)
    detail_records = [record for record in run_summaries if record.get("detail_status") == "available"]
    return {
        "events_loaded": len(events),
        "graph_context_events_loaded": len(graph_context_events),
        "events_total_count": event_total_count,
        "event_segment_files": event_stats["files"],
        "event_segment_bytes": event_stats["bytes"],
        "run_records": len(run_summaries),
        "run_detail_records_built": len(detail_records),
        **preview_stats,
    }


def _event_segment_file_stats(paths: WorkflowPaths) -> dict[str, int]:
    files = 0
    bytes_total = 0
    events_dir = paths.runtime_dir / "events"
    try:
        candidates = sorted(events_dir.glob("*.jsonl"))
    except OSError:
        return {"files": 0, "bytes": 0}
    for path in candidates:
        if not path.is_file():
            continue
        files += 1
        try:
            bytes_total += path.stat().st_size
        except OSError:
            continue
    return {"files": files, "bytes": bytes_total}


def _run_detail_preview_stats(run_summaries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    stats = {
        "detail_file_preview_records": 0,
        "detail_source_file_bytes": 0,
        "detail_full_sha256_unavailable": 0,
    }

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if "size_bytes" in value and "path" in value:
                stats["detail_file_preview_records"] += 1
                try:
                    stats["detail_source_file_bytes"] += max(0, int(value.get("size_bytes") or 0))
                except (TypeError, ValueError):
                    pass
                if value.get("full_sha256_available") is False:
                    stats["detail_full_sha256_unavailable"] += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                visit(child)

    for record in run_summaries:
        if record.get("detail_status") == "available":
            visit(record.get("details"))
    return stats


def _written_file_bytes(project: Path, written_files: Sequence[str]) -> int:
    total = 0
    for value in written_files:
        path = Path(value)
        if not path.is_absolute():
            path = project / path
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


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
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        total_ms = diagnostics.get("total_duration_ms")
        if total_ms is not None:
            lines.append(f"diagnostics_total_ms: {total_ms}")
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def format_strict_read_model_diagnostics_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane read-model strict diagnostics: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"read_models_dir: {result.get('read_models_dir') or 'unknown'}",
    ]
    checks = result.get("checks")
    if isinstance(checks, Mapping):
        read_model_load = checks.get("read_model_load")
        if isinstance(read_model_load, Mapping):
            lines.append(f"read_model_load: {read_model_load.get('status', 'unknown')}")
        read_model_schema = checks.get("read_model_schema")
        if isinstance(read_model_schema, Mapping):
            lines.append(f"read_model_schema: {read_model_schema.get('status', 'unknown')}")
        events = checks.get("events")
        if isinstance(events, Mapping):
            lines.append(f"events_sha256: {events.get('events_sha256') or 'unknown'}")
            lines.append(f"event_count: {events.get('event_count')}")
            chain = events.get("chain")
            if isinstance(chain, Mapping):
                lines.append(f"event_chain: {chain.get('status', 'unknown')}")
        sidecars = checks.get("payload_sidecars")
        if isinstance(sidecars, Mapping):
            lines.append(
                "payload_sidecars: "
                f"{sidecars.get('status', 'unknown')} "
                f"(referenced={sidecars.get('referenced', 0)}, checked={sidecars.get('checked', 0)})"
            )
        source_hashes = checks.get("read_model_source_hashes")
        if isinstance(source_hashes, Mapping):
            missing = source_hashes.get("events_sha256_missing_files")
            if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes)):
                lines.append(f"events_sha256_missing_files: {len(missing)}")
        run_details = checks.get("run_details")
        if isinstance(run_details, Mapping):
            lines.append(
                "run_details: "
                f"{run_details.get('status', 'unknown')} "
                f"(checked={run_details.get('checked', 0)}, missing={run_details.get('missing', 0)})"
            )
        rebuild_compare = checks.get("no_write_rebuild_comparison")
        if isinstance(rebuild_compare, Mapping):
            comparison = rebuild_compare.get("comparison")
            changed = len(comparison.get("changed_files") or []) if isinstance(comparison, Mapping) else 0
            missing = len(comparison.get("missing_files") or []) if isinstance(comparison, Mapping) else 0
            lines.append(
                "no_write_rebuild_comparison: "
                f"{rebuild_compare.get('status', 'unknown')} "
                f"(changed={changed}, missing={missing})"
            )
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("total_duration_ms") is not None:
        lines.append(f"diagnostics_total_ms: {diagnostics.get('total_duration_ms')}")
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
        closed_at = _objective_closed_at(report, result)
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
            "closed_at": closed_at if closed else None,
            "started_at": closed_at if closed else None,
            "ended_at": closed_at if closed else None,
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


def _objective_closed_at(report: Any, result: Any) -> str | None:
    report_map = _mapping_or_empty(report)
    result_map = _mapping_or_empty(result)
    return _first_timestamp_string(
        result_map.get("ended_at"),
        result_map.get("completed_at"),
        result_map.get("verified_at"),
        result_map.get("validated_at"),
        result_map.get("updated_at"),
        report_map.get("ended_at"),
        report_map.get("completed_at"),
        report_map.get("verified_at"),
        report_map.get("validated_at"),
        report_map.get("updated_at"),
        report_map.get("generated_at"),
    )


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


def _phase_node_id(phase_id: str) -> str:
    return "node_phase_" + _safe_id(phase_id or "unphased")


def _task_node_id(task_id: str) -> str:
    return "node_task_" + _safe_id(task_id)


def _objective_node_id(objective_id: str) -> str:
    return "objective_" + _safe_id(objective_id)


def _graph_edge_id(edge_type: str, source: str, target: str, discriminator: str = "") -> str:
    suffix = _stable_suffix({"type": edge_type, "source": source, "target": target, "discriminator": discriminator})
    return f"edge_{_safe_id(edge_type)}_{suffix}"


def _graph_edge(
    edge_type: str,
    source: str,
    target: str,
    *,
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    label: str | None = None,
    layer: str | None = None,
    status: Any = None,
    created_at: Any = None,
    ended_at: Any = None,
    hidden_by_default: bool = False,
    priority: int | None = None,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    discriminator: str = "",
) -> dict[str, Any]:
    source_node = _mapping_or_empty(nodes_by_id.get(source))
    target_node = _mapping_or_empty(nodes_by_id.get(target))
    edge: dict[str, Any] = {
        "edge_id": _graph_edge_id(edge_type, source, target, discriminator),
        "source": source,
        "target": target,
        "type": edge_type,
        "label": label or edge_type.replace("_", " "),
        "source_type": str(source_node.get("type") or ""),
        "target_type": str(target_node.get("type") or ""),
        "hidden_by_default": hidden_by_default,
    }
    if layer:
        edge["layer"] = layer
    if status not in (None, ""):
        edge["status"] = status
    if created_at not in (None, ""):
        edge["created_at"] = created_at
    if ended_at not in (None, ""):
        edge["ended_at"] = ended_at
    if priority is not None:
        edge["priority"] = priority
    evidence_values = [dict(item) for item in evidence or [] if isinstance(item, Mapping)]
    if evidence_values:
        edge["evidence"] = evidence_values
    return edge


def _append_graph_edge(
    edges: list[dict[str, Any]],
    edge_type: str,
    source: str,
    target: str,
    *,
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    require_nodes: bool = True,
    **kwargs: Any,
) -> None:
    if not source or not target or source == target:
        return
    if require_nodes and (source not in nodes_by_id or target not in nodes_by_id):
        return
    edges.append(_graph_edge(edge_type, source, target, nodes_by_id=nodes_by_id, **kwargs))


def _plan_phase_lookup(plan_phases: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    phases_by_id: dict[str, Mapping[str, Any]] = {}
    task_to_phase: dict[str, str] = {}
    for raw_phase in plan_phases:
        phase = _mapping_or_empty(raw_phase)
        phase_id = str(phase.get("phase_id") or _phase_id(str(phase.get("title") or "Unphased")))
        if not phase_id:
            continue
        phases_by_id[phase_id] = phase
        for raw_task in _sequence(phase.get("tasks")):
            task = _mapping_or_empty(raw_task)
            task_id = str(task.get("task_id") or "")
            if task_id:
                task_to_phase[task_id] = phase_id
    return phases_by_id, task_to_phase


def _task_graph_started_at(task: Mapping[str, Any]) -> str | None:
    return _first_timestamp_string(
        task.get("started_at"),
        task.get("prepared_at"),
        task.get("created_at"),
        _timestamp_from_run_id(task.get("latest_run_id")),
        task.get("last_updated_at") if str(task.get("status") or "") in {"done", "skipped", "blocked"} else None,
    )


def _task_graph_ended_at(task: Mapping[str, Any]) -> str | None:
    if str(task.get("status") or "") not in {"done", "skipped", "blocked"}:
        return None
    return _first_timestamp_string(
        task.get("ended_at"),
        task.get("completed_at"),
        task.get("finished_at"),
        task.get("validated_at"),
        task.get("last_updated_at"),
    )


def _phase_graph_timing(tasks: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    starts: list[tuple[datetime, str]] = []
    ends: list[tuple[datetime, str]] = []
    for task in tasks:
        started_at = _task_graph_started_at(task)
        ended_at = _task_graph_ended_at(task)
        started = _parse_timestamp(started_at)
        ended = _parse_timestamp(ended_at)
        if started is not None and started_at is not None:
            starts.append((started, started_at))
        if ended is not None and ended_at is not None:
            ends.append((ended, ended_at))
    starts.sort(key=lambda item: item[0])
    ends.sort(key=lambda item: item[0])
    return {
        "started_at": starts[0][1] if starts else None,
        "ended_at": ends[-1][1] if ends else None,
    }


def _add_plan_graph_nodes(
    nodes_by_id: dict[str, dict[str, Any]],
    *,
    plan_phases: Sequence[Mapping[str, Any]],
    task_summaries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    phases_by_id, task_to_phase = _plan_phase_lookup(plan_phases)
    task_summaries_by_phase: dict[str, list[Mapping[str, Any]]] = {}
    for task in task_summaries:
        task_id = str(task.get("task_id") or "")
        phase_title = str(task.get("phase") or "")
        phase_id = task_to_phase.get(task_id) or _phase_id(phase_title or "Unphased")
        task_summaries_by_phase.setdefault(phase_id, []).append(task)
    for phase_id, phase in phases_by_id.items():
        title = str(phase.get("title") or phase_id)
        tasks = [_mapping_or_empty(task) for task in _sequence(phase.get("tasks"))]
        node_id = _phase_node_id(phase_id)
        timing = _phase_graph_timing(task_summaries_by_phase.get(phase_id, []))
        nodes_by_id.setdefault(
            node_id,
            _node_with_timing(
                {
                    "node_id": node_id,
                    "type": "phase",
                    "kind": "phase",
                    "layer": "plan",
                    "status": phase.get("status") or "planned",
                    "title": title,
                    "display_label": phase_id,
                    "phase_id": phase_id,
                    "phase": title,
                    "started_at": timing.get("started_at"),
                    "ended_at": timing.get("ended_at"),
                    "pipeline_visible": False,
                    "importance": 100,
                    "task_count": len(tasks),
                    "summary": {
                        "one_line": f"Phase {phase_id}: {title}",
                        "highlights": [f"{len(tasks)} task(s)."],
                        "risks": [],
                    },
                    "input_refs": [],
                    "output_refs": [],
                }
            ),
        )
    for task in task_summaries:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        phase_title = str(task.get("phase") or "")
        phase_id = task_to_phase.get(task_id) or _phase_id(phase_title or "Unphased")
        task_to_phase[task_id] = phase_id
        if phase_id and phase_id not in phases_by_id:
            phases_by_id[phase_id] = {"phase_id": phase_id, "title": phase_title or phase_id, "tasks": []}
            node_id = _phase_node_id(phase_id)
            timing = _phase_graph_timing(task_summaries_by_phase.get(phase_id, [task]))
            nodes_by_id.setdefault(
                node_id,
                _node_with_timing(
                    {
                        "node_id": node_id,
                        "type": "phase",
                        "kind": "phase",
                        "layer": "plan",
                        "status": "planned",
                        "title": phase_title or phase_id,
                        "display_label": phase_id,
                        "phase_id": phase_id,
                        "phase": phase_title or phase_id,
                        "started_at": timing.get("started_at"),
                        "ended_at": timing.get("ended_at"),
                        "pipeline_visible": False,
                        "importance": 100,
                        "summary": {
                            "one_line": f"Phase {phase_id}: {phase_title or phase_id}",
                            "highlights": [],
                            "risks": [],
                        },
                        "input_refs": [],
                        "output_refs": [],
                    }
                ),
            )
        node_id = _task_node_id(task_id)
        started_at = _task_graph_started_at(task)
        ended_at = _task_graph_ended_at(task)
        nodes_by_id.setdefault(
            node_id,
            _node_with_timing(
                {
                    "node_id": node_id,
                    "type": "task",
                    "kind": "task",
                    "layer": "plan",
                    "status": task.get("status") or "unknown",
                    "title": str(task.get("title") or task_id),
                    "display_label": task_id,
                    "phase_id": phase_id,
                    "phase": phase_title or phase_id,
                    "task_id": task_id,
                    "pipeline_visible": False,
                    "importance": 90,
                    "deliverables": task.get("deliverables"),
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "summary": {
                        "one_line": f"Task {task_id}: {task.get('title') or task.get('status') or 'planned'}",
                        "highlights": [str(task.get("deliverables") or "")] if task.get("deliverables") else [],
                        "risks": [],
                    },
                    "input_refs": [],
                    "output_refs": _present_refs(task.get("latest_path"), task.get("validation_path")),
                }
            ),
        )
    return phases_by_id, task_to_phase


def _annotate_pipeline_visibility(nodes_by_id: Mapping[str, dict[str, Any]]) -> None:
    for node in nodes_by_id.values():
        node_type = str(node.get("type") or "").strip().lower()
        if node_type in {"phase", "task"}:
            node["pipeline_visible"] = False
        else:
            node.setdefault("pipeline_visible", True)


def _new_run_graph_context() -> dict[str, list[str]]:
    return {
        "phase_ids": [],
        "target_task_ids": [],
        "target_objective_ids": [],
        "proposal_ids": [],
    }


def _append_graph_context_value(context: dict[str, list[str]], key: str, value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    values = context.setdefault(key, [])
    if text not in values:
        values.append(text)


def _graph_context_values(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _normalize_context_phase_id(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(?i)P(\d+)", text)
    if match:
        return f"P{int(match.group(1))}"
    return text


def _normalize_context_task_id(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(?i)P(\d+)[._-]?T(\d{1,4})", text)
    if match:
        return f"P{int(match.group(1))}.T{int(match.group(2)):03d}"
    return text


def _normalize_context_objective_id(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(?i)P(\d+)[._-]O(\d{1,4})", text)
    if match:
        return f"P{int(match.group(1))}.O{int(match.group(2))}"
    return text


def _add_graph_context_phase(context: dict[str, list[str]], value: Any) -> None:
    phase_id = _normalize_context_phase_id(value)
    _append_graph_context_value(context, "phase_ids", phase_id)


def _add_graph_context_task(context: dict[str, list[str]], value: Any) -> None:
    task_id = _normalize_context_task_id(value)
    _append_graph_context_value(context, "target_task_ids", task_id)
    match = re.fullmatch(r"(?i)P(\d+)[._-]?T\d{1,4}", task_id)
    if match:
        _add_graph_context_phase(context, f"P{match.group(1)}")


def _add_graph_context_objective(context: dict[str, list[str]], value: Any) -> None:
    objective_id = _normalize_context_objective_id(value)
    _append_graph_context_value(context, "target_objective_ids", objective_id)
    match = re.fullmatch(r"(?i)P(\d+)[._-]O\d{1,4}", objective_id)
    if match:
        _add_graph_context_phase(context, f"P{match.group(1)}")


def _add_graph_context_from_text(context: dict[str, list[str]], value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    for match in GRAPH_CONTEXT_TASK_RE.finditer(text):
        _add_graph_context_task(context, f"P{match.group('phase')}.T{int(match.group('task')):03d}")
    for match in GRAPH_CONTEXT_OBJECTIVE_RE.finditer(text):
        _add_graph_context_objective(context, f"P{match.group('phase')}.O{int(match.group('objective'))}")
    for match in GRAPH_CONTEXT_PHASE_RE.finditer(text):
        _add_graph_context_phase(context, f"P{match.group('phase')}")


def _add_graph_context_from_nested_text(context: dict[str, list[str]], value: Any, *, depth: int = 0) -> None:
    if depth > 5 or value in (None, ""):
        return
    if isinstance(value, str):
        _add_graph_context_from_text(context, value)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _add_graph_context_from_nested_text(context, item, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in list(value)[:200]:
            _add_graph_context_from_nested_text(context, item, depth=depth + 1)


def _add_event_run_context(context: dict[str, list[str]], event: Mapping[str, Any]) -> None:
    payload = _event_payload(event)
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    for value in (
        event.get("phase_id"),
        payload.get("phase_id"),
        payload.get("objective_phase_id"),
        _mapping_value(subject, "phase_id"),
        _mapping_value(subject, "objective_phase_id"),
    ):
        _add_graph_context_phase(context, value)
    for value in (
        event.get("task_id"),
        payload.get("task_id"),
        _mapping_value(subject, "task_id"),
    ):
        _add_graph_context_task(context, value)
    for key in ("target_task_ids", "added_task_ids", "related_task_ids"):
        for value in _graph_context_values(payload.get(key)):
            _add_graph_context_task(context, value)
    for value in _graph_context_values(payload.get("target_objective_ids")):
        _add_graph_context_objective(context, value)
    for value in (payload.get("objective_id"), payload.get("target_objective_id")):
        _add_graph_context_objective(context, value)
    proposal_id = payload.get("proposal_id")
    _append_graph_context_value(context, "proposal_ids", proposal_id)
    for key in (
        "proposal_id",
        "loop_signature",
        "objective_gap_signature",
        "failure_signature",
        "action_scope_key",
        "scope_key",
        "message",
        "summary",
        "reason",
    ):
        _add_graph_context_from_text(context, payload.get(key))
    for value in _graph_context_values(payload.get("target_failure_ids")):
        _add_graph_context_from_text(context, value)
    _add_graph_context_from_nested_text(context, payload.get("agent_status"))
    for key in ("candidate", "selected_candidate", "proposal", "expansion"):
        _add_graph_context_from_proposal_like(context, payload.get(key))


def _add_expansion_proposal_run_context(context: dict[str, list[str]], proposal: Mapping[str, Any]) -> None:
    for key in ("target_task_ids", "added_task_ids", "related_task_ids"):
        for value in _graph_context_values(proposal.get(key)):
            _add_graph_context_task(context, value)
    for key in ("target_objective_ids", "added_objective_ids"):
        for value in _graph_context_values(proposal.get(key)):
            _add_graph_context_objective(context, value)
    for key in ("target_phase_ids", "added_phase_ids", "related_phase_ids"):
        for value in _graph_context_values(proposal.get(key)):
            _add_graph_context_phase(context, value)
    for value in (proposal.get("phase_id"), proposal.get("new_phase_id"), proposal.get("objective_phase_id")):
        _add_graph_context_phase(context, value)
    proposal_id = proposal.get("proposal_id")
    _append_graph_context_value(context, "proposal_ids", proposal_id)
    for key in (
        "proposal_id",
        "loop_signature",
        "objective_gap_signature",
        "failure_signature",
        "action_scope_key",
        "scope_key",
        "trigger",
        "summary",
        "reason",
    ):
        _add_graph_context_from_text(context, proposal.get(key))
    for value in _graph_context_values(proposal.get("target_failure_ids")):
        _add_graph_context_from_text(context, value)


def _add_graph_context_from_proposal_like(context: dict[str, list[str]], value: Any, *, depth: int = 0) -> None:
    if depth > 3 or value in (None, ""):
        return
    if isinstance(value, Mapping):
        _add_expansion_proposal_run_context(context, value)
        for key in ("proposal", "expansion", "candidate", "selected_candidate"):
            nested = value.get(key)
            if nested not in (None, ""):
                _add_graph_context_from_proposal_like(context, nested, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in list(value)[:50]:
            _add_graph_context_from_proposal_like(context, item, depth=depth + 1)


def _run_context_index(
    events: Sequence[Mapping[str, Any]],
    expansion_registry: Mapping[str, Any],
) -> dict[str, dict[str, list[str]]]:
    contexts: dict[str, dict[str, list[str]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        payload = _event_payload(event)
        subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
        run_id = str(event.get("run_id") or payload.get("run_id") or _mapping_value(subject, "run_id") or "").strip()
        if not run_id:
            continue
        _add_event_run_context(contexts.setdefault(run_id, _new_run_graph_context()), event)

    proposals = expansion_registry.get("proposals")
    if isinstance(proposals, Sequence) and not isinstance(proposals, (str, bytes, bytearray)):
        for raw_proposal in proposals:
            proposal = _mapping_or_empty(raw_proposal)
            run_id = str(proposal.get("run_id") or "").strip()
            if not run_id:
                continue
            _add_expansion_proposal_run_context(contexts.setdefault(run_id, _new_run_graph_context()), proposal)
    return contexts


def _merge_context_values(existing: Any, additions: Sequence[str]) -> list[str]:
    values: list[str] = []
    for value in _graph_context_values(existing):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    for value in additions:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _node_phase_ids(node: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for value in _graph_context_values(node.get("phase_ids")):
        phase_id = _normalize_context_phase_id(value)
        if phase_id and phase_id not in values:
            values.append(phase_id)
    for value in (node.get("phase_id"), node.get("objective_phase_id")):
        phase_id = _normalize_context_phase_id(value)
        if phase_id and phase_id not in values:
            values.append(phase_id)
    return values


def _merge_run_context_into_nodes(
    nodes_by_id: Mapping[str, dict[str, Any]],
    run_context: Mapping[str, Mapping[str, Sequence[str]]],
) -> None:
    for node in nodes_by_id.values():
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        context = run_context.get(run_id)
        if not context:
            continue
        phase_ids = _merge_context_values(node.get("phase_ids"), list(context.get("phase_ids") or []))
        if phase_ids:
            node["phase_ids"] = phase_ids
            if node.get("phase_id") in (None, "", []):
                node["phase_id"] = phase_ids[0]
            if node.get("phase") in (None, "", []):
                node["phase"] = phase_ids[0]
        for key in ("target_task_ids", "target_objective_ids", "proposal_ids"):
            values = _merge_context_values(node.get(key), list(context.get(key) or []))
            if values:
                node[key] = values


def _workflow_graph_model(
    *,
    common: Mapping[str, Any],
    plan_phases: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    event_limit: int,
    node_summaries: Sequence[Mapping[str, Any]],
    agent_statuses: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
    task_summaries: Sequence[Mapping[str, Any]],
    objective_model: Mapping[str, Any],
    expansion_registry: Mapping[str, Any],
    event_total_count: int | None,
    context_events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    graph_events = _recent_dashboard_events(events, limit=event_limit)
    run_context = _run_context_index(context_events if context_events is not None else events, expansion_registry)
    total_events = event_total_count if event_total_count is not None else len(events)
    nodes_by_id: dict[str, dict[str, Any]] = {}
    phases_by_id, task_to_phase = _add_plan_graph_nodes(
        nodes_by_id,
        plan_phases=plan_phases,
        task_summaries=task_summaries,
    )
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
    _merge_run_context_into_nodes(nodes_by_id, run_context)
    _annotate_pipeline_visibility(nodes_by_id)
    _propagate_run_graph_metadata(nodes_by_id)

    edges: list[dict[str, Any]] = []
    for task_id, phase_id in sorted(task_to_phase.items()):
        _append_graph_edge(
            edges,
            "phase_contains_task",
            _phase_node_id(phase_id),
            _task_node_id(task_id),
            nodes_by_id=nodes_by_id,
            label="contains",
            layer="plan",
            priority=100,
            evidence=[{"kind": "plan_index", "id": task_id}],
        )
    for phase_id, phase in sorted(phases_by_id.items()):
        phase_node = _phase_node_id(phase_id)
        for objective in _sequence(phase.get("objectives")):
            objective_id = str(_mapping_or_empty(objective).get("objective_id") or "")
            if not objective_id:
                continue
            _append_graph_edge(
                edges,
                "phase_has_objective",
                phase_node,
                _objective_node_id(objective_id),
                nodes_by_id=nodes_by_id,
                label="objective",
                layer="objective",
                priority=80,
                evidence=[{"kind": "objective", "id": objective_id}],
            )
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
        _append_graph_edge(
            edges,
            "validated_by",
            source,
            target,
            nodes_by_id=nodes_by_id,
            label="validated by",
            layer="validation",
            status=validation.get("status"),
            created_at=validation.get("started_at") or validation.get("validated_at"),
            ended_at=validation.get("validated_at"),
            priority=70,
            evidence=[{"kind": "validation", "id": run_id}],
        )
        task_id = str(validation.get("primary_task_id") or validation.get("task_id") or "")
        if task_id:
            _append_graph_edge(
                edges,
                "validation_checks_task",
                target,
                _task_node_id(task_id),
                nodes_by_id=nodes_by_id,
                label="checks",
                layer="validation",
                status=validation.get("status"),
                priority=55,
                evidence=[{"kind": "validation", "id": run_id}],
            )
    previous_event_node: str | None = None
    for event in graph_events:
        node_id = _event_node_id(event)
        if previous_event_node is not None:
            _append_graph_edge(
                edges,
                "event_sequence",
                previous_event_node,
                node_id,
                nodes_by_id=nodes_by_id,
                label="next event",
                layer="event",
                hidden_by_default=True,
                priority=5,
                discriminator=str(_event_sequence(event) or _event_id(event) or node_id),
            )
        previous_event_node = node_id

    for node_id, node in sorted(nodes_by_id.items()):
        node_type = str(node.get("type") or "").strip().lower()
        task_id = str(node.get("task_id") or "")
        run_id = str(node.get("run_id") or "")
        if task_id and run_id and node_type not in {"event", "validation"}:
            _append_graph_edge(
                edges,
                "task_has_run",
                _task_node_id(task_id),
                node_id,
                nodes_by_id=nodes_by_id,
                label="run",
                layer="runtime",
                status=node.get("status"),
                created_at=node.get("started_at"),
                ended_at=node.get("ended_at"),
                priority=75,
                evidence=[{"kind": "run", "id": run_id}],
            )
        if run_id and node_type not in {"event", "validation"}:
            for target_task_id in sorted({str(value) for value in _graph_context_values(node.get("target_task_ids")) if str(value)}):
                if target_task_id == task_id:
                    continue
                _append_graph_edge(
                    edges,
                    "task_has_run",
                    _task_node_id(target_task_id),
                    node_id,
                    nodes_by_id=nodes_by_id,
                    label="targets",
                    layer="runtime",
                    status=node.get("status"),
                    created_at=node.get("started_at"),
                    ended_at=node.get("ended_at"),
                    priority=58,
                    evidence=[{"kind": "run_context", "id": run_id}],
                    discriminator=target_task_id,
                )
            if not task_id:
                for phase_id in sorted({phase_id for phase_id in _node_phase_ids(node) if phase_id}):
                    _append_graph_edge(
                        edges,
                        "phase_has_run",
                        _phase_node_id(phase_id),
                        node_id,
                        nodes_by_id=nodes_by_id,
                        label="run",
                        layer="runtime",
                        status=node.get("status"),
                        created_at=node.get("started_at"),
                        ended_at=node.get("ended_at"),
                        priority=57,
                        evidence=[{"kind": "run_context", "id": run_id}],
                        discriminator=phase_id,
                    )
        if node_type == "event":
            if run_id:
                _append_graph_edge(
                    edges,
                    "event_about_node",
                    node_id,
                    _preferred_run_node_id(nodes_by_id, run_id),
                    nodes_by_id=nodes_by_id,
                    label="about",
                    layer="event",
                    hidden_by_default=True,
                    priority=15,
                    evidence=[{"kind": "event", "id": str(node.get("event_id") or node_id)}],
                )
            elif task_id:
                _append_graph_edge(
                    edges,
                    "event_about_node",
                    node_id,
                    _task_node_id(task_id),
                    nodes_by_id=nodes_by_id,
                    label="about",
                    layer="event",
                    hidden_by_default=True,
                    priority=15,
                    evidence=[{"kind": "event", "id": str(node.get("event_id") or node_id)}],
                )
        target_objective_ids = node.get("target_objective_ids")
        if isinstance(target_objective_ids, Sequence) and not isinstance(target_objective_ids, (str, bytes, bytearray)):
            for objective_id in sorted({str(value) for value in target_objective_ids if str(value)}):
                target = _objective_node_id(objective_id)
                if node_type == "event":
                    edge_type = "event_about_node"
                    source = node_id
                    label = "about"
                    priority = 20
                elif run_id:
                    edge_type = "run_supports_objective"
                    source = _preferred_run_node_id(nodes_by_id, run_id)
                    label = "supports"
                    priority = 60
                else:
                    continue
                _append_graph_edge(
                    edges,
                    edge_type,
                    source,
                    target,
                    nodes_by_id=nodes_by_id,
                    label=label,
                    layer="objective",
                    status=node.get("status"),
                    hidden_by_default=node_type == "event",
                    priority=priority,
                    evidence=[{"kind": node_type or "node", "id": str(node.get("event_id") or run_id or node_id)}],
                    discriminator=objective_id,
                )

    task_lookup = {
        str(task.get("task_id") or ""): task
        for task in task_summaries
        if str(task.get("task_id") or "")
    }
    nodes = [_enrich_node_with_task(node, task_lookup) for node in nodes_by_id.values()]
    graph = _aggregate_self_expansion_graph(
        nodes,
        _dedupe_edges(edges),
        detail_limit=SELF_EXPANSION_GRAPH_DETAIL_LIMIT,
    )
    verifier_graph = _aggregate_objective_verifier_graph(
        graph["nodes"],
        graph["edges"],
        detail_limit=OBJECTIVE_VERIFIER_GRAPH_DETAIL_LIMIT,
    )
    graph_edges = _annotate_parallel_edges(verifier_graph["edges"])
    return {
        **dict(common),
        "event_window": {
            "total_events": total_events,
            "visible_event_nodes": len(graph_events),
            "limit": event_limit,
            "truncated": total_events > len(graph_events),
        },
        "self_expansion_aggregation": graph["aggregation"],
        "objective_verifier_aggregation": verifier_graph["aggregation"],
        "nodes": verifier_graph["nodes"],
        "edges": graph_edges,
        "network": _network_graph_metadata(verifier_graph["nodes"], graph_edges, event_window_truncated=total_events > len(graph_events)),
    }


def _preferred_run_node_id(nodes_by_id: Mapping[str, Mapping[str, Any]], run_id: str) -> str:
    for node_id, node in nodes_by_id.items():
        if str(node.get("run_id") or "") != run_id:
            continue
        node_type = str(node.get("type") or "").strip().lower()
        if node_type not in {"event", "validation"}:
            return str(node_id)
    return _run_node_id(run_id)


def _annotate_parallel_edges(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        item = dict(edge)
        key = (str(item.get("source") or ""), str(item.get("target") or ""))
        groups.setdefault(key, []).append(item)
    annotated: list[dict[str, Any]] = []
    for group_edges in groups.values():
        ordered = sorted(group_edges, key=lambda item: (str(item.get("type") or ""), str(item.get("edge_id") or "")))
        count = len(ordered)
        for index, edge in enumerate(ordered):
            edge["parallel_index"] = index
            edge["parallel_count"] = count
            annotated.append(edge)
    return sorted(annotated, key=lambda item: str(item.get("edge_id") or ""))


def _network_graph_metadata(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    event_window_truncated: bool,
) -> dict[str, Any]:
    hidden_edge_types = sorted({str(edge.get("type") or "") for edge in edges if edge.get("hidden_by_default") is True and edge.get("type")})
    return {
        "schema_version": "1",
        "default_layout": "temporal_force",
        "base_layout": "cose",
        "available_layouts": ["temporal_force", "cose"],
        "temporal_axis": "x",
        "temporal_scale": "rank",
        "temporal_pending": "end_alpha",
        "default_visible_layers": ["plan", "runtime", "validation", "objective"],
        "default_hidden_edge_types": hidden_edge_types or ["event_sequence"],
        "node_count": len(nodes),
        "edge_count": len(edges),
        "truncated": bool(event_window_truncated),
    }


def _aggregate_self_expansion_graph(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    detail_limit: int,
) -> dict[str, Any]:
    node_list = [dict(node) for node in nodes]
    expansion_nodes = [node for node in node_list if _is_self_expansion_graph_node(node)]
    if detail_limit <= 0:
        keep_ids = {str(node.get("node_id") or "") for node in expansion_nodes if _status_like_active(node.get("status"))}
    else:
        active_ids = {str(node.get("node_id") or "") for node in expansion_nodes if _status_like_active(node.get("status"))}
        recent = sorted(expansion_nodes, key=_self_expansion_detail_sort_key, reverse=True)[:detail_limit]
        keep_ids = active_ids | {str(node.get("node_id") or "") for node in recent}
    aggregate_candidates = [
        node
        for node in expansion_nodes
        if str(node.get("node_id") or "") and str(node.get("node_id") or "") not in keep_ids
    ]
    if not aggregate_candidates:
        sorted_nodes = sorted(node_list, key=_graph_node_sort_key)
        return {
            "nodes": sorted_nodes,
            "edges": _dedupe_edges(edges),
            "aggregation": {
                "enabled": False,
                "visible_node_count": len(sorted_nodes),
                "aggregated_node_count": 0,
                "detail_limit": detail_limit,
                "retention_policy": _self_expansion_retention_policy(detail_limit),
                "groups": [],
            },
        }

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for node in aggregate_candidates:
        groups.setdefault(_self_expansion_aggregation_key(node), []).append(node)
    node_to_group: dict[str, str] = {}
    aggregate_nodes: list[dict[str, Any]] = []
    group_records: list[dict[str, Any]] = []
    for key, group_nodes in sorted(groups.items()):
        group_id = f"aggregate_self_expansion_{_safe_id(key)}"
        for node in group_nodes:
            node_to_group[str(node.get("node_id") or "")] = group_id
        fields = _self_expansion_group_fields(group_nodes)
        aggregate_node = _self_expansion_aggregate_node(group_id, key, group_nodes, fields=fields)
        aggregate_nodes.append(aggregate_node)
        group_records.append(
            {
                "group_id": group_id,
                "key": key,
                **fields,
                "aggregated_node_count": len(group_nodes),
                "status_counts": _count_values(str(node.get("status") or "unknown") for node in group_nodes),
                "event_type_counts": _count_values(str(node.get("event_type") or node.get("status") or "unknown") for node in group_nodes),
                "sample_run_ids": _sample_values(
                    (str(node.get("run_id") or "") for node in group_nodes if str(node.get("run_id") or "")),
                    limit=SELF_EXPANSION_GRAPH_SAMPLE_LIMIT,
                ),
            }
        )
    visible_nodes = [
        node
        for node in node_list
        if str(node.get("node_id") or "") not in node_to_group
    ]
    visible_nodes.extend(aggregate_nodes)
    visible_ids = {str(node.get("node_id") or "") for node in visible_nodes}
    rewired_edges: list[dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        source = node_to_group.get(source, source)
        target = node_to_group.get(target, target)
        if not source or not target or source == target or source not in visible_ids or target not in visible_ids:
            continue
        rewired = dict(edge)
        rewired["source"] = source
        rewired["target"] = target
        rewired["edge_id"] = f"edge_{source}_to_{target}_{_stable_suffix(rewired)}"
        if source.startswith("aggregate_self_expansion_") or target.startswith("aggregate_self_expansion_"):
            rewired["aggregated"] = True
        rewired_edges.append(rewired)
    sorted_nodes = sorted(visible_nodes, key=_graph_node_sort_key)
    return {
        "nodes": sorted_nodes,
        "edges": _dedupe_edges(rewired_edges),
        "aggregation": {
            "enabled": True,
            "visible_node_count": len(sorted_nodes),
            "aggregated_node_count": len(aggregate_candidates),
            "detail_limit": detail_limit,
            "retention_policy": _self_expansion_retention_policy(detail_limit),
            "groups": group_records,
        },
    }


def _aggregate_objective_verifier_graph(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    detail_limit: int,
) -> dict[str, Any]:
    node_list = [dict(node) for node in nodes]
    verifier_nodes = [node for node in node_list if _is_objective_verifier_graph_node(node)]
    if detail_limit <= 0:
        keep_ids = {str(node.get("node_id") or "") for node in verifier_nodes if _status_like_active(node.get("status"))}
    else:
        active_ids = {str(node.get("node_id") or "") for node in verifier_nodes if _status_like_active(node.get("status"))}
        recent = sorted(verifier_nodes, key=_self_expansion_detail_sort_key, reverse=True)[:detail_limit]
        keep_ids = active_ids | {str(node.get("node_id") or "") for node in recent}
    aggregate_candidates = [
        node
        for node in verifier_nodes
        if str(node.get("node_id") or "") and str(node.get("node_id") or "") not in keep_ids
    ]
    if not aggregate_candidates:
        sorted_nodes = sorted(node_list, key=_graph_node_sort_key)
        return {
            "nodes": sorted_nodes,
            "edges": _dedupe_edges(edges),
            "aggregation": _objective_verifier_empty_aggregation(detail_limit, visible_node_count=len(sorted_nodes)),
        }

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for node in aggregate_candidates:
        groups.setdefault(_objective_verifier_aggregation_key(node), []).append(node)
    node_to_group: dict[str, str] = {}
    aggregate_nodes: list[dict[str, Any]] = []
    group_records: list[dict[str, Any]] = []
    for key, group_nodes in sorted(groups.items()):
        group_id = f"aggregate_objective_verifier_{_safe_id(key)}"
        for node in group_nodes:
            node_to_group[str(node.get("node_id") or "")] = group_id
        fields = _objective_verifier_group_fields(group_nodes)
        aggregate_node = _objective_verifier_aggregate_node(group_id, key, group_nodes, fields=fields)
        aggregate_nodes.append(aggregate_node)
        group_records.append(
            {
                "group_id": group_id,
                "key": key,
                **fields,
                "aggregated_node_count": len(group_nodes),
                "status_counts": _count_values(str(node.get("status") or "unknown") for node in group_nodes),
                "sample_run_ids": _sample_values(
                    (str(node.get("run_id") or "") for node in group_nodes if str(node.get("run_id") or "")),
                    limit=OBJECTIVE_VERIFIER_GRAPH_SAMPLE_LIMIT,
                ),
            }
        )
    visible_nodes = [
        node
        for node in node_list
        if str(node.get("node_id") or "") not in node_to_group
    ]
    visible_nodes.extend(aggregate_nodes)
    visible_ids = {str(node.get("node_id") or "") for node in visible_nodes}
    rewired_edges = _rewire_edges_for_aggregation(
        edges,
        node_to_group=node_to_group,
        visible_ids=visible_ids,
        aggregate_prefix="aggregate_objective_verifier_",
    )
    sorted_nodes = sorted(visible_nodes, key=_graph_node_sort_key)
    return {
        "nodes": sorted_nodes,
        "edges": rewired_edges,
        "aggregation": {
            "enabled": True,
            "visible_node_count": len(sorted_nodes),
            "aggregated_node_count": len(aggregate_candidates),
            "detail_limit": detail_limit,
            "retention_policy": _objective_verifier_retention_policy(detail_limit),
            "groups": group_records,
        },
    }


def _rewire_edges_for_aggregation(
    edges: Sequence[Mapping[str, Any]],
    *,
    node_to_group: Mapping[str, str],
    visible_ids: set[str],
    aggregate_prefix: str,
) -> list[dict[str, Any]]:
    rewired_by_key: dict[str, dict[str, Any]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        mapped_source = node_to_group.get(source, source)
        mapped_target = node_to_group.get(target, target)
        if not mapped_source or not mapped_target or mapped_source == mapped_target:
            continue
        if mapped_source not in visible_ids or mapped_target not in visible_ids:
            continue
        rewired = dict(edge)
        rewired["source"] = mapped_source
        rewired["target"] = mapped_target
        changed = mapped_source != source or mapped_target != target
        if changed or mapped_source.startswith(aggregate_prefix) or mapped_target.startswith(aggregate_prefix):
            rewired["aggregated"] = True
        signature = {
            "type": rewired.get("type"),
            "source": mapped_source,
            "target": mapped_target,
            "label": rewired.get("label"),
            "layer": rewired.get("layer"),
            "status": rewired.get("status"),
            "hidden_by_default": rewired.get("hidden_by_default") is True,
        }
        edge_id = _graph_edge_id(str(rewired.get("type") or "edge"), mapped_source, mapped_target, _stable_suffix(signature))
        existing = rewired_by_key.get(edge_id)
        if existing:
            existing["aggregated_edge_count"] = int(existing.get("aggregated_edge_count") or 1) + 1
            if rewired.get("aggregated"):
                existing["aggregated"] = True
            continue
        rewired["edge_id"] = edge_id
        if rewired.get("aggregated"):
            rewired["aggregated_edge_count"] = 1
        rewired_by_key[edge_id] = rewired
    return sorted(rewired_by_key.values(), key=lambda item: str(item.get("edge_id") or ""))


def _is_objective_verifier_graph_node(node: Mapping[str, Any]) -> bool:
    node_type = str(node.get("type") or "").strip().lower()
    role = str(node.get("agent_role") or "").strip().lower()
    title = str(node.get("title") or "").strip().lower()
    return "objective_verifier" in {node_type, role} or "objective verifier" in title or "objective_verifier" in title


def _objective_verifier_empty_aggregation(detail_limit: int, *, visible_node_count: int) -> dict[str, Any]:
    return {
        "enabled": False,
        "visible_node_count": visible_node_count,
        "aggregated_node_count": 0,
        "detail_limit": detail_limit,
        "retention_policy": _objective_verifier_retention_policy(detail_limit),
        "groups": [],
    }


def _objective_verifier_retention_policy(detail_limit: int) -> dict[str, Any]:
    return {
        "mode": "recent_active_detail_with_historical_buckets",
        "recent_detail_limit": detail_limit,
        "active_nodes_always_visible": True,
        "bucket_fields": ["time_bucket", "phase_key", "target_key", "terminal_status"],
        "sample_limit": OBJECTIVE_VERIFIER_GRAPH_SAMPLE_LIMIT,
    }


def _objective_verifier_aggregation_key(node: Mapping[str, Any]) -> str:
    fields = _objective_verifier_group_fields([node])
    return "|".join(
        (
            "bucket=" + fields["time_bucket"],
            "phase=" + fields["phase_key"],
            "target=" + fields["target_key"],
            "status=" + fields["terminal_status"],
        )
    )


def _objective_verifier_group_fields(nodes: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    first = nodes[0] if nodes else {}
    return {
        "time_bucket": _self_expansion_time_bucket(first),
        "phase_key": _objective_verifier_phase_key(first),
        "target_key": _self_expansion_target_key(first),
        "terminal_status": _self_expansion_terminal_status(first),
    }


def _objective_verifier_phase_key(node: Mapping[str, Any]) -> str:
    values = _node_phase_ids(node)
    return "phase:" + ",".join(sorted(values)[:3]) if values else "phase:workflow"


def _objective_verifier_aggregate_node(
    group_id: str,
    key: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    fields: Mapping[str, str],
) -> dict[str, Any]:
    timestamps = [_latest_node_timestamp(node) for node in nodes if _latest_node_timestamp(node)]
    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    run_ids = [str(node.get("run_id") or "") for node in nodes if str(node.get("run_id") or "")]
    phase_ids = sorted({phase_id for node in nodes for phase_id in _node_phase_ids(node) if phase_id})
    target_objective_ids = sorted(
        {
            str(value)
            for node in nodes
            for value in _graph_context_values(node.get("target_objective_ids"))
            if str(value)
        }
    )
    status_counts = _count_values(str(node.get("status") or "unknown") for node in nodes)
    return _node_with_timing(
        {
            "node_id": group_id,
            "type": "objective_verifier_group",
            "layer": "runtime",
            "kind": "objective_verifier_group",
            "status": key,
            "title": f"Historical Objective Verifier: {fields.get('phase_key')} · {fields.get('target_key')}",
            "display_label": f"Verifier x{len(nodes)}",
            "started_at": started_at,
            "ended_at": ended_at,
            "aggregated": True,
            "aggregated_node_count": len(nodes),
            "retention_policy": "recent_active_detail_with_historical_buckets",
            "time_bucket": fields.get("time_bucket"),
            "phase_key": fields.get("phase_key"),
            "target_key": fields.get("target_key"),
            "terminal_status": fields.get("terminal_status"),
            "phase_ids": phase_ids,
            "phase_id": phase_ids[0] if phase_ids else None,
            "target_objective_ids": target_objective_ids,
            "sample_run_ids": _sample_values(run_ids, limit=OBJECTIVE_VERIFIER_GRAPH_SAMPLE_LIMIT),
            "status_counts": status_counts,
            "input_refs": [],
            "output_refs": [],
            "summary": {
                "one_line": f"{len(nodes)} historical objective verifier run(s) are aggregated for {fields.get('phase_key')}.",
                "highlights": [f"{len(set(run_ids))} run(s) represented."],
                "risks": [],
            },
        }
    )


def _is_self_expansion_graph_node(node: Mapping[str, Any]) -> bool:
    node_type = str(node.get("type") or "").strip().lower()
    status = str(node.get("status") or "").strip().lower()
    event_type = str(node.get("event_type") or "").strip().lower()
    actor = str(node.get("actor_label") or "").strip().lower()
    role = str(node.get("agent_role") or "").strip().lower()
    title = str(node.get("title") or "").strip().lower()
    return any(
        "expansion_planner" in value or "self-expansion" in value or "self_expansion" in value
        for value in (node_type, status, event_type, actor, role, title)
    )


def _self_expansion_detail_sort_key(node: Mapping[str, Any]) -> tuple[str, int, str]:
    timestamp = _latest_node_timestamp(node)
    sequence = _int_or_none(node.get("event_sequence")) or 0
    return (timestamp, sequence, str(node.get("node_id") or ""))


def _self_expansion_aggregation_key(node: Mapping[str, Any]) -> str:
    fields = _self_expansion_group_fields([node])
    return "|".join(
        (
            "bucket=" + fields["time_bucket"],
            "event=" + fields["event_type"],
            "type=" + fields["expansion_type"],
            "status=" + fields["terminal_status"],
            "target=" + fields["target_key"],
        )
    )


def _self_expansion_group_fields(nodes: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    first = nodes[0] if nodes else {}
    return {
        "time_bucket": _self_expansion_time_bucket(first),
        "event_type": _self_expansion_field(first, "event_type", "status", "type", default="self_expansion"),
        "expansion_type": _self_expansion_field(first, "expansion_type", default="unknown"),
        "terminal_status": _self_expansion_terminal_status(first),
        "target_key": _self_expansion_target_key(first),
    }


def _self_expansion_time_bucket(node: Mapping[str, Any]) -> str:
    timestamp = _latest_node_timestamp(node)
    if not timestamp or len(timestamp) < 10:
        return "unknown_date"
    return timestamp[:10]


def _self_expansion_field(node: Mapping[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return default


def _self_expansion_terminal_status(node: Mapping[str, Any]) -> str:
    value = _self_expansion_field(node, "status", default="unknown")
    if _status_like_active(value):
        return "active"
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"pass", "ok", "completed", "complete", "done", "applied"}:
        return "completed"
    if normalized in {"fail", "failed", "error", "rejected", "blocked"}:
        return "failed"
    return value


def _self_expansion_target_key(node: Mapping[str, Any]) -> str:
    for key in ("target_failure_ids", "target_task_ids", "target_objective_ids"):
        raw_values = node.get(key)
        values = (
            [str(item).strip() for item in raw_values if str(item).strip()]
            if isinstance(raw_values, Sequence) and not isinstance(raw_values, (str, bytes, bytearray))
            else []
        )
        if values:
            return key.removesuffix("_ids") + ":" + ",".join(sorted(values)[:3])
    for key in ("proposal_id",):
        value = str(node.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return "workflow"


def _self_expansion_retention_policy(detail_limit: int) -> dict[str, Any]:
    return {
        "mode": "recent_active_detail_with_historical_buckets",
        "recent_detail_limit": detail_limit,
        "active_nodes_always_visible": True,
        "bucket_fields": ["time_bucket", "event_type", "expansion_type", "terminal_status", "target_key"],
        "sample_limit": SELF_EXPANSION_GRAPH_SAMPLE_LIMIT,
    }


def _self_expansion_aggregate_node(
    group_id: str,
    key: str,
    nodes: Sequence[Mapping[str, Any]],
    *,
    fields: Mapping[str, str],
) -> dict[str, Any]:
    timestamps = [_latest_node_timestamp(node) for node in nodes if _latest_node_timestamp(node)]
    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    run_ids = [str(node.get("run_id") or "") for node in nodes if str(node.get("run_id") or "")]
    task_ids = [str(node.get("task_id") or "") for node in nodes if str(node.get("task_id") or "")]
    status_counts = _count_values(str(node.get("status") or "unknown") for node in nodes)
    event_type_counts = _count_values(str(node.get("event_type") or node.get("status") or "unknown") for node in nodes)
    return _node_with_timing(
        {
            "node_id": group_id,
            "type": "self_expansion_group",
            "status": key,
            "title": f"Historical Self-Expansion: {key}",
            "started_at": started_at,
            "ended_at": ended_at,
            "aggregated": True,
            "aggregated_node_count": len(nodes),
            "aggregated_run_count": len(set(run_ids)),
            "retention_policy": "recent_active_detail_with_historical_buckets",
            "time_bucket": fields.get("time_bucket"),
            "expansion_type": fields.get("expansion_type"),
            "terminal_status": fields.get("terminal_status"),
            "target_key": fields.get("target_key"),
            "sample_run_ids": _sample_values(run_ids, limit=SELF_EXPANSION_GRAPH_SAMPLE_LIMIT),
            "sample_task_ids": _sample_values(task_ids, limit=SELF_EXPANSION_GRAPH_SAMPLE_LIMIT),
            "status_counts": status_counts,
            "event_type_counts": event_type_counts,
            "input_refs": [],
            "output_refs": [],
            "summary": {
                "one_line": f"{len(nodes)} historical self-expansion node(s) are aggregated for {key}.",
                "highlights": [f"{len(set(run_ids))} run(s) represented."],
                "risks": [],
            },
        }
    )


def _sample_values(values: Sequence[str] | Any, *, limit: int) -> list[str]:
    unique = sorted({str(value) for value in values if str(value)})
    return unique[: max(0, int(limit))]


def _count_values(values: Sequence[str] | Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _propagate_run_graph_metadata(nodes_by_id: Mapping[str, dict[str, Any]]) -> None:
    metadata_by_run: dict[str, dict[str, Any]] = {}
    metadata_keys = ("phase_id", "phase", "objective_phase_id", "objective_scope")
    list_metadata_keys = ("phase_ids", "target_objective_ids", "target_task_ids", "proposal_ids")
    for node in nodes_by_id.values():
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        metadata = metadata_by_run.setdefault(run_id, {})
        for key in metadata_keys:
            value = node.get(key)
            if value not in (None, "", []):
                metadata.setdefault(key, value)
        for key in list_metadata_keys:
            values = _graph_context_values(node.get(key))
            if values:
                metadata[key] = _merge_context_values(metadata.get(key), [str(value) for value in values])
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


def _run_detail_build_run_ids(
    *,
    node_summaries: Sequence[Mapping[str, Any]],
    agent_statuses: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
    limit: int,
) -> set[str]:
    active_run_ids = {str(lease.get("run_id") or "") for lease in active_leases if str(lease.get("run_id") or "")}
    scored: dict[str, tuple[str, int]] = {}
    order = 0

    def add(record: Mapping[str, Any], *timestamp_keys: str) -> None:
        nonlocal order
        run_id = str(record.get("run_id") or "")
        if not run_id:
            return
        order += 1
        timestamp = ""
        for key in timestamp_keys:
            value = str(record.get(key) or "")
            if value > timestamp:
                timestamp = value
        current = scored.get(run_id)
        candidate = (timestamp, order)
        if current is None or candidate >= current:
            scored[run_id] = candidate

    for record in agent_statuses:
        add(record, "ended_at", "updated_at", "started_at", "created_at")
    for record in node_summaries:
        add(record, "ended_at", "updated_at", "started_at", "created_at", "ts", "timestamp")
    for record in validation_records:
        add(record, "validated_at", "updated_at", "created_at", "ts", "timestamp")
    for record in active_leases:
        add(record, "heartbeat_at", "started_at", "prepared_at", "created_at")

    if limit <= 0:
        return active_run_ids
    if len(scored) <= limit:
        return set(scored) | active_run_ids
    recent = {
        run_id
        for run_id, _score in sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]
    }
    return recent | active_run_ids


def _build_run_summaries(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    events: Sequence[Mapping[str, Any]],
    node_summaries: Sequence[Mapping[str, Any]],
    agent_statuses: Sequence[Mapping[str, Any]],
    validation_records: Sequence[Mapping[str, Any]],
    active_leases: Sequence[Mapping[str, Any]],
    detail_run_ids: set[str],
    previous_details: Mapping[str, Mapping[str, Any]] | None = None,
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
        if run_id not in detail_run_ids:
            record["detail_status"] = "not_built"
            continue
        task_id = _first_string(record.get("task_id"), _mapping_value(statuses_by_run.get(run_id), "task_id"))
        detail_source = _run_detail_source_metadata(
            paths,
            run_id=run_id,
            task_id=task_id,
            agent_status=statuses_by_run.get(run_id),
            node_summary=summaries_by_run.get(run_id),
            validation=validations_by_run.get(run_id),
            active_lease=active_leases_by_run.get(run_id),
        )
        reused = _reusable_previous_detail(previous_details or {}, run_id=run_id, detail_source=detail_source)
        if reused is not None:
            record["details"] = dict(_mapping_or_empty(reused.get("details")))
            record["detail_source"] = detail_source
            record["detail_status"] = "available"
            record["detail_reused"] = True
            continue
        record["details"] = _run_detail_sections(
            paths,
            run_id=run_id,
            task_id=task_id,
            agent_status=statuses_by_run.get(run_id),
            node_summary=summaries_by_run.get(run_id),
            validation=validations_by_run.get(run_id),
            active_lease=active_leases_by_run.get(run_id),
        )
        record["detail_source"] = detail_source
        record["detail_status"] = "available"
        record["detail_reused"] = False
    return sorted(by_run.values(), key=lambda item: str(item.get("run_id")))


def _run_index_record(paths: WorkflowPaths, record: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(record.get("run_id") or "")
    details = _mapping_or_empty(record.get("details"))
    index = {str(key): value for key, value in record.items() if key != "details"}
    for key in ("generated_at", "started_at", "ended_at", "heartbeat_at", "lease_expires_at"):
        if key in index:
            index[key] = _read_model_timestamp(index[key])
    if run_id and record.get("detail_status") == "available":
        index["detail_path"] = _path_for_record(paths.project_root, _run_detail_path(paths, run_id))
        index["detail_status"] = "available"
    index["available_sections"] = list(details.get("available_sections") or [])
    index["missing_sections"] = list(details.get("missing_sections") or [])
    return index


def _legacy_run_summary_record(paths: WorkflowPaths, record: Mapping[str, Any]) -> dict[str, Any]:
    summary = _run_index_record(paths, record)
    summary["compatibility_mode"] = "split_details"
    if record.get("detail_status") == "available":
        summary["details_externalized"] = True
    return summary


def _run_detail_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in record.items()}


def _run_detail_source_metadata(
    paths: WorkflowPaths,
    *,
    run_id: str,
    task_id: str | None,
    agent_status: Mapping[str, Any] | None,
    node_summary: Mapping[str, Any] | None,
    validation: Mapping[str, Any] | None,
    active_lease: Mapping[str, Any] | None,
) -> dict[str, Any]:
    run_dirs = _run_candidate_dirs(paths, run_id=run_id, task_id=task_id, active_lease=active_lease)
    files: list[Path] = []

    def add(path: Path | None) -> None:
        if path is not None:
            files.append(path)

    for run_dir in run_dirs:
        for name in (
            "prompt.md",
            "planner_prompt.md",
            "auditor_prompt.md",
            "change_request_planner_prompt.md",
            "final.md",
            "final_output.md",
            "report.md",
            "validation.json",
            "agent_status.json",
            "node_summary.json",
            "git/changed_files.json",
            "git/project_diff.patch",
        ):
            add(run_dir / name)
        logs_dir = run_dir / "logs"
        if logs_dir.is_dir():
            try:
                for path in sorted(logs_dir.glob("*")):
                    add(path)
            except OSError:
                pass
        for path in (run_dir / "stdout.log", run_dir / "stderr.log"):
            add(path)
        artifacts_dir = run_dir / "artifacts"
        if artifacts_dir.is_dir():
            try:
                for path in sorted(artifacts_dir.rglob("*")):
                    add(path)
            except OSError:
                pass
    if task_id:
        add(paths.results_dir / task_id / "human_summary.md")
    for key in ("stdout_path", "stderr_path", "scheduler_run_dir", "role_output_dir", "task_evidence_run_dir"):
        add(_project_path(paths, _first_string(_mapping_value(active_lease, key))))
    add(_project_path(paths, _mapping_value(agent_status, "_path")))
    add(_project_path(paths, _mapping_value(node_summary, "_path")))
    add(_project_path(paths, _mapping_value(validation, "_path")))
    add(paths.runtime_dir / "git_checkpoints.jsonl")

    refs = _stat_refs(paths, files)
    digest = sha256(json.dumps(refs, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": "sha256:" + digest,
        "files": refs,
    }


def _stat_refs(paths: WorkflowPaths, files: Sequence[Path]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        if not _is_dashboard_safe_path(paths, path):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        key = resolved.as_posix()
        if key in seen:
            continue
        seen.add(key)
        safe_path = _safe_dashboard_path(paths, resolved)
        if not safe_path or safe_path == "[redacted path]":
            continue
        try:
            stat_result = resolved.stat()
        except OSError:
            refs.append({"path": safe_path, "exists": False})
            continue
        refs.append(
            {
                "path": safe_path,
                "exists": True,
                "is_file": resolved.is_file(),
                "size_bytes": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
            }
        )
    return refs


def _reusable_previous_detail(
    previous_details: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    detail_source: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    previous = previous_details.get(run_id)
    if not isinstance(previous, Mapping):
        return None
    if previous.get("detail_status") != "available":
        return None
    if _mapping_or_empty(previous.get("detail_source")).get("fingerprint") != detail_source.get("fingerprint"):
        return None
    details = previous.get("details")
    if not isinstance(details, Mapping):
        return None
    return previous


def _run_details_manifest_model(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records = []
    for record in run_summaries:
        run_id = str(record.get("run_id") or "")
        if not run_id or record.get("detail_status") != "available":
            continue
        detail_path = _run_detail_path(paths, run_id)
        details = _mapping_or_empty(record.get("details"))
        records.append(
            {
                "run_id": run_id,
                "node_id": record.get("node_id"),
                "task_id": record.get("task_id"),
                "role": record.get("role"),
                "status": record.get("status"),
                "path": _path_for_record(paths.project_root, detail_path),
                "available_sections": list(details.get("available_sections") or []),
                "missing_sections": list(details.get("missing_sections") or []),
            }
        )
    return {
        "schema_version": common["schema_version"],
        "workflow_id": common["workflow_id"],
        "generated_at": common["generated_at"],
        "source_hashes": dict(common.get("source_hashes") or {}),
        "last_event_seq": common.get("last_event_seq"),
        "source_event_id": common.get("source_event_id"),
        "detail_dir": paths.value("read_models_dir").rstrip("/") + f"/{READ_MODEL_DETAIL_DIR}",
        "run_count": len(records),
        "runs": records,
    }


def _build_manifest_model(
    *,
    common: Mapping[str, Any],
    paths: WorkflowPaths,
    source_hashes: Mapping[str, Any],
    run_summaries: Sequence[Mapping[str, Any]],
    event_projection: Mapping[str, Any],
) -> dict[str, Any]:
    run_details = []
    for record in run_summaries:
        run_id = str(record.get("run_id") or "")
        if not run_id or record.get("detail_status") != "available":
            continue
        run_details.append(
            {
                "run_id": run_id,
                "path": _path_for_record(paths.project_root, _run_detail_path(paths, run_id)),
                "detail_source": dict(_mapping_or_empty(record.get("detail_source"))),
                "detail_reused": record.get("detail_reused") is True,
            }
        )
    return {
        "schema_version": common["schema_version"],
        "workflow_id": common["workflow_id"],
        "generated_at": common["generated_at"],
        "last_event_seq": common.get("last_event_seq"),
        "source_event_id": common.get("source_event_id"),
        "builder_version": READ_MODEL_BUILDER_VERSION,
        "source_hashes": dict(source_hashes),
        "event_replay": _event_replay_summary(event_projection, project=paths.project_root),
        "event_segment_manifest": _event_segment_manifest_ref(paths),
        "run_details": run_details,
    }


def _event_segment_manifest_ref(paths: WorkflowPaths) -> dict[str, Any]:
    path = paths.runtime_dir / "events" / "manifest.json"
    try:
        stat_result = path.stat()
    except OSError:
        return {
            "path": _path_for_record(paths.project_root, path),
            "exists": False,
        }
    return {
        "path": _path_for_record(paths.project_root, path),
        "exists": True,
        "size_bytes": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
    }


def _previous_build_manifest(paths: WorkflowPaths, *, workflow_id: str) -> Mapping[str, Any]:
    manifest = _read_json_object(paths.read_models_dir / "build_manifest.json", default={})
    if not isinstance(manifest, Mapping):
        return {}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return {}
    if manifest.get("workflow_id") != workflow_id:
        return {}
    if manifest.get("builder_version") != READ_MODEL_BUILDER_VERSION:
        return {}
    return manifest


def _previous_run_detail_cache(paths: WorkflowPaths, manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cache: dict[str, Mapping[str, Any]] = {}
    for entry in manifest.get("run_details") or []:
        if not isinstance(entry, Mapping):
            continue
        run_id = str(entry.get("run_id") or "")
        path_value = str(entry.get("path") or "")
        if not run_id or not path_value:
            continue
        path = paths.project_root / path_value if not Path(path_value).is_absolute() else Path(path_value)
        try:
            resolved = path.resolve()
            resolved.relative_to(paths.read_models_dir.resolve())
        except (OSError, ValueError):
            continue
        detail = _read_json_object(path, default={})
        if isinstance(detail, Mapping) and detail.get("run_id") == run_id:
            cache[run_id] = detail
    return cache


def _run_detail_path(paths: WorkflowPaths, run_id: str) -> Path:
    return paths.read_models_dir / READ_MODEL_DETAIL_DIR / _run_detail_filename(run_id)


def _run_detail_filename(run_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("._") or "run"
    slug = slug[:80]
    suffix = sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}_{suffix}.json"


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


def _file_written_metadata(stat_result: os.stat_result) -> dict[str, Any]:
    return {
        "last_written_at": datetime.fromtimestamp(stat_result.st_mtime, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "mtime_ns": stat_result.st_mtime_ns,
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
        stat_result = path.stat()
    except OSError:
        return None
    preview_limit = max(limit + NODE_DETAIL_REDACTION_EXTRA_BYTES, limit * 4, 8192)
    data = _read_file_prefix(path, preview_limit)
    preview_data = data[:preview_limit]
    decoded = preview_data.decode("utf-8", errors="replace")
    content = _truncate_text(_redact_detail_text(decoded), limit)
    if stat_result.st_size > len(preview_data):
        content["truncated"] = True
    record: dict[str, Any] = {
        "path": _safe_dashboard_path(paths, path),
        "size_bytes": stat_result.st_size,
        **_file_written_metadata(stat_result),
        "render_mode": _file_render_mode(path),
        "content": content["content"],
        "truncated": content["truncated"],
    }
    _add_bounded_file_hash(record, preview_data=preview_data, size_bytes=stat_result.st_size)
    return record


def _file_render_mode(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_PREVIEW_SUFFIXES:
        return "image"
    return "markdown" if suffix in {".md", ".markdown", ".mdown", ".mkd"} else "text"


def _artifact_record(paths: WorkflowPaths, path: Path) -> dict[str, Any]:
    if not path.is_file() or not _is_dashboard_safe_path(paths, path):
        return {"available": False, "path": _safe_dashboard_path(paths, path)}
    try:
        stat_result = path.stat()
    except OSError:
        return {"available": False, "path": _safe_dashboard_path(paths, path)}
    preview_limit = max(1200 + NODE_DETAIL_REDACTION_EXTRA_BYTES, 8192)
    data = _read_file_prefix(path, preview_limit)
    record: dict[str, Any] = {
        "available": True,
        "path": _safe_dashboard_path(paths, path),
        "size_bytes": stat_result.st_size,
        **_file_written_metadata(stat_result),
        "render_mode": _file_render_mode(path),
    }
    _add_bounded_file_hash(record, preview_data=data, size_bytes=stat_result.st_size)
    if _looks_textual(path, data):
        content = _truncate_text(_redact_detail_text(data.decode("utf-8", errors="replace")), 1200)
        if stat_result.st_size > len(data):
            content["truncated"] = True
        record["content"] = content["content"]
        record["truncated"] = content["truncated"]
    return record


def _read_file_prefix(path: Path, limit: int) -> bytes:
    if limit <= 0:
        return b""
    try:
        with path.open("rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _add_bounded_file_hash(record: dict[str, Any], *, preview_data: bytes, size_bytes: int) -> None:
    if size_bytes <= len(preview_data):
        record["sha256"] = "sha256:" + sha256(preview_data).hexdigest()
        record["full_sha256_available"] = True
    else:
        record["preview_sha256"] = "sha256:" + sha256(preview_data).hexdigest()
        record["full_sha256_available"] = False


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
        last_updated_at = _first_timestamp_string(
            _mapping_value(context.latest, "updated_at"),
            _mapping_value(context.validation, "validated_at"),
            _latest_failure_timestamp(by_task_failures.get(task.task_id, [])),
        )
        started_at = _first_timestamp_string(
            _mapping_value(context.latest, "started_at"),
            _mapping_value(context.latest, "prepared_at"),
            _mapping_value(context.latest, "created_at"),
            _mapping_value(context.validation, "started_at"),
            _timestamp_from_run_id(latest_run_id),
            last_updated_at if status in {"done", "skipped", "blocked"} else None,
        )
        ended_at = _first_timestamp_string(
            _mapping_value(context.latest, "ended_at"),
            _mapping_value(context.latest, "completed_at"),
            _mapping_value(context.latest, "finished_at"),
            _mapping_value(context.validation, "validated_at"),
            _mapping_value(context.latest, "updated_at") if status in {"done", "skipped", "blocked"} else None,
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
            "started_at": started_at,
            "ended_at": ended_at,
            "human_summary": _fresh_human_summary(
                load_task_human_summary(context.project_root, context.paths, task.task_id),
                expected_source_hash=task_human_summary_source_hash(context.project_root, context.paths, task),
            ),
            "last_updated_at": last_updated_at,
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
    event_total_count: int | None,
    last_event: Mapping[str, Any] | None,
    validation_manifest: Mapping[str, Any],
    active_leases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    event_source = _event_source_reference(paths, events=events, event_total_count=event_total_count, last_event=last_event)
    return {
        "plan": _sha256_file(plan_source.path),
        "plan_source": plan_source.kind,
        "plan_file": plan_source.value,
        "active_plan": _sha256_file(paths.plan_file),
        "events_head": event_source.get("events_head"),
        "events_source_mode": event_source.get("mode"),
        "events_segment_manifest": event_source.get("fingerprint"),
        "events_segment_manifest_path": event_source.get("manifest_path"),
        "state": _sha256_file(paths.runtime_dir / "state.json"),
        "expansion_registry": _sha256_file(paths.runtime_dir / "expansion_registry.json"),
        "validations_manifest": validation_manifest.get("sha256"),
        "active_leases": _active_leases_sha256(active_leases),
        "events_count": event_source.get("events_count", len(events)),
    }


def _event_source_reference(
    paths: WorkflowPaths,
    *,
    events: Sequence[Mapping[str, Any]],
    event_total_count: int | None,
    last_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    events_dir = paths.runtime_dir / "events"
    manifest = load_event_segment_manifest(events_dir)
    if isinstance(manifest, Mapping):
        compact_manifest = {
            "event_count": manifest.get("event_count"),
            "latest_event": _compact_manifest_event_ref(manifest.get("latest_event")),
            "segments": [
                _compact_segment_ref(entry)
                for entry in manifest.get("segments") or []
                if isinstance(entry, Mapping)
            ],
        }
        fingerprint = sha256(
            json.dumps(compact_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        latest = _mapping_or_empty(manifest.get("latest_event"))
        return {
            "mode": "event_manifest",
            "fingerprint": "sha256:" + fingerprint,
            "manifest_path": _path_for_record(paths.project_root, events_dir / EVENTS_MANIFEST_FILENAME),
            "events_count": _int_from_manifest(manifest.get("event_count"), default=len(events)),
            "events_head": _event_id(latest) if latest else _event_id(last_event),
        }
    segment_refs = _event_segment_stat_refs(paths)
    fallback = {
        "segments": segment_refs,
        "events_count": event_total_count,
        "events_head": _event_id(last_event),
    }
    fingerprint = sha256(json.dumps(fallback, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "mode": "event_segment_stats",
        "fingerprint": "sha256:" + fingerprint,
        "manifest_path": _path_for_record(paths.project_root, events_dir / EVENTS_MANIFEST_FILENAME),
        "events_count": event_total_count,
        "events_head": _event_id(last_event),
    }


def _compact_manifest_event_ref(value: Any) -> dict[str, Any]:
    event = _mapping_or_empty(value)
    return {
        key: event.get(key)
        for key in ("sequence", "seq", "event_id", "event_hash", "event_type", "ts", "timestamp")
        if event.get(key) is not None
    }


def _compact_segment_ref(entry: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "path",
        "size_bytes",
        "mtime_ns",
        "first_sequence",
        "last_sequence",
        "first_event_id",
        "last_event_id",
        "first_event_hash",
        "last_event_hash",
        "record_count",
        "contains_payload_refs",
    )
    return {key: entry.get(key) for key in keys if entry.get(key) is not None}


def _event_segment_stat_refs(paths: WorkflowPaths) -> list[dict[str, Any]]:
    events_dir = paths.runtime_dir / "events"
    try:
        segments = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    except OSError:
        return []
    refs: list[dict[str, Any]] = []
    for path in segments:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        refs.append(
            {
                "path": _path_for_record(paths.project_root, path),
                "size_bytes": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
            }
        )
    return refs


def _int_from_manifest(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        "started_at": task.get("started_at"),
        "ended_at": task.get("ended_at"),
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
                requires_approval = bool(_mapping_or_empty(latest.get("impact")).get("requires_approval"))
                if requires_approval:
                    approval = _approval_status_for_change_request(approvals, approval_responses, str(approval_id or ""))
                    approval_status = str(_mapping_or_empty(approval).get("status") or "")
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
        "started_at": summary.get("started_at") or _timestamp_from_run_id(run_id),
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
    phase_title = objective.get("phase_title")
    phase_id = str(objective.get("phase_id") or (_phase_id(str(phase_title)) if phase_title else ""))
    closed_at = _first_timestamp_string(
        objective.get("closed_at"),
        objective.get("ended_at"),
        objective.get("completed_at"),
        objective.get("verified_at"),
        result.get("ended_at") if isinstance(result, Mapping) else None,
        result.get("completed_at") if isinstance(result, Mapping) else None,
        result.get("verified_at") if isinstance(result, Mapping) else None,
        result.get("updated_at") if isinstance(result, Mapping) else None,
    )
    started_at = _first_timestamp_string(objective.get("started_at"))
    ended_at = _first_timestamp_string(objective.get("ended_at"), objective.get("completed_at"))
    if status == "closed" and closed_at is not None:
        started_at = started_at or closed_at
        ended_at = ended_at or closed_at
    return _node_with_timing({
        "node_id": _objective_node_id(objective_id),
        "type": "objective",
        "kind": "objective",
        "layer": "objective",
        "status": status,
        "title": str(objective.get("text") or objective_id),
        "display_label": objective_id,
        "task_id": None,
        "run_id": None,
        "objective_id": objective_id,
        "phase_id": phase_id,
        "phase": phase_title,
        "closed_at": closed_at if status == "closed" else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "group_key": _phase_node_id(phase_id) if phase_id else None,
        "importance": 80,
        "context_label": _event_context_label(task_id=None, phase=phase_title, actor_label="objective", runner_id=None),
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
        "started_at": status.get("started_at") or status.get("prepared_at") or _timestamp_from_run_id(run_id),
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
        "target_task_ids": list(payload.get("target_task_ids") or []),
        "target_failure_ids": list(payload.get("target_failure_ids") or []),
        "proposal_id": payload.get("proposal_id"),
        "expansion_type": payload.get("expansion_type"),
        "resolution_strategy": payload.get("resolution_strategy"),
        "risk": payload.get("risk"),
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


def _event_log_record_count(paths: WorkflowPaths, *, fallback: int | None) -> int | None:
    manifest = load_event_segment_manifest(paths.runtime_dir / "events")
    if isinstance(manifest, Mapping):
        value = _int_or_none(manifest.get("event_count"))
        if value is not None:
            return value
    return fallback


def _read_event_records(paths: WorkflowPaths, *, limit: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    events_dir = paths.runtime_dir / "events"
    if not events_dir.exists():
        return records
    try:
        event_paths = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    except OSError:
        return records
    if limit is not None:
        remaining = max(0, int(limit))
        if remaining <= 0:
            return records
        for path in reversed(event_paths):
            segment_records = _read_jsonl_tail(path, limit=remaining)
            for record in segment_records:
                record["_segment"] = _path_for_record(paths.project_root, path)
                records.append(record)
            remaining = max(0, int(limit) - len(records))
            if remaining <= 0:
                break
        return sorted(records, key=lambda record: (_event_sequence(record) or 0, str(record.get("event_id") or "")))
    for path in event_paths:
        for record in _read_jsonl(path):
            record["_segment"] = _path_for_record(paths.project_root, path)
            records.append(record)
    return sorted(records, key=lambda record: (_event_sequence(record) or 0, str(record.get("event_id") or "")))


def _hydrate_graph_context_event_payloads(
    paths: WorkflowPaths,
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in events:
        record = dict(event)
        event_type = str(record.get("event_type") or "")
        payload_ref = str(record.get("payload_ref") or "").strip()
        if (
            payload_ref
            and record.get("payload_compacted") is True
            and (event_type in GRAPH_CONTEXT_SIDECAR_EVENT_TYPES or "objective_verifier" in event_type)
        ):
            payload_path = Path(payload_ref)
            if not payload_path.is_absolute():
                payload_path = paths.project_root / payload_path
            sidecar = _read_json_object(payload_path, default={})
            payload = sidecar.get("payload") if isinstance(sidecar, Mapping) else None
            if isinstance(payload, Mapping):
                record["payload"] = dict(payload)
        records.append(record)
    return records


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return records
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, Mapping):
                records.append(dict(data))
    return records


def _read_jsonl_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    chunk_size = 64 * 1024
    chunks: list[bytes] = []
    newline_count = 0
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while position > 0 and newline_count <= limit:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return []
    if not chunks:
        return []
    raw = b"".join(reversed(chunks))
    lines = raw.splitlines()
    if len(lines) > limit:
        lines = lines[-limit:]
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
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


def _atomic_write_json_if_changed(path: Path, data: Mapping[str, Any]) -> bool:
    existing = _read_json_object(path, default=None)
    if isinstance(existing, Mapping) and _read_model_semantic_value(existing) == _read_model_semantic_value(data):
        return False
    _atomic_write_json(path, data)
    return True


def _atomic_write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    text = "".join(json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _atomic_write_jsonl_if_changed(path: Path, records: Sequence[Mapping[str, Any]]) -> bool:
    existing = _read_jsonl_strict(path)
    if existing is not None and _read_model_semantic_value(existing) == _read_model_semantic_value(list(records)):
        return False
    _atomic_write_jsonl(path, records)
    return True


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, Mapping):
            return None
        records.append(dict(data))
    return records


def _strict_load_read_models(
    paths: WorkflowPaths,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Sequence[Mapping[str, Any]]], dict[str, Any]]:
    json_models: dict[str, Mapping[str, Any]] = {}
    jsonl_models: dict[str, Sequence[Mapping[str, Any]]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    loaded: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    for filename in READ_MODEL_JSON_FILES:
        path = paths.read_models_dir / filename
        if not path.exists():
            missing.append(filename)
            if filename in READ_MODEL_COMPAT_OPTIONAL_FILES:
                warnings.append(f"{filename}: missing optional compatibility read-model")
            else:
                errors.append(f"{filename}: missing read-model file")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            invalid.append(filename)
            errors.append(f"{filename}: invalid JSON ({error})")
            continue
        if not isinstance(data, Mapping):
            invalid.append(filename)
            errors.append(f"{filename}: expected JSON object")
            continue
        json_models[filename] = dict(data)
        loaded.append(filename)
    for filename in READ_MODEL_JSONL_FILES:
        path = paths.read_models_dir / filename
        if not path.exists():
            missing.append(filename)
            if filename in READ_MODEL_COMPAT_OPTIONAL_FILES:
                warnings.append(f"{filename}: missing optional compatibility read-model")
            else:
                errors.append(f"{filename}: missing read-model file")
            continue
        records, line_errors = _strict_read_jsonl_with_errors(path)
        if line_errors:
            invalid.append(filename)
            errors.extend(f"{filename}: {error}" for error in line_errors[:5])
            if len(line_errors) > 5:
                errors.append(f"{filename}: {len(line_errors) - 5} additional JSONL parse errors")
            continue
        jsonl_models[filename] = records
        loaded.append(filename)
    return (
        json_models,
        jsonl_models,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "pass" if not errors else "fail",
            "loaded_files": sorted(loaded),
            "missing_files": sorted(missing),
            "invalid_files": sorted(set(invalid)),
            "errors": errors,
            "warnings": warnings,
        },
    )


def _strict_read_jsonl_with_errors(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as error:
        return [], [f"could not read JSONL ({error})"]
    with handle:
        for index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(f"line {index}: invalid JSON ({error})")
                continue
            if not isinstance(data, Mapping):
                errors.append(f"line {index}: expected JSON object")
                continue
            records.append(dict(data))
    return records, errors


def _read_model_no_write_comparison(
    paths: WorkflowPaths,
    *,
    json_models: Mapping[str, Mapping[str, Any]],
    jsonl_models: Mapping[str, Sequence[Mapping[str, Any]]],
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    changed_files: list[str] = []
    missing_files: list[str] = []
    invalid_files: list[str] = []
    checked_files: list[str] = []
    samples: list[dict[str, Any]] = []
    for filename, expected in sorted(json_models.items()):
        path = paths.read_models_dir / filename
        checked_files.append(filename)
        existing = _read_json_object(path, default=None)
        if existing is None:
            if path.exists():
                invalid_files.append(filename)
                _append_strict_sample(samples, {"issue": "invalid_json", "file": filename})
            else:
                missing_files.append(filename)
                _append_strict_sample(samples, {"issue": "missing_file", "file": filename})
            continue
        if _read_model_comparison_value(existing) != _read_model_comparison_value(expected):
            changed_files.append(filename)
            _append_strict_sample(samples, {"issue": "changed_file", "file": filename})
    for filename, expected_records in sorted(jsonl_models.items()):
        path = paths.read_models_dir / filename
        checked_files.append(filename)
        existing_records = _read_jsonl_strict(path)
        if existing_records is None:
            if path.exists():
                invalid_files.append(filename)
                _append_strict_sample(samples, {"issue": "invalid_jsonl", "file": filename})
            else:
                missing_files.append(filename)
                _append_strict_sample(samples, {"issue": "missing_file", "file": filename})
            continue
        if _read_model_comparison_value(existing_records) != _read_model_comparison_value(list(expected_records)):
            changed_files.append(filename)
            _append_strict_sample(samples, {"issue": "changed_file", "file": filename})

    detail_compare = _run_detail_no_write_comparison(paths, run_summaries=run_summaries)
    changed_files.extend(detail_compare["changed_files"])
    missing_files.extend(detail_compare["missing_files"])
    invalid_files.extend(detail_compare["invalid_files"])
    checked_files.extend(detail_compare["checked_files"])
    for sample in detail_compare["samples"]:
        _append_strict_sample(samples, sample)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not (changed_files or missing_files or invalid_files or detail_compare["extra_run_detail_files"]) else "fail",
        "checked_files": sorted(checked_files),
        "checked_run_details": detail_compare["checked_run_details"],
        "changed_files": sorted(changed_files),
        "missing_files": sorted(missing_files),
        "invalid_files": sorted(invalid_files),
        "extra_run_detail_files": detail_compare["extra_run_detail_files"],
        "samples": samples,
    }


def _run_detail_no_write_comparison(
    paths: WorkflowPaths,
    *,
    run_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    changed_files: list[str] = []
    missing_files: list[str] = []
    invalid_files: list[str] = []
    checked_files: list[str] = []
    expected_paths: set[Path] = set()
    checked_run_details = 0
    samples: list[dict[str, Any]] = []
    for record in run_summaries:
        run_id = str(record.get("run_id") or "")
        if not run_id or record.get("detail_status") != "available":
            continue
        detail_path = _run_detail_path(paths, run_id)
        rel = _path_for_record(paths.project_root, detail_path) or detail_path.as_posix()
        checked_files.append(rel)
        try:
            expected_paths.add(detail_path.resolve())
        except OSError:
            expected_paths.add(detail_path.absolute())
        existing = _read_json_object(detail_path, default=None)
        if existing is None:
            if detail_path.exists():
                invalid_files.append(rel)
                _append_strict_sample(samples, {"issue": "invalid_run_detail", "file": rel, "run_id": run_id})
            else:
                missing_files.append(rel)
                _append_strict_sample(samples, {"issue": "missing_run_detail", "file": rel, "run_id": run_id})
            continue
        checked_run_details += 1
        expected = _run_detail_record(record)
        if _read_model_comparison_value(existing) != _read_model_comparison_value(expected):
            changed_files.append(rel)
            _append_strict_sample(samples, {"issue": "changed_run_detail", "file": rel, "run_id": run_id})
    extra_files = _extra_run_detail_files(paths, expected_paths)
    for path in extra_files[:10]:
        _append_strict_sample(samples, {"issue": "extra_run_detail", "file": path})
    return {
        "checked_files": checked_files,
        "checked_run_details": checked_run_details,
        "changed_files": changed_files,
        "missing_files": missing_files,
        "invalid_files": invalid_files,
        "extra_run_detail_files": extra_files,
        "samples": samples,
    }


def _extra_run_detail_files(paths: WorkflowPaths, expected_paths: set[Path]) -> list[str]:
    detail_root = paths.read_models_dir / READ_MODEL_DETAIL_DIR
    try:
        candidates = sorted(path for path in detail_root.glob("*.json") if path.is_file())
    except OSError:
        return []
    extras: list[str] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in expected_paths:
            continue
        rel = _path_for_record(paths.project_root, path)
        if rel is not None:
            extras.append(rel)
    return extras


def _read_model_comparison_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _read_model_comparison_value(item)
            for key, item in value.items()
            if str(key) not in {"generated_at", "detail_reused", "workflow_elapsed_seconds", "active_worker_seconds"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_read_model_comparison_value(item) for item in value]
    return value


def _strict_no_write_rebuild_comparison(project: Path, *, workflow_id: str) -> dict[str, Any]:
    rebuild = rebuild_read_models(project, write=False, workflow_id=workflow_id)
    comparison = rebuild.get("no_write_comparison")
    comparison_status = comparison.get("status") if isinstance(comparison, Mapping) else None
    ok = rebuild.get("ok") is True and comparison_status == "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if ok else "fail",
        "rebuild_ok": rebuild.get("ok") is True,
        "rebuild_status": rebuild.get("status"),
        "comparison": dict(comparison) if isinstance(comparison, Mapping) else None,
        "diagnostics": rebuild.get("diagnostics") if isinstance(rebuild.get("diagnostics"), Mapping) else {},
        "errors": list(rebuild.get("errors") or []),
        "warnings": list(rebuild.get("warnings") or []),
    }


def _strict_event_chain_diagnostics(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hash_mismatches = 0
    prev_hash_mismatches = 0
    sequence_gaps = 0
    samples: list[dict[str, Any]] = []
    previous_hash: str | None = None
    previous_sequence: int | None = None
    for index, event in enumerate(events):
        event_id = _event_id(event)
        sequence = _event_sequence(event)
        recorded_hash = event.get("event_hash")
        comparable = {
            str(key): value
            for key, value in dict(event).items()
            if not str(key).startswith("_")
        }
        computed_hash = _event_hash(comparable)
        if recorded_hash != computed_hash:
            hash_mismatches += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "event_hash_mismatch",
                    "event_id": event_id,
                    "sequence": sequence,
                    "recorded": recorded_hash,
                    "computed": computed_hash,
                    "segment": event.get("_segment"),
                },
            )
        recorded_prev = event.get("prev_event_hash")
        if index == 0:
            if recorded_prev not in (None, ""):
                prev_hash_mismatches += 1
                _append_strict_sample(
                    samples,
                    {
                        "issue": "first_event_prev_hash_present",
                        "event_id": event_id,
                        "sequence": sequence,
                        "recorded_prev_event_hash": recorded_prev,
                    },
                )
        elif recorded_prev != previous_hash:
            prev_hash_mismatches += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "prev_event_hash_mismatch",
                    "event_id": event_id,
                    "sequence": sequence,
                    "recorded_prev_event_hash": recorded_prev,
                    "expected_prev_event_hash": previous_hash,
                },
            )
        if previous_sequence is not None and sequence is not None and sequence != previous_sequence + 1:
            sequence_gaps += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "event_sequence_gap",
                    "event_id": event_id,
                    "sequence": sequence,
                    "expected_sequence": previous_sequence + 1,
                },
            )
        previous_hash = str(recorded_hash) if isinstance(recorded_hash, str) else None
        previous_sequence = sequence if sequence is not None else previous_sequence
    return {
        "status": "pass" if not (hash_mismatches or prev_hash_mismatches or sequence_gaps) else "fail",
        "checked": len(events),
        "hash_mismatches": hash_mismatches,
        "prev_hash_mismatches": prev_hash_mismatches,
        "sequence_gaps": sequence_gaps,
        "samples": samples,
    }


def _strict_event_payload_sidecar_diagnostics(
    project: Path,
    paths: WorkflowPaths,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    referenced = 0
    checked = 0
    missing = 0
    mismatch = 0
    invalid = 0
    samples: list[dict[str, Any]] = []
    for event in events:
        if event.get("payload_compacted") is not True and not event.get("payload_ref"):
            continue
        referenced += 1
        event_id = _event_id(event)
        sequence = _event_sequence(event)
        expected_digest = event.get("payload_sha256")
        expected_size = _int_or_none(event.get("payload_size_bytes"))
        sidecar_path = _strict_payload_sidecar_path(project, paths, event.get("payload_ref"))
        if sidecar_path is None:
            invalid += 1
            _append_strict_sample(
                samples,
                {"issue": "invalid_payload_ref", "event_id": event_id, "sequence": sequence, "payload_ref": event.get("payload_ref")},
            )
            continue
        if not sidecar_path.exists():
            missing += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "missing_payload_sidecar",
                    "event_id": event_id,
                    "sequence": sequence,
                    "path": _path_for_record(project, sidecar_path),
                },
            )
            continue
        sidecar = _read_json_object(sidecar_path, default=None)
        if not isinstance(sidecar, Mapping) or "payload" not in sidecar:
            invalid += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "invalid_payload_sidecar",
                    "event_id": event_id,
                    "sequence": sequence,
                    "path": _path_for_record(project, sidecar_path),
                },
            )
            continue
        checked += 1
        encoded = json.dumps(sidecar.get("payload"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        actual_digest = "sha256:" + sha256(encoded).hexdigest()
        actual_size = len(encoded)
        sidecar_digest = sidecar.get("payload_sha256")
        sidecar_size = _int_or_none(sidecar.get("payload_size_bytes"))
        if (
            not isinstance(expected_digest, str)
            or actual_digest != expected_digest
            or sidecar_digest != expected_digest
            or (expected_size is not None and actual_size != expected_size)
            or (sidecar_size is not None and sidecar_size != actual_size)
        ):
            mismatch += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "payload_sidecar_mismatch",
                    "event_id": event_id,
                    "sequence": sequence,
                    "path": _path_for_record(project, sidecar_path),
                    "expected_sha256": expected_digest,
                    "sidecar_sha256": sidecar_digest,
                    "actual_sha256": actual_digest,
                    "expected_size_bytes": expected_size,
                    "sidecar_size_bytes": sidecar_size,
                    "actual_size_bytes": actual_size,
                },
            )
    return {
        "status": "pass" if not (missing or mismatch or invalid) else "fail",
        "referenced": referenced,
        "checked": checked,
        "missing": missing,
        "mismatch": mismatch,
        "invalid": invalid,
        "samples": samples,
    }


def _strict_payload_sidecar_path(project: Path, paths: WorkflowPaths, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw_path = Path(value)
    candidate = raw_path if raw_path.is_absolute() else project / raw_path
    try:
        resolved = candidate.resolve()
        sidecar_root = (paths.runtime_dir / EVENT_PAYLOAD_SIDECAR_DIR).resolve()
    except OSError:
        return None
    if not _path_is_relative_to(resolved, sidecar_root):
        return None
    return resolved


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _strict_read_model_source_hash_diagnostics(
    json_models: Mapping[str, Mapping[str, Any]],
    jsonl_models: Mapping[str, Sequence[Mapping[str, Any]]],
    events_sha256: str,
) -> dict[str, Any]:
    checked_files: list[str] = []
    events_sha256_recorded_files: list[str] = []
    events_sha256_missing_files: list[str] = []
    validation_manifest_recorded_files: list[str] = []
    events_sha256_mismatches: list[dict[str, Any]] = []
    for filename, payload in sorted(json_models.items()):
        checked_files.append(filename)
        source_hashes = payload.get("source_hashes")
        if not isinstance(source_hashes, Mapping):
            continue
        if source_hashes.get("validations_manifest"):
            validation_manifest_recorded_files.append(filename)
        event_hash = source_hashes.get("events_sha256")
        if event_hash is None:
            events_sha256_missing_files.append(filename)
        elif event_hash == events_sha256:
            events_sha256_recorded_files.append(filename)
        else:
            events_sha256_mismatches.append({"file": filename, "recorded": event_hash, "computed": events_sha256})
    for filename, records in sorted(jsonl_models.items()):
        checked_files.append(filename)
        hashes = [record.get("source_hashes") for record in records if isinstance(record.get("source_hashes"), Mapping)]
        validation_recorded = any(source.get("validations_manifest") for source in hashes if isinstance(source, Mapping))
        if validation_recorded:
            validation_manifest_recorded_files.append(filename)
        event_hashes = [
            source.get("events_sha256")
            for source in hashes
            if isinstance(source, Mapping) and source.get("events_sha256") is not None
        ]
        if not event_hashes:
            events_sha256_missing_files.append(filename)
            continue
        mismatched = [value for value in event_hashes if value != events_sha256]
        if mismatched:
            events_sha256_mismatches.append(
                {"file": filename, "recorded": mismatched[0], "computed": events_sha256, "mismatched_records": len(mismatched)}
            )
        else:
            events_sha256_recorded_files.append(filename)
    return {
        "checked_files": checked_files,
        "events_sha256": events_sha256,
        "events_sha256_recorded_files": sorted(events_sha256_recorded_files),
        "events_sha256_missing_files": sorted(events_sha256_missing_files),
        "events_sha256_mismatches": events_sha256_mismatches[:10],
        "validations_manifest_recorded_files": sorted(validation_manifest_recorded_files),
    }


def _strict_run_detail_diagnostics(project: Path, paths: WorkflowPaths, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    samples: list[dict[str, Any]] = []
    if not isinstance(manifest, Mapping):
        return {
            "status": "fail",
            "checked": 0,
            "missing": 0,
            "invalid": 0,
            "mismatched": 0,
            "orphan_files": [],
            "samples": [{"issue": "missing_run_details_manifest"}],
            "warnings": [],
        }
    runs = manifest.get("runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
        runs = []
        errors.append("run_details_manifest.json: runs must be an array")
        _append_strict_sample(samples, {"issue": "invalid_manifest_runs"})
    declared_count = _int_or_none(manifest.get("run_count"))
    if declared_count is not None and declared_count != len(runs):
        errors.append("run_details_manifest.json: run_count does not match runs length")
        _append_strict_sample(
            samples,
            {"issue": "run_count_mismatch", "declared": declared_count, "actual": len(runs)},
        )
    referenced_paths: set[Path] = set()
    checked = 0
    missing = 0
    invalid = 0
    mismatched = 0
    detail_root = (paths.read_models_dir / READ_MODEL_DETAIL_DIR).resolve()
    for entry in runs:
        if not isinstance(entry, Mapping):
            invalid += 1
            _append_strict_sample(samples, {"issue": "invalid_manifest_entry"})
            continue
        run_id = str(entry.get("run_id") or "")
        detail_path = _strict_run_detail_path(project, detail_root, entry.get("path"))
        if detail_path is None:
            invalid += 1
            _append_strict_sample(samples, {"issue": "invalid_run_detail_path", "run_id": run_id, "path": entry.get("path")})
            continue
        referenced_paths.add(detail_path)
        if not detail_path.exists():
            missing += 1
            _append_strict_sample(
                samples,
                {"issue": "missing_run_detail", "run_id": run_id, "path": _path_for_record(project, detail_path)},
            )
            continue
        detail = _read_json_object(detail_path, default=None)
        if not isinstance(detail, Mapping):
            invalid += 1
            _append_strict_sample(
                samples,
                {"issue": "invalid_run_detail_json", "run_id": run_id, "path": _path_for_record(project, detail_path)},
            )
            continue
        checked += 1
        detail_errors = _strict_run_detail_record_errors(paths, entry, detail)
        if detail_errors:
            mismatched += 1
            _append_strict_sample(
                samples,
                {
                    "issue": "run_detail_record_mismatch",
                    "run_id": run_id,
                    "path": _path_for_record(project, detail_path),
                    "errors": detail_errors[:5],
                },
            )
    orphan_files = _strict_run_detail_orphans(project, detail_root, referenced_paths)
    if orphan_files:
        warnings.append(f"{len(orphan_files)} run detail files are not referenced by run_details_manifest.json")
    return {
        "status": "pass" if not (errors or missing or invalid or mismatched) else "fail",
        "declared_run_count": declared_count,
        "manifest_run_count": len(runs),
        "checked": checked,
        "missing": missing,
        "invalid": invalid,
        "mismatched": mismatched,
        "orphan_files": orphan_files[:10],
        "samples": samples,
        "errors": errors,
        "warnings": warnings,
    }


def _strict_run_detail_path(project: Path, detail_root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw_path = Path(value)
    candidate = raw_path if raw_path.is_absolute() else project / raw_path
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not _path_is_relative_to(resolved, detail_root):
        return None
    return resolved


def _strict_run_detail_record_errors(
    paths: WorkflowPaths,
    manifest_entry: Mapping[str, Any],
    detail: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for field in ("schema_version", "workflow_id", "generated_at", "source_hashes", "run_id", "node_id", "detail_status", "details"):
        if field not in detail:
            errors.append(f"missing field {field}")
    if detail.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if detail.get("workflow_id") != paths.workflow_id:
        errors.append("workflow_id mismatch")
    if detail.get("run_id") != manifest_entry.get("run_id"):
        errors.append("run_id mismatch")
    if detail.get("node_id") != manifest_entry.get("node_id"):
        errors.append("node_id mismatch")
    if detail.get("detail_status") != "available":
        errors.append("detail_status must be available")
    if not isinstance(detail.get("source_hashes"), Mapping):
        errors.append("source_hashes must be an object")
    details = detail.get("details")
    if not isinstance(details, Mapping):
        errors.append("details must be an object")
    else:
        if details.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"details.schema_version must be {SCHEMA_VERSION}")
        if details.get("run_id") != detail.get("run_id"):
            errors.append("details.run_id mismatch")
        if not isinstance(details.get("sections"), Sequence) or isinstance(details.get("sections"), (str, bytes)):
            errors.append("details.sections must be an array")
        if not isinstance(details.get("available_sections"), Sequence) or isinstance(details.get("available_sections"), (str, bytes)):
            errors.append("details.available_sections must be an array")
        if not isinstance(details.get("missing_sections"), Sequence) or isinstance(details.get("missing_sections"), (str, bytes)):
            errors.append("details.missing_sections must be an array")
    return errors


def _strict_run_detail_orphans(project: Path, detail_root: Path, referenced_paths: set[Path]) -> list[str]:
    try:
        candidates = sorted(path.resolve() for path in detail_root.glob("*.json") if path.is_file())
    except OSError:
        return []
    return [
        value
        for value in (_path_for_record(project, path) for path in candidates if path not in referenced_paths)
        if value is not None
    ]


def _event_segment_count(events_dir: Path) -> int:
    try:
        return len([path for path in events_dir.glob("*.jsonl") if path.is_file()])
    except OSError:
        return 0


def _append_strict_sample(samples: list[dict[str, Any]], sample: Mapping[str, Any], *, limit: int = 10) -> None:
    if len(samples) < limit:
        samples.append(dict(sample))


def _read_model_semantic_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _read_model_semantic_value(item)
            for key, item in value.items()
            if str(key) not in {"generated_at", "workflow_elapsed_seconds", "active_worker_seconds"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_read_model_semantic_value(item) for item in value]
    return value


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


def _read_model_timestamp(value: Any) -> Any:
    if value is None:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        return value
    return parsed.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_timestamp_string(*values: Any) -> str | None:
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _timestamp_from_run_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:^|[^A-Za-z0-9])run_(\d{8})_(\d{6})(?:_|$)", value)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


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
