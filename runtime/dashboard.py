from __future__ import annotations

import base64
import fnmatch
import gzip
import html
import json
import mimetypes
import os
import re
import resource
import signal
import secrets
import shlex
import shutil
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from statistics import mean, median
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from runtime.adapters.base import utc_timestamp
from runtime.loopplane_home import (
    LOOPPLANE_HOME_AUTHORITY,
    LOOPPLANE_HOME_SCHEMA_VERSION,
    loopplane_home_layout,
    ensure_loopplane_home_layout,
)
from runtime.agent_runners import (
    PROMPT_DELIVERY_MODES,
    AgentRunnerConfigError,
    RunnerConfig,
    load_agent_runners,
    project_local_agent_runner_override_file,
    write_project_local_agent_runner_overrides,
)
from runtime.approval import (
    ALLOWED_APPROVAL_DECISIONS,
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
    approval_record_status,
    load_approval_policy,
    read_approval_requests,
    read_approval_responses,
)
from runtime.change_requests import (
    CHANGE_REQUEST_RESPONSES_FILENAME,
    CHANGE_REQUESTS_FILENAME,
    change_request_status_record,
    new_change_request_id,
)
from runtime.control import (
    CONTROL_REQUEST_TYPES,
    CONTROL_REQUESTS_FILENAME,
    CONTROL_RESPONSES_FILENAME,
    control_request_statuses,
    new_control_request_id,
    read_control_requests,
    read_control_responses,
)
from runtime.health import run_health_probe
from runtime.inspector import (
    CHAT_REQUESTS_FILENAME,
    CHAT_RESPONSES_FILENAME,
    INSPECTION_MODE,
    answer_inspection,
    default_allowed_paths,
)
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, default_workflow_path_values, load_workflow_config
from runtime.read_models import (
    READ_MODEL_COMPAT_OPTIONAL_FILES,
    READ_MODEL_DETAIL_DIR,
    READ_MODEL_FILES,
    active_leases_source_hash,
    rebuild_read_models,
    strict_read_model_diagnostics,
)
from runtime.scheduler import (
    ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME,
    EVENTS_MANIFEST_FILENAME,
    SCHEMA_VERSION,
    load_event_segment_manifest,
)
from runtime.workflow_lifecycle import WorkflowLifecycleError, ensure_compatibility_workflow_metadata
from runtime.workflows import create_workflow, list_workflows


DASHBOARD_ASSET_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "public"
STATIC_ASSET_FILES = (
    "static_dashboard.css",
    "static_dashboard.js",
    "loopplane_logo.png",
    "loopplane_logo_dark.png",
    "loopplane_logo_light.png",
)
DASHBOARD_ACCESS_LINK_FILENAME = "LOOPPLANE_DASHBOARD.url"
DASHBOARD_FILE_PREVIEW_LINE_LIMIT = 500
FRESHNESS_LIVE_DRIFT_EVENT_LIMIT = 200
BENIGN_FRESHNESS_DRIFT_EVENT_TYPES = frozenset(
    {
        "scheduler_started",
        "scheduler_tick",
        "scheduler_exited",
        "scheduler_wait_tick",
    }
)
BENIGN_FRESHNESS_DRIFT_WAIT_ACTIONS = frozenset(
    {
        "wait_paused",
        "wait_stopped",
        "wait_approval",
        "wait_background_job",
        "wait_config",
        "wait_no_executable_work",
    }
)
BENIGN_FRESHNESS_DRIFT_WAIT_STATUSES = frozenset(
    {
        "paused",
        "stopped",
        "waiting_approval",
        "waiting_background_job",
        "waiting_config",
        "waiting",
    }
)
READ_MODEL_REBUILD_ACTIVE_STATUSES = frozenset(
    {
        "accepted",
        "in_progress",
        "pending",
        "processing",
        "queued",
        "requested",
        "running",
        "started",
    }
)
WORKFLOW_RECENCY_READ_MODEL_FILES = (
    "workflow_status.json",
    "metrics.json",
    "version_control_status.json",
)
DASHBOARD_LIVE_READ_MODEL_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "dashboard_feed.jsonl",
    "run_index.jsonl",
    "run_summaries.jsonl",
    "metrics.json",
    "version_control_status.json",
)
READ_MODEL_CACHE_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
READ_MODEL_CACHE_DEFAULT_MAX_ENTRIES = 64
READ_MODEL_CACHE_DEFAULT_MAX_SCOPES = 8
DASHBOARD_ETAG_RUNTIME_FILES = (
    "state.json",
    CONTROL_REQUESTS_FILENAME,
    CONTROL_RESPONSES_FILENAME,
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
)
DASHBOARD_ETAG_REQUEST_FILES = (
    CHAT_REQUESTS_FILENAME,
    CHAT_RESPONSES_FILENAME,
    CHANGE_REQUESTS_FILENAME,
    CHANGE_REQUEST_RESPONSES_FILENAME,
    "dashboard_requests.jsonl",
)
REDACTED = "[REDACTED]"
SENSITIVE_KEY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "private_key",
        "secret",
        "token",
        "x_loopplane_token",
        "x-loopplane-token",
    }
)
SENSITIVE_KEY_SUFFIXES = ("_api_key", "_access_token", "_password", "_private_key", "_secret", "_token")
SAFE_TOKEN_KEY_NAMES = frozenset({"mutating_api_requires_token", "token_file", "token_required"})
RUNNER_CONFIGURATION_REQUEST_FIELDS = frozenset(
    {
        "runner_id",
        "role",
        "adapter",
        "command",
        "enabled",
        "prompt_delivery_mode",
        "timeout_seconds",
        "model",
        "reasoning_effort",
        "adapter_options",
        "reason",
    }
)
RUNNER_CONFIGURATION_FORBIDDEN_FIELDS = frozenset(
    {
        "args",
        "cwd",
        "env",
        "environment",
        "shell",
        "script",
        "subprocess",
        "permission_policy",
        "doctor",
        "check_command",
        "auth_check_command",
        "check_auth_command",
    }
)
RUNNER_COMMAND_DENIED_TOKENS = frozenset({";", "&&", "||", "|", "&", "`", "$(", ">", "<"})
RUNNER_REASONING_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh"})
DASHBOARD_LIST_SCHEMA_VERSION = "1.6"


def render_static_dashboard(
    project_root: Path | str,
    *,
    output_dir: Path | str | None = None,
    rebuild_read_models_first: bool = False,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
    embed_workflow_snapshots: bool = False,
) -> dict[str, Any]:
    prepared = _prepare_dashboard_payload(
        project_root,
        rebuild_read_models_first=rebuild_read_models_first,
        workflow_id=workflow_id,
        max_dashboard_events=max_dashboard_events,
        include_workflow_snapshots=embed_workflow_snapshots,
    )
    if not prepared.get("ok"):
        return _public_result(prepared)

    project = Path(prepared["project_root"])
    paths = prepared["_paths"]
    payload = prepared["_payload"]
    warnings = list(prepared.get("warnings") or [])
    destination = _dashboard_output_dir(project, output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "read_models").mkdir(parents=True, exist_ok=True)
    _copy_read_models(paths.read_models_dir, destination / "read_models")
    _copy_static_assets(destination)
    payload = {
        **dict(payload),
        "dashboard_dir": _project_relative(project, destination),
        "static_project_root_href": Path(os.path.relpath(project, destination)).as_posix(),
    }

    index_file = destination / "index.html"
    index_file.write_text(_render_index_html(payload, server_mode=False), encoding="utf-8")

    generated_files = [_project_relative(project, index_file)]
    generated_files.extend(_project_relative(project, destination / filename) for filename in STATIC_ASSET_FILES)
    generated_files.extend(
        _project_relative(project, destination / "read_models" / filename)
        for filename in READ_MODEL_FILES
        if (destination / "read_models" / filename).is_file()
    )
    generated_files.extend(
        _project_relative(project, path)
        for path in sorted((destination / "read_models" / READ_MODEL_DETAIL_DIR).glob("*.json"))
        if path.is_file()
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "rendered_with_warnings" if warnings else "rendered",
        "project_root": project.as_posix(),
        "workflow_id": payload["workflow_id"],
        "started_at": prepared["started_at"],
        "ended_at": utc_timestamp(),
        "dashboard_dir": _project_relative(project, destination),
        "index_file": _project_relative(project, index_file),
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "generated_files": generated_files,
        "covered_sections": [
            "workspace_selector",
            "workflow_status",
            "plan_checklist",
            "workflow_graph",
            "node_details",
            "activity_feed",
            "git_checkpoint_status",
            "runner_configuration",
            "planning_controls",
            "read_model_freshness",
            "read_model_rebuild_request",
            "control_requests",
            "approval_panel",
            "inspector_console",
            "change_requests",
            "requires_attention",
        ],
        "errors": [],
        "warnings": warnings,
        "read_model_freshness": payload["read_model_freshness"],
        "rebuild_read_models": prepared.get("rebuild_read_models"),
        "embed_workflow_snapshots": embed_workflow_snapshots,
    }


def create_dashboard_server(
    project_root: Path | str,
    *,
    port: int | str,
    host: str | None = None,
    rebuild_read_models_first: bool = False,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
) -> tuple[ThreadingHTTPServer, dict[str, Any]]:
    read_model_cache = _ReadModelCache()
    prepared = _prepare_dashboard_payload(
        project_root,
        rebuild_read_models_first=rebuild_read_models_first,
        workflow_id=workflow_id,
        max_dashboard_events=max_dashboard_events,
        include_workflow_snapshots=False,
        include_legacy_run_summaries=False,
        read_model_cache=read_model_cache,
    )
    if not prepared.get("ok"):
        raise DashboardServerError(_server_failure_from_prepared(prepared))

    project = Path(prepared["project_root"])
    paths = prepared["_paths"]
    workflow_config = prepared["_workflow_config"]
    security = _load_project_json(paths.config_file("security.json"))
    dashboard_config = _load_project_json(paths.config_file("dashboard.json"))
    host_value = str(
        host
        or _mapping(security.get("dashboard")).get("bind_host")
        or dashboard_config.get("host")
        or "127.0.0.1"
    )
    port_candidates = _dashboard_port_candidates(dashboard_config, port)
    dashboard_security = _mapping(security.get("dashboard"))
    token_required = dashboard_security.get("require_token") is not False
    mutating_api_requires_token = dashboard_security.get("mutating_api_requires_token") is not False
    token_file = _dashboard_token_file(project, paths, security)
    token = _load_or_create_dashboard_token(token_file) if token_required or mutating_api_requires_token else None

    context = {
        "project": project,
        "paths": paths,
        "workflow_config": workflow_config,
        "workflow_id": str(workflow_config.get("workflow_id") or prepared.get("workflow_id") or "unknown_workflow"),
        "host": host_value,
        "port": port_candidates[0],
        "token_required": token_required,
        "mutating_api_requires_token": mutating_api_requires_token,
        "token": token,
        "token_file": token_file,
        "same_origin_required": dashboard_security.get("same_origin_required") is not False,
        "redaction": _dashboard_redaction_config(security),
        "max_dashboard_events": prepared.get("max_dashboard_events"),
        "read_model_cache": read_model_cache,
        "started_at": utc_timestamp(),
        "server_state_file": _dashboard_server_state_file(project, paths, dashboard_config),
    }

    server: ThreadingHTTPServer | None = None
    bind_errors: list[str] = []
    for port_value in port_candidates:
        try:
            server = ThreadingHTTPServer((host_value, port_value), DashboardRequestHandler)
            break
        except OSError as error:
            bind_errors.append(f"{host_value}:{port_value}: {error}")
            if str(port).strip().lower() != "auto":
                break
    if server is None:
        raise DashboardServerError(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "status": "port_unavailable",
                "server_mode": True,
                "project_root": project.as_posix(),
                "workflow_id": context["workflow_id"],
                "host": host_value,
                "port": port,
                "errors": bind_errors or ["unable to bind dashboard port"],
                "warnings": [],
            }
        )
    server.daemon_threads = True
    server.dashboard_context = context  # type: ignore[attr-defined]
    actual_port = int(server.server_address[1])
    context["port"] = actual_port
    startup = _dashboard_server_startup(context)
    home_record = _write_loopplane_home_dashboard_server_record(context, startup)
    startup["loopplane_home"] = home_record.get("loopplane_home")
    startup["loopplane_home_dashboard_servers_file"] = home_record.get("servers_file")
    startup["loopplane_home_server_record_status"] = home_record.get("status")
    startup["warnings"].extend(str(warning) for warning in home_record.get("warnings", []))
    access_link = _write_dashboard_access_link(context, startup)
    startup["access_link_file"] = access_link.get("path")
    if access_link.get("warning"):
        startup["warnings"].append(str(access_link["warning"]))
    _write_dashboard_server_state(context, startup)
    return server, startup


class DashboardServerError(RuntimeError):
    def __init__(self, result: Mapping[str, Any]):
        super().__init__("dashboard server could not start")
        self.result = dict(result)


def dashboard_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def list_dashboard_servers(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_result = list_workflows(project)
    if workflow_result.get("ok") is not True:
        return {
            "schema_version": DASHBOARD_LIST_SCHEMA_VERSION,
            "ok": False,
            "status": workflow_result.get("status") or "workspace_unavailable",
            "project_root": project.as_posix(),
            "workspace_id": workflow_result.get("workspace_id"),
            "registry_file": workflow_result.get("registry_file"),
            "current_workflow_file": workflow_result.get("current_workflow_file"),
            "current_workflow_id": workflow_result.get("current_workflow_id"),
            "workflow_count": workflow_result.get("workflow_count", 0),
            "server_record_count": 0,
            "active_server_count": 0,
            "workflows": [],
            "dashboard_servers": [],
            "errors": list(workflow_result.get("errors") or []),
            "warnings": list(workflow_result.get("warnings") or []),
            "recovery_actions": list(workflow_result.get("recovery_actions") or []),
            "workflow_list": workflow_result,
        }

    workflow_records = [
        record for record in _sequence(workflow_result.get("workflows")) if isinstance(record, Mapping)
    ]
    workflows: list[dict[str, Any]] = []
    dashboard_servers: list[dict[str, Any]] = []
    warnings = list(workflow_result.get("warnings") or [])
    current_workflow_id = str(workflow_result.get("current_workflow_id") or "")
    home_index = _load_loopplane_home_dashboard_servers(
        project,
        workspace_id=str(workflow_result.get("workspace_id") or ""),
    )
    warnings.extend(str(warning) for warning in home_index.get("warnings", []))

    for record in workflow_records:
        workflow = _dashboard_list_workflow_record(
            project,
            record,
            current_workflow_id=current_workflow_id,
        )
        workflows.append(workflow)
        server = _mapping(workflow.get("dashboard_server"))
        if server.get("exists") is True:
            dashboard_servers.append(
                {
                    "workflow_id": workflow.get("workflow_id"),
                    "workflow_name": workflow.get("name"),
                    "status": server.get("status"),
                    "state_file": server.get("state_file"),
                    "host": server.get("host"),
                    "port": server.get("port"),
                    "url": server.get("url"),
                    "pid": server.get("pid"),
                    "started_at": server.get("started_at"),
                    "selected_workflow_id": server.get("selected_workflow_id"),
                    "current_workflow_id": server.get("current_workflow_id"),
                    "liveness": server.get("liveness"),
                    "stale": server.get("stale"),
                    "stale_reasons": list(server.get("stale_reasons") or []),
                    "token_file": server.get("token_file"),
                    "token_required": server.get("token_required"),
                    "server_state": server.get("server_state"),
                }
            )
        if server.get("status") in {"invalid_state_path", "invalid_json", "unreadable"}:
            warnings.append(
                f"workflow {workflow.get('workflow_id') or 'unknown'} dashboard server record is {server.get('status')}."
            )

    active_server_count = sum(
        1 for server in dashboard_servers if server.get("liveness") == "alive" and server.get("stale") is False
    )
    known_pids = {
        int(server["pid"])
        for server in dashboard_servers
        if isinstance(server.get("pid"), int) and int(server["pid"]) > 0
    }
    orphan_processes = _discover_orphan_dashboard_processes(project, known_pids=known_pids)
    if orphan_processes:
        sample = "; ".join(_orphan_dashboard_process_label(process) for process in orphan_processes[:3])
        warnings.append(
            f"Detected {len(orphan_processes)} unregistered dashboard process(es) for this project; "
            f"{sample}. Run `loopplane dashboard cleanup --terminate-orphans` to stop matching orphan processes."
        )
    return {
        "schema_version": DASHBOARD_LIST_SCHEMA_VERSION,
        "ok": True,
        "status": "listed" if dashboard_servers else "no_active_dashboard",
        "project_root": project.as_posix(),
        "workspace_id": workflow_result.get("workspace_id"),
        "registry_file": workflow_result.get("registry_file"),
        "current_workflow_file": workflow_result.get("current_workflow_file"),
        "registry_generated_at": workflow_result.get("registry_generated_at"),
        "current_workflow_id": workflow_result.get("current_workflow_id"),
        "current_found": workflow_result.get("current_found"),
        "workflow_count": len(workflows),
        "server_record_count": len(dashboard_servers),
        "active_server_count": active_server_count,
        "loopplane_home": home_index.get("loopplane_home"),
        "loopplane_home_dashboard_servers_file": home_index.get("servers_file"),
        "loopplane_home_server_record_count": home_index.get("server_record_count", 0),
        "loopplane_home_active_server_count": home_index.get("active_server_count", 0),
        "workflows": workflows,
        "dashboard_servers": dashboard_servers,
        "loopplane_home_dashboard_servers": list(home_index.get("servers") or []),
        "orphan_dashboard_processes": orphan_processes,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "dashboard list is read-only; it inspects project-local .loopplane/workflow_registry.json "
            "and workflow runtime dashboard_server.json records, plus matching machine-local "
            "LOOPPLANE_HOME dashboard discovery records, without updating workflow truth or "
            ".loopplane/current_workflow.json."
        ),
    }


def cleanup_dashboard_servers(
    project_root: Path | str,
    *,
    stop: bool = False,
    terminate_orphans: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    listing = list_dashboard_servers(project)
    if listing.get("ok") is not True:
        result = dict(listing)
        result["status"] = "cleanup_unavailable"
        return result

    removed_project_records: list[str] = []
    removed_home_records: list[dict[str, Any]] = []
    terminated_processes: list[dict[str, Any]] = []
    warnings = list(listing.get("warnings") or [])
    errors: list[str] = []

    for server in _sequence(listing.get("dashboard_servers")):
        if not isinstance(server, Mapping):
            continue
        should_remove = bool(stop or server.get("stale"))
        if stop and server.get("liveness") == "alive":
            terminated = _terminate_dashboard_pid(server.get("pid"), reason="registered_dashboard_stop")
            if terminated:
                terminated_processes.append(terminated)
        if not should_remove:
            continue
        state_file_value = str(server.get("state_file") or "").strip()
        if not state_file_value:
            continue
        try:
            state_file = _resolve_project_relative_file(project, state_file_value)
        except (OSError, WorkflowPathError, ValueError) as error:
            warnings.append(f"Unable to resolve dashboard state file {state_file_value!r}: {error}")
            continue
        if state_file.exists():
            try:
                state_file.unlink()
                removed_project_records.append(_safe_project_path(project, state_file))
            except OSError as error:
                errors.append(f"Unable to remove dashboard state file {state_file_value}: {error}")

    if terminate_orphans:
        for process in _sequence(listing.get("orphan_dashboard_processes")):
            if not isinstance(process, Mapping):
                continue
            pid = _optional_int(process.get("pid"))
            if _is_current_process_or_ancestor(pid):
                warnings.append(
                    "Skipped dashboard orphan termination for current process lineage: "
                    + _orphan_dashboard_process_label(process)
                )
                continue
            terminated = _terminate_dashboard_pid(process.get("pid"), reason="orphan_dashboard_cleanup")
            if terminated:
                terminated_processes.append(terminated)

    removed_home_records = _cleanup_loopplane_home_dashboard_records(
        project,
        workspace_id=str(listing.get("workspace_id") or ""),
        stop=stop,
    )
    refreshed = list_dashboard_servers(project)
    return {
        "schema_version": DASHBOARD_LIST_SCHEMA_VERSION,
        "ok": not errors,
        "status": "cleaned" if not errors else "cleanup_failed",
        "project_root": project.as_posix(),
        "workspace_id": listing.get("workspace_id"),
        "current_workflow_id": listing.get("current_workflow_id"),
        "stop_requested": stop,
        "terminate_orphans": terminate_orphans,
        "removed_project_records": removed_project_records,
        "removed_project_record_count": len(removed_project_records),
        "removed_loopplane_home_records": removed_home_records,
        "removed_loopplane_home_record_count": len(removed_home_records),
        "terminated_processes": terminated_processes,
        "terminated_process_count": len(terminated_processes),
        "dashboard_servers_remaining": refreshed.get("dashboard_servers", []),
        "orphan_dashboard_processes_remaining": refreshed.get("orphan_dashboard_processes", []),
        "warnings": warnings,
        "errors": errors,
    }


def dashboard_list_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def format_dashboard_cleanup_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane dashboard cleanup: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"removed_project_record_count: {result.get('removed_project_record_count', 0)}",
        f"removed_loopplane_home_record_count: {result.get('removed_loopplane_home_record_count', 0)}",
        f"terminated_process_count: {result.get('terminated_process_count', 0)}",
    ]
    for key in ("removed_project_records", "terminated_processes", "warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            for value in values:
                if isinstance(value, Mapping):
                    lines.append(f"  - {json.dumps(value, sort_keys=True)}")
                else:
                    lines.append(f"  - {value}")
    return "\n".join(lines) + "\n"


def format_dashboard_list_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane dashboard list: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        lines.append(f"workflow_count: {result.get('workflow_count')}")
        lines.append(f"server_record_count: {result.get('server_record_count')}")
        lines.append(f"active_server_count: {result.get('active_server_count')}")
        lines.append(
            "loopplane_home_dashboard_servers_file: "
            f"{result.get('loopplane_home_dashboard_servers_file') or 'unknown'}"
        )
        lines.append(f"loopplane_home_server_record_count: {result.get('loopplane_home_server_record_count')}")
        lines.append(f"loopplane_home_active_server_count: {result.get('loopplane_home_active_server_count')}")
        orphan_processes = result.get("orphan_dashboard_processes")
        if isinstance(orphan_processes, Sequence) and orphan_processes and not isinstance(orphan_processes, (str, bytes)):
            lines.append("orphan_dashboard_processes:")
            for process in orphan_processes:
                if isinstance(process, Mapping):
                    lines.append(
                        "  - "
                        f"pid={process.get('pid')} "
                        f"ppid={process.get('ppid') or 'unknown'} "
                        f"port={process.get('port') or 'unknown'} "
                        f"started_at={process.get('started_at') or 'unknown'} "
                        f"registration={process.get('registration') or 'unregistered'} "
                        f"command={process.get('command')}"
                    )
        workflows = result.get("workflows")
        if isinstance(workflows, Sequence) and workflows and not isinstance(workflows, (str, bytes)):
            lines.append("workflows:")
            for workflow in workflows:
                if not isinstance(workflow, Mapping):
                    continue
                labels = workflow.get("labels")
                label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
                progress = workflow.get("progress_summary")
                progress_text = _dashboard_progress_text(progress)
                completion = _mapping(workflow.get("completion_freshness"))
                git_status = _mapping(workflow.get("git_checkpoint_status"))
                runtime_health = _mapping(workflow.get("runtime_health"))
                server = _mapping(workflow.get("dashboard_server"))
                lines.append(
                    "  "
                    f"- {workflow.get('workflow_id') or 'unknown'} "
                    f"name={workflow.get('name') or 'unknown'} "
                    f"status={workflow.get('status') or 'unknown'} "
                    f"created_at={workflow.get('created_at') or 'unknown'} "
                    f"last_seen_at={workflow.get('last_seen_at') or 'unknown'} "
                    f"labels={label_text or 'none'}"
                )
                if progress_text:
                    lines.append(f"    progress: {progress_text}")
                lines.append(f"    completion: {completion.get('status') or 'unknown'}")
                lines.append(f"    runtime_health: {runtime_health.get('status') or 'unknown'}")
                lines.append(f"    git_checkpoint: {git_status.get('status') or 'unknown'}")
                if server.get("exists") is True:
                    lines.append(
                        "    dashboard_server: "
                        f"{server.get('url') or 'unknown'} "
                        f"pid={server.get('pid') or 'unknown'} "
                        f"liveness={server.get('liveness') or 'unknown'} "
                        f"stale={str(bool(server.get('stale'))).lower()}"
                    )
                else:
                    lines.append(f"    dashboard_server: {server.get('status') or 'missing'}")
        else:
            lines.append("workflows: none")
        if not result.get("server_record_count"):
            lines.append("dashboard_servers: none")
        home_servers = result.get("loopplane_home_dashboard_servers")
        if isinstance(home_servers, Sequence) and home_servers and not isinstance(home_servers, (str, bytes)):
            lines.append("loopplane_home_dashboard_servers:")
            for server in home_servers:
                if not isinstance(server, Mapping):
                    continue
                lines.append(
                    "  "
                    f"- {server.get('workflow_id') or 'unknown'} "
                    f"url={server.get('url') or 'unknown'} "
                    f"pid={server.get('pid') or 'unknown'} "
                    f"liveness={server.get('liveness') or 'unknown'} "
                    f"stale={str(bool(server.get('stale'))).lower()}"
                )
                guidance = server.get("health_guidance")
                if guidance:
                    lines.append(f"    health_guidance: {guidance}")
        else:
            lines.append("loopplane_home_dashboard_servers: none")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    recovery_actions = result.get("recovery_actions")
    if isinstance(recovery_actions, Sequence) and recovery_actions and not isinstance(recovery_actions, (str, bytes)):
        lines.append("recovery_actions:")
        lines.extend(f"  - {value}" for value in recovery_actions)
    return "\n".join(lines) + "\n"


def format_dashboard_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane dashboard: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    if result.get("server_mode"):
        lines.append(f"url: {result.get('url')}")
        lines.append(f"api_base_url: {result.get('api_base_url')}")
        lines.append(f"host: {result.get('host')}")
        lines.append(f"port: {result.get('port')}")
        lines.append(f"token_required: {_text(result.get('token_required'))}")
        if result.get("token_file"):
            lines.append(f"token_file: {result['token_file']}")
        if result.get("access_link_file"):
            lines.append(f"access_link_file: {result['access_link_file']}")
        if result.get("server_state_file"):
            lines.append(f"server_state_file: {result['server_state_file']}")
    if result.get("dashboard_dir"):
        lines.append(f"dashboard_dir: {result['dashboard_dir']}")
    if result.get("index_file"):
        lines.append(f"index_file: {result['index_file']}")
    if result.get("read_models_dir"):
        lines.append(f"read_models_dir: {result['read_models_dir']}")
    freshness = result.get("read_model_freshness")
    if isinstance(freshness, Mapping):
        lines.append(f"read_model_freshness: {freshness.get('status', 'unknown')}")
        if freshness.get("status") == "stale" and freshness.get("rebuild_command"):
            lines.append(f"rebuild_command: {freshness['rebuild_command']}")
    covered = result.get("covered_sections")
    if isinstance(covered, Sequence) and not isinstance(covered, (str, bytes)) and covered:
        lines.append("covered_sections:")
        lines.extend(f"  - {section}" for section in covered)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _dashboard_asset_content_type(filename: str) -> str:
    if filename.endswith(".css"):
        return "text/css; charset=utf-8"
    if filename.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if filename.endswith(".png"):
        return "image/png"
    if filename.endswith(".svg"):
        return "image/svg+xml; charset=utf-8"
    return "application/octet-stream"


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "LoopPlaneDashboard/1.0"

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def context(self) -> Mapping[str, Any]:
        return self.server.dashboard_context  # type: ignore[attr-defined]

    def _handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query)
        if method == "GET" and path.strip("/") in STATIC_ASSET_FILES:
            self._serve_asset(path.rsplit("/", 1)[-1])
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        if not self._authenticated(method, query):
            self._send_json({"ok": False, "status": "unauthorized", "errors": ["dashboard token required"]}, HTTPStatus.UNAUTHORIZED)
            return
        if method == "POST" and not self._same_origin_allowed():
            self._send_json({"ok": False, "status": "forbidden", "errors": ["same-origin check failed"]}, HTTPStatus.FORBIDDEN)
            return

        try:
            if method == "GET":
                self._route_get(path, query)
            elif method == "POST":
                self._route_post(path)
            else:
                self._send_json({"ok": False, "status": "method_not_allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)
        except (OSError, json.JSONDecodeError, WorkflowPathError, ValueError) as error:
            self._send_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "status": "request_failed",
                    "errors": [str(error)],
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _route_get(self, path: str, query: Mapping[str, list[str]] | None = None) -> None:
        query = query or {}
        if path in {"/", "/index.html"}:
            self._serve_index(query)
            return
        if path.startswith("/read_models/"):
            self._serve_read_model(path.removeprefix("/read_models/"))
            return
        if path == "/api/health":
            self._send_json(_dashboard_server_status(self.context))
            return
        if path == "/api/dashboard":
            requested_workflow = _first_query_value(query, "workflow", "workflow_id")
            if requested_workflow:
                resolved = _resolve_workflow(self.context, requested_workflow)
                if not resolved.get("ok"):
                    self._send_json(resolved, HTTPStatus.NOT_FOUND)
                    return
                self._send_dashboard_data(_workflow_context(self.context, resolved))
                return
            self._send_json(_dashboard_server_status(self.context))
            return
        if path in {"/api/workspace", "/api/workspaces"}:
            self._send_json(_workspace_payload(self.context))
            return
        if path == "/api/workspace/workflows":
            self._send_json(_workflow_list_payload(self.context))
            return
        if path.startswith("/api/workspace/workflows/"):
            workflow_id = unquote(path.removeprefix("/api/workspace/workflows/"))
            resolved = _resolve_workflow(self.context, workflow_id)
            if resolved.get("ok"):
                self._send_json(_workspace_workflow_detail_response(self.context, resolved))
            else:
                self._send_json(resolved, HTTPStatus.NOT_FOUND)
            return

        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "workflows":
            workflow_id = parts[2]
            resolved = _resolve_workflow(self.context, workflow_id)
            if not resolved.get("ok"):
                self._send_json(resolved, HTTPStatus.NOT_FOUND)
                return
            workflow_context = _workflow_context(self.context, resolved)
            if len(parts) == 4 and parts[3] == "status":
                self._send_json(_read_model_response(workflow_context, "workflow_status.json"))
                return
            if len(parts) == 4 and parts[3] == "plan-index":
                self._send_json(_read_model_response(workflow_context, "plan_index.json"))
                return
            if len(parts) == 4 and parts[3] == "graph":
                self._send_json(_read_model_response(workflow_context, "workflow_graph.json"))
                return
            if len(parts) == 4 and parts[3] in {"dashboard", "dashboard-data"}:
                self._send_dashboard_data(workflow_context)
                return
            if len(parts) == 4 and parts[3] in {"read-model-diagnostics", "strict-diagnostics"}:
                self._send_json(strict_read_model_diagnostics(workflow_context["project"], workflow_id=workflow_id))
                return
            if len(parts) == 4 and parts[3] == "runners":
                self._send_json(_runner_configuration_response(workflow_context))
                return
            if len(parts) == 4 and parts[3] == "approvals":
                self._send_json(_approval_status_response(workflow_context, include_all=True))
                return
            if len(parts) == 4 and parts[3] == "control-requests":
                self._send_json(_control_status_response(workflow_context))
                return
            if len(parts) == 4 and parts[3] == "files":
                self._serve_project_file(workflow_context, query)
                return
            if len(parts) == 5 and parts[3] == "runs":
                run_payload = _run_detail_payload(workflow_context, parts[4])
                self._send_json(run_payload, HTTPStatus.OK if run_payload.get("ok") else HTTPStatus.NOT_FOUND)
                return
        self._send_json({"ok": False, "status": "not_found", "path": path}, HTTPStatus.NOT_FOUND)

    def _route_post(self, path: str) -> None:
        body = self._redacted_request_body(self._read_json_body())
        if path == "/api/workspace/workflows":
            result = _create_workspace_workflow(self.context, body)
            self._send_json(result, _workspace_create_http_status(result))
            return
        if path == "/api/workflows/init":
            result = _record_dashboard_request(self.context, "workflow_init", body)
            self._send_json(result, HTTPStatus.ACCEPTED)
            return

        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "workflows":
            workflow_id = parts[2]
            resolved = _resolve_workflow(self.context, workflow_id)
            if not resolved.get("ok"):
                self._send_json(resolved, HTTPStatus.NOT_FOUND)
                return
            if not _workflow_allows_mutation(resolved):
                self._send_json(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "ok": False,
                        "status": "read_only_workflow",
                        "workflow_id": workflow_id,
                        "errors": ["mutating dashboard requests are rejected for archived, read-only, immutable, or policy-ineligible workflows"],
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            workflow_context = _workflow_context(self.context, resolved)
            action = parts[3]
            if len(parts) == 4 and action in {"plan", "audit", "activate-plan"}:
                request_type = {"activate-plan": "activate_plan"}.get(action, action)
                self._send_json(_record_dashboard_request(workflow_context, request_type, body), HTTPStatus.ACCEPTED)
                return
            if len(parts) == 4 and action == "control-requests":
                control_type = str(body.get("type") or body.get("control_type") or body.get("action") or "").strip()
                result = _record_control_request(workflow_context, control_type, payload=_json_safe_object(body))
                self._send_json(result, HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.BAD_REQUEST)
                return
            if len(parts) == 4 and action in {"rebuild-read-models", "read-model-rebuild"}:
                result = _record_read_model_rebuild_request(workflow_context, body)
                self._send_json(result, HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.BAD_REQUEST)
                return
            if len(parts) == 4 and action == "chat":
                result = _record_chat_request(workflow_context, body)
                self._send_json(result, HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.BAD_REQUEST)
                return
            if len(parts) == 4 and action == "change-requests":
                text = str(body.get("user_request") or body.get("message") or body.get("text") or "").strip()
                result = _record_change_request(
                    workflow_context,
                    text,
                    source="dashboard_api",
                    metadata={"dashboard_request": _json_safe_object(body)},
                )
                self._send_json(result, HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.BAD_REQUEST)
                return
            if (len(parts) == 4 and action == "runner-configuration") or (
                len(parts) == 5 and action == "runners" and parts[4] == "configuration-requests"
            ):
                result = _record_runner_configuration_request(workflow_context, body)
                status = HTTPStatus.ACCEPTED if result.get("ok") else (
                    HTTPStatus.FORBIDDEN if result.get("status") == "trusted_local_required" else HTTPStatus.BAD_REQUEST
                )
                self._send_json(result, status)
                return
            if len(parts) == 6 and action == "approvals" and parts[5] == "respond":
                result = _record_approval_response(
                    workflow_context,
                    parts[4],
                    body,
                )
                self._send_json(result, HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.BAD_REQUEST)
                return
        self._send_json({"ok": False, "status": "not_found", "path": path}, HTTPStatus.NOT_FOUND)

    def _serve_index(self, query: Mapping[str, Sequence[str]] | None = None) -> None:
        requested_workflow = _first_query_value(query or {}, "workflow", "workflow_id")
        selected_workflow = requested_workflow or _current_workflow_id(self.context) or str(self.context.get("workflow_id") or "")
        prepared = _prepare_dashboard_shell_payload(
            self.context["project"],
            workflow_id=selected_workflow,
            max_dashboard_events=_optional_positive_int(self.context.get("max_dashboard_events")),
        )
        if not prepared.get("ok"):
            self._send_html(_render_error_html(_public_result(prepared)), HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        payload = dict(prepared["_payload"])
        self._send_html(_render_index_html(payload, server_mode=True))

    def _serve_asset(self, filename: str) -> None:
        if filename not in STATIC_ASSET_FILES:
            self._send_json({"ok": False, "status": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        path = DASHBOARD_ASSET_DIR / filename
        content_type = _dashboard_asset_content_type(filename)
        self._send_bytes(path.read_bytes(), content_type)

    def _serve_read_model(self, raw_filename: str) -> None:
        filename = unquote(raw_filename).strip("/")
        if filename.startswith(f"{READ_MODEL_DETAIL_DIR}/"):
            detail_rel = PurePosixPath(filename)
            if ".." in detail_rel.parts or detail_rel.suffix != ".json":
                self._send_json({"ok": False, "status": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            path = self.context["paths"].read_models_dir / Path(*detail_rel.parts)
            detail_root = (self.context["paths"].read_models_dir / READ_MODEL_DETAIL_DIR).resolve()
            try:
                resolved = path.resolve()
            except OSError:
                self._send_json({"ok": False, "status": "not_found", "file": filename}, HTTPStatus.NOT_FOUND)
                return
            if resolved != detail_root and detail_root not in resolved.parents:
                self._send_json({"ok": False, "status": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            if not resolved.is_file():
                self._send_json({"ok": False, "status": "not_found", "file": filename}, HTTPStatus.NOT_FOUND)
                return
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            data = json.dumps(_redact_dashboard_value(self.context, payload), indent=2, sort_keys=True).encode("utf-8")
            self._send_bytes(data, "application/json; charset=utf-8")
            return
        if filename not in READ_MODEL_FILES or "/" in filename:
            self._send_json({"ok": False, "status": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        path = self.context["paths"].read_models_dir / filename
        if not path.is_file():
            self._send_json({"ok": False, "status": "not_found", "file": filename}, HTTPStatus.NOT_FOUND)
            return
        if filename.endswith(".json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            data = json.dumps(_redact_dashboard_value(self.context, payload), indent=2, sort_keys=True).encode("utf-8")
            self._send_bytes(data, "application/json; charset=utf-8")
            return
        records = [_redact_dashboard_value(self.context, record) for record in _read_jsonl(path)]
        data = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records).encode("utf-8")
        self._send_bytes(data, "application/x-ndjson; charset=utf-8")

    def _serve_project_file(self, context: Mapping[str, Any], query: Mapping[str, list[str]]) -> None:
        raw_value = (query.get("path") or [""])[0]
        resolved = _resolve_dashboard_project_file(context, raw_value)
        if resolved is None or not resolved.is_file():
            self._send_json({"ok": False, "status": "not_found", "path": raw_value}, HTTPStatus.NOT_FOUND)
            return
        tail_mode = _truthy_query_value((query.get("tail") or [""])[0])
        if tail_mode:
            max_lines = _optional_positive_int((query.get("max_lines") or [None])[0]) or DASHBOARD_FILE_PREVIEW_LINE_LIMIT
            content, truncated = _read_file_tail(resolved, max_lines=max_lines)
            if truncated:
                content = f"[Showing the last {max_lines} lines. Open file to view the full file.]\n" + content
            redacted = _redact_dashboard_text(_mapping(context.get("redaction")), content)
            self._send_bytes(redacted.encode("utf-8"), "text/plain; charset=utf-8")
            return
        preview_mode = _truthy_query_value((query.get("preview") or [""])[0])
        if preview_mode:
            max_lines = _optional_positive_int((query.get("max_lines") or [None])[0]) or DASHBOARD_FILE_PREVIEW_LINE_LIMIT
            content, truncated = _read_file_preview(resolved, max_lines=max_lines)
            if truncated:
                content = (
                    content.rstrip("\n")
                    + f"\n\n[Preview truncated after {max_lines} lines. Use Open file to view the full file.]\n"
                )
            redacted = _redact_dashboard_text(_mapping(context.get("redaction")), content)
            self._send_bytes(redacted.encode("utf-8"), "text/plain; charset=utf-8")
            return
        data = resolved.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            self._send_json({"ok": False, "status": "file_too_large", "path": raw_value}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        content_type, _encoding = mimetypes.guess_type(resolved.as_posix())
        if content_type and content_type.startswith("image/"):
            self._send_bytes(data, content_type)
            return
        content = data.decode("utf-8", errors="replace")
        redacted = _redact_dashboard_text(_mapping(context.get("redaction")), content)
        self._send_bytes(redacted.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Content-Length must be an integer")
        if length > 1024 * 1024:
            raise ValueError("request body is too large")
        if length == 0:
            return {}
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("request body must be a JSON object")
        return dict(payload)

    def _authenticated(self, method: str, query: Mapping[str, Sequence[str]]) -> bool:
        token_required = self.context.get("token_required") or (
            method == "POST" and self.context.get("mutating_api_requires_token")
        )
        if not token_required:
            return True
        expected = self.context.get("token")
        if not isinstance(expected, str) or not expected:
            return False
        candidates = [
            self.headers.get("X-LoopPlane-Token", ""),
            _bearer_token(self.headers.get("Authorization", "")),
        ]
        if method == "GET":
            candidates.extend(query.get("token", []))
        return any(secrets.compare_digest(str(candidate), expected) for candidate in candidates if candidate)

    def _same_origin_allowed(self) -> bool:
        if not self.context.get("same_origin_required"):
            return True
        allowed_hosts = _allowed_origin_netlocs(self.context)
        host = _normalize_netloc(self.headers.get("Host"))
        if host and host not in allowed_hosts:
            return False
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"}:
                return False
            if _normalize_netloc(parsed.netloc) not in allowed_hosts:
                return False
        return True

    def _redacted_request_body(self, body: Mapping[str, Any]) -> dict[str, Any]:
        redacted = _redact_dashboard_value(self.context, body)
        return dict(redacted) if isinstance(redacted, Mapping) else {}

    def _send_dashboard_data(self, context: Mapping[str, Any]) -> None:
        request_started = time.perf_counter()
        etag_started = time.perf_counter()
        etag = _dashboard_data_etag(context)
        etag_ms = _dashboard_elapsed_ms(etag_started)
        if _request_etag_matches(self.headers.get("If-None-Match"), etag):
            self._send_not_modified(
                etag,
                headers={
                    "X-LoopPlane-Dashboard-Result": "not_modified",
                    "X-LoopPlane-Dashboard-Duration-Ms": str(_dashboard_elapsed_ms(request_started)),
                    "X-LoopPlane-Dashboard-ETag-Ms": str(etag_ms),
                },
            )
            return
        build_started = time.perf_counter()
        payload = _dashboard_data_response(context)
        build_ms = _dashboard_elapsed_ms(build_started)
        payload["dashboard_etag"] = etag
        payload["dashboard_diagnostics"] = {
            "response_mode": "full",
            "etag": etag,
            "timings": {
                "etag_ms": etag_ms,
                "payload_build_ms": build_ms,
                "total_ms": _dashboard_elapsed_ms(request_started),
            },
        }
        self._send_json(
            payload,
            HTTPStatus.OK if payload.get("ok") else HTTPStatus.NOT_FOUND,
            headers={
                "ETag": etag,
                "Cache-Control": "no-cache",
                "X-LoopPlane-Dashboard-Result": "full",
                "X-LoopPlane-Dashboard-Duration-Ms": str(_dashboard_elapsed_ms(request_started)),
            },
        )

    def _send_json(
        self,
        payload: Mapping[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        data = json.dumps(_redact_dashboard_value(self.context, payload), indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(str(name), str(value))
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(data)

    def _send_not_modified(self, etag: str, *, headers: Mapping[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(str(name), str(value))
        self.end_headers()

    def _send_html(self, html_text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(html_text.encode("utf-8"), "text/html; charset=utf-8", status=status)

    def _send_bytes(self, data: bytes, content_type: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _prepare_dashboard_payload(
    project_root: Path | str,
    *,
    rebuild_read_models_first: bool,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
    include_workflow_snapshots: bool = True,
    include_legacy_run_summaries: bool = True,
    read_model_cache: "_ReadModelCache | None" = None,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        ensure_compatibility_workflow_metadata(project, updated_by="loopplane dashboard")
        workflow_id = _dashboard_default_workflow_id(project, workflow_id)
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError) as error:
        return _failure(project=project, started_at=started_at, message=f"Unable to load workflow configuration: {error}")

    rebuild_result: Mapping[str, Any] | None = None
    if rebuild_read_models_first:
        rebuild_result = rebuild_read_models(
            project,
            write=True,
            workflow_id=workflow_id,
            max_dashboard_events=max_dashboard_events,
        )
        if not rebuild_result.get("ok"):
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "status": "read_model_rebuild_failed",
                "project_root": project.as_posix(),
                "workflow_id": workflow_config.get("workflow_id"),
                "started_at": started_at,
                "ended_at": utc_timestamp(),
                "dashboard_dir": None,
                "index_file": None,
                "read_models_dir": paths.value("read_models_dir"),
                "errors": list(rebuild_result.get("errors") or ["read model rebuild failed"]),
                "warnings": list(rebuild_result.get("warnings") or []),
                "rebuild_read_models": dict(rebuild_result),
            }

    models = _load_read_models(
        paths.read_models_dir,
        cache=read_model_cache,
        cache_scope=_read_model_cache_scope(project, paths, workflow_id),
        include_legacy_run_summaries=include_legacy_run_summaries,
    )
    if models["errors"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "missing_read_models",
            "project_root": project.as_posix(),
            "workflow_id": workflow_config.get("workflow_id"),
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "dashboard_dir": None,
            "index_file": None,
            "read_models_dir": _project_relative(project, paths.read_models_dir),
            "errors": models["errors"],
            "warnings": [],
            "rebuild_read_models": dict(rebuild_result) if rebuild_result is not None else None,
        }

    freshness = _read_model_freshness(project, paths, models["json"])
    warnings = _freshness_warning_messages(freshness)
    workflow_id = str(workflow_config.get("workflow_id") or models["json"]["workflow_status.json"].get("workflow_id") or "unknown_workflow")
    dashboard_event_limit = _dashboard_payload_event_limit(
        paths,
        explicit_limit=max_dashboard_events,
        rebuild_result=rebuild_result,
    )
    context = {
        "project": project,
        "paths": paths,
        "workflow_config": workflow_config,
        "workflow_id": workflow_id,
    }
    workflows = _workflow_records(context)
    workflow_record = _workflow_record_for_id(workflows, workflow_id)
    workflow_title = _workflow_display_title(workflow_id, workflow=workflow_record, read_models=models["json"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "server_mode": False,
        "rendered_at": utc_timestamp(),
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "workflow_title": workflow_title,
        "max_dashboard_events": dashboard_event_limit,
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "read_model_files": list(READ_MODEL_FILES),
        "read_models": models["json"],
        "jsonl_models": models["jsonl"],
        "plan_markdown": _plan_markdown_payload(project, paths, models["json"].get("plan_index.json")),
        "node_details": _node_details_payload(
            context,
            models,
            include_split_run_details=include_legacy_run_summaries,
        ),
        "read_model_freshness": freshness,
        "read_model_rebuild": _read_model_rebuild_payload(context, workflow=workflow_record, freshness=freshness),
        "workspace": _workspace_selection_metadata(context, workflows=workflows, selected_workflow_id=workflow_id),
        "workflows": workflows,
        "workflow_summaries": _workflow_summaries(context, workflows),
        "runner_configuration": _runner_configuration_payload(context),
        "planning_controls": _planning_controls_payload(context, workflow=workflow_record),
        "execution_controls": _execution_controls_payload(context, workflow=workflow_record),
        "approval_controls": _approval_controls_payload(context, workflow=workflow_record),
        "inspector_console": _inspector_console_payload(context, workflow=workflow_record),
    }
    if include_workflow_snapshots:
        payload["workflow_snapshots"] = _workflow_snapshots(context, workflows, read_model_cache=read_model_cache)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "loaded_with_warnings" if warnings else "loaded",
        "project_root": project.as_posix(),
        "workflow_id": payload["workflow_id"],
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "max_dashboard_events": dashboard_event_limit,
        "errors": [],
        "warnings": warnings,
        "read_model_freshness": freshness,
        "rebuild_read_models": dict(rebuild_result) if rebuild_result is not None else None,
        "_payload": payload,
        "_paths": paths,
        "_workflow_config": workflow_config,
    }


def _prepare_dashboard_shell_payload(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        ensure_compatibility_workflow_metadata(project, updated_by="loopplane dashboard shell")
        workflow_id = _dashboard_default_workflow_id(project, workflow_id)
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError) as error:
        return _failure(project=project, started_at=started_at, message=f"Unable to load workflow shell: {error}")

    selected_workflow_id = str(workflow_config.get("workflow_id") or workflow_id or "unknown_workflow")
    context = {
        "project": project,
        "paths": paths,
        "workflow_config": workflow_config,
        "workflow_id": selected_workflow_id,
    }
    workflows = _workflow_records_for_shell(project, paths=paths, workflow_id=selected_workflow_id)
    workflow = _workflow_record_for_id(workflows, selected_workflow_id)
    workflow_title = _workflow_display_title(selected_workflow_id, workflow=workflow, read_models={})
    freshness = {
        "schema_version": SCHEMA_VERSION,
        "status": "loading",
        "summary": "Dashboard data is loading from the selected workflow API.",
        "checked_files": [],
        "warnings": [],
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "server_mode": True,
        "initial_dashboard_load": True,
        "rendered_at": utc_timestamp(),
        "project_root": project.as_posix(),
        "workflow_id": selected_workflow_id,
        "workflow_title": workflow_title,
        "max_dashboard_events": _optional_positive_int(max_dashboard_events),
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "read_model_files": list(READ_MODEL_FILES),
        "read_models": {},
        "jsonl_models": {},
        "plan_markdown": _loading_plan_markdown_payload(paths),
        "node_details": {},
        "read_model_freshness": freshness,
        "read_model_rebuild": _loading_read_model_rebuild_payload(context, workflow=workflow),
        "read_model_diagnostics": {
            "read_models": {
                "cache_enabled": False,
                "response_mode": "shell",
                "disk_reads": 0,
                "disk_read_bytes": 0,
            }
        },
        "rebuild_read_models": None,
        "workspace": _workspace_selection_metadata(context, workflows=workflows, selected_workflow_id=selected_workflow_id),
        "workflows": workflows,
        "workflow_summaries": _workflow_summaries(context, workflows),
        "runner_configuration": _loading_control_payload(selected_workflow_id, "runner_configuration"),
        "planning_controls": _loading_control_payload(selected_workflow_id, "planning_controls"),
        "execution_controls": _loading_control_payload(selected_workflow_id, "execution_controls"),
        "approval_controls": _loading_control_payload(selected_workflow_id, "approval_controls"),
        "inspector_console": _loading_control_payload(selected_workflow_id, "inspector_console"),
        "errors": [],
        "warnings": [],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "shell",
        "project_root": project.as_posix(),
        "workflow_id": selected_workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "warnings": [],
        "_payload": payload,
        "_paths": paths,
        "_workflow_config": workflow_config,
    }


def _workflow_records_for_shell(project: Path, *, paths: WorkflowPaths, workflow_id: str) -> list[dict[str, Any]]:
    registry_path = project / ".loopplane" / "workflow_registry.json"
    if registry_path.is_file():
        registry = _load_project_json(registry_path)
        workflows = registry.get("workflows")
        if isinstance(workflows, Sequence) and not isinstance(workflows, (str, bytes)):
            records = [dict(record) for record in workflows if isinstance(record, Mapping)]
            if records:
                return records
    return [
        {
            "workflow_id": workflow_id,
            "name": "current workflow",
            "status": "unknown",
            "workflow_root": paths.value("workflow_root"),
            "plan_file": paths.value("plan_file"),
            "read_models_dir": paths.value("read_models_dir"),
            "completion_marker": f"{paths.value('runtime_dir')}/plan_loop_complete.json",
            "read_only": False,
            "archived": False,
            "summary": {"one_line": "Current LoopPlane workflow."},
        }
    ]


def _loading_plan_markdown_payload(paths: WorkflowPaths) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "loading",
        "path": paths.value("plan_file"),
        "active_plan_file": paths.value("plan_file"),
        "plan_source": {},
        "content": "",
        "size_bytes": 0,
        "errors": [],
    }


def _loading_control_payload(workflow_id: str, kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "loading",
        "workflow_id": workflow_id,
        "mutation_allowed": False,
        "mutation_blockers": ["dashboard_data_loading"],
        "request_record_only": True,
        "pending_count": 0,
        "recent": [],
        "warnings": ["Dashboard data is loading."],
        "kind": kind,
    }


def _loading_read_model_rebuild_payload(
    context: Mapping[str, Any],
    *,
    workflow: Mapping[str, Any] | None,
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    endpoint = f"/api/workflows/{workflow_id}/rebuild-read-models"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "loading" if not blockers else "disabled",
        "workflow_id": workflow_id,
        "freshness_status": "loading",
        "freshness_summary": "Dashboard data is loading from the selected workflow API.",
        "mutation_allowed": not blockers,
        "request_allowed": False,
        "rebuild_in_progress": False,
        "in_progress_summary": None,
        "mutation_blockers": blockers,
        "request_record_only": True,
        "control_type": "rebuild_read_models",
        "endpoint": endpoint,
        "control_endpoint": f"/api/workflows/{workflow_id}/control-requests",
        "requests_path": _project_relative(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": _project_relative(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "pending_count": 0,
        "recent": [],
        "latest_request_id": None,
        "latest_status": None,
        "commands": [
            f"loopplane rebuild-read-models --project {project.as_posix()} --workflow {workflow_id}",
            f"loopplane dashboard --project {project.as_posix()} --workflow {workflow_id} --rebuild-read-models",
        ],
        "warnings": ["Dashboard data is loading."],
    }


def benchmark_dashboard_loading(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    max_dashboard_events: int | None = None,
    iterations: int = 3,
    warmups: int = 1,
    include_no_write_rebuild: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    iterations = max(1, int(iterations))
    warmups = max(0, int(warmups))
    try:
        context = _dashboard_benchmark_context(
            project,
            workflow_id=workflow_id,
            max_dashboard_events=max_dashboard_events,
            read_model_cache=_ReadModelCache(),
        )
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError, ValueError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "iterations": iterations,
            "warmups": warmups,
            "operations": {},
            "errors": [str(error)],
            "warnings": [],
        }

    selected_workflow_id = str(context["workflow_id"])
    operations: dict[str, Any] = {
        "dashboard_shell": _dashboard_benchmark_operation(
            iterations=iterations,
            warmups=warmups,
            build=lambda: _dashboard_benchmark_shell_payload(
                project,
                workflow_id=selected_workflow_id,
                max_dashboard_events=max_dashboard_events,
            ),
            extract=_dashboard_benchmark_payload_stats,
        ),
        "dashboard_data_etag": _dashboard_benchmark_operation(
            iterations=iterations,
            warmups=warmups,
            build=lambda: {"etag": _dashboard_data_etag(context)},
            extract=_dashboard_benchmark_etag_stats,
        ),
        "selected_dashboard_data_cold_cache": _dashboard_benchmark_operation(
            iterations=iterations,
            warmups=warmups,
            build=lambda: _dashboard_data_response(
                _dashboard_benchmark_context(
                    project,
                    workflow_id=selected_workflow_id,
                    max_dashboard_events=max_dashboard_events,
                    read_model_cache=_ReadModelCache(),
                )
            ),
            extract=_dashboard_benchmark_payload_stats,
        ),
    }
    warm_cache = _ReadModelCache()
    warm_context = _dashboard_benchmark_context(
        project,
        workflow_id=selected_workflow_id,
        max_dashboard_events=max_dashboard_events,
        read_model_cache=warm_cache,
    )
    operations["selected_dashboard_data_warm_cache"] = _dashboard_benchmark_operation(
        iterations=iterations,
        warmups=warmups,
        build=lambda: _dashboard_data_response(warm_context),
        extract=_dashboard_benchmark_payload_stats,
    )
    if include_no_write_rebuild:
        operations["no_write_rebuild"] = _dashboard_benchmark_operation(
            iterations=iterations,
            warmups=warmups,
            build=lambda: rebuild_read_models(
                project,
                write=False,
                workflow_id=selected_workflow_id,
                max_dashboard_events=max_dashboard_events,
            ),
            extract=_dashboard_benchmark_rebuild_stats,
        )
    errors = [
        f"{name}: {sample_error}"
        for name, operation in operations.items()
        for sample in operation.get("samples", [])
        for sample_error in _sequence(sample.get("errors"))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "status": "benchmarked" if not errors else "benchmarked_with_errors",
        "project_root": project.as_posix(),
        "workflow_id": selected_workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "iterations": iterations,
        "warmups": warmups,
        "max_dashboard_events": _optional_positive_int(max_dashboard_events),
        "operations": operations,
        "budgets": {
            "dashboard_shell_ms": 1000,
            "selected_dashboard_data_ms": 1500,
            "selected_dashboard_data_maxrss_kb": 350 * 1024,
            "etag_ms": 200,
        },
        "errors": errors,
        "warnings": [],
    }


def _dashboard_benchmark_context(
    project: Path,
    *,
    workflow_id: str | None,
    max_dashboard_events: int | None,
    read_model_cache: "_ReadModelCache | None",
) -> dict[str, Any]:
    ensure_compatibility_workflow_metadata(project, updated_by="loopplane dashboard benchmark")
    selected_workflow_id = _dashboard_default_workflow_id(project, workflow_id)
    workflow_config = load_workflow_config(project, workflow_id=selected_workflow_id)
    paths = WorkflowPaths.from_config(project, workflow_config)
    return {
        "project": project,
        "paths": paths,
        "workflow_config": workflow_config,
        "workflow_id": str(workflow_config.get("workflow_id") or selected_workflow_id or "unknown_workflow"),
        "max_dashboard_events": _optional_positive_int(max_dashboard_events),
        "read_model_cache": read_model_cache,
    }


def _dashboard_benchmark_shell_payload(
    project: Path,
    *,
    workflow_id: str,
    max_dashboard_events: int | None,
) -> dict[str, Any]:
    prepared = _prepare_dashboard_shell_payload(
        project,
        workflow_id=workflow_id,
        max_dashboard_events=max_dashboard_events,
    )
    payload = dict(prepared.get("_payload") or {})
    payload["_benchmark_status"] = prepared.get("status")
    payload["_benchmark_ok"] = prepared.get("ok")
    payload["_benchmark_errors"] = list(prepared.get("errors") or [])
    return payload


def _dashboard_benchmark_operation(
    *,
    iterations: int,
    warmups: int,
    build: Any,
    extract: Any,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    last_stats: dict[str, Any] = {}
    for index in range(warmups + iterations):
        started = time.perf_counter()
        rss_before = _dashboard_maxrss_kb()
        errors: list[str] = []
        try:
            payload = build()
            stats = extract(payload)
        except Exception as error:  # pragma: no cover - surfaced in benchmark result for operator diagnosis.
            stats = {}
            errors = [str(error)]
        stats_errors = [str(error) for error in _sequence(stats.pop("errors", []))]
        elapsed_ms = _dashboard_elapsed_ms(started)
        rss_after = _dashboard_maxrss_kb()
        sample = {
            "iteration": index + 1 - warmups,
            "elapsed_ms": elapsed_ms,
            "maxrss_kb": rss_after,
            "maxrss_delta_kb": max(0, rss_after - rss_before),
            **stats,
            "errors": [*stats_errors, *errors],
        }
        last_stats = stats
        if index >= warmups:
            samples.append(sample)
    return {
        "iterations": iterations,
        "warmups": warmups,
        "samples": samples,
        "summary": _dashboard_benchmark_summary(samples),
        "last": last_stats,
    }


def _dashboard_benchmark_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    elapsed_values = [float(sample.get("elapsed_ms") or 0) for sample in samples]
    rss_values = [int(sample.get("maxrss_kb") or 0) for sample in samples]
    payload_values = [int(sample.get("payload_bytes") or 0) for sample in samples if sample.get("payload_bytes") is not None]
    disk_read_values = [
        int(sample.get("read_model_disk_read_bytes") or 0)
        for sample in samples
        if sample.get("read_model_disk_read_bytes") is not None
    ]
    return {
        "elapsed_ms": _dashboard_benchmark_numeric_summary(elapsed_values),
        "maxrss_kb": _dashboard_benchmark_numeric_summary(rss_values),
        "payload_bytes": _dashboard_benchmark_numeric_summary(payload_values),
        "read_model_disk_read_bytes": _dashboard_benchmark_numeric_summary(disk_read_values),
        "sample_count": len(samples),
        "error_count": sum(1 for sample in samples if sample.get("errors")),
    }


def _dashboard_benchmark_numeric_summary(values: Sequence[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {"min": None, "max": None, "mean": None, "median": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(float(mean(values)), 3),
        "median": round(float(median(values)), 3),
    }


def _dashboard_benchmark_payload_stats(payload: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = _mapping(_mapping(payload.get("read_model_diagnostics")).get("read_models"))
    json_models = payload.get("read_models")
    jsonl_models = payload.get("jsonl_models")
    return {
        "ok": bool(payload.get("ok", payload.get("_benchmark_ok", True))),
        "status": str(payload.get("status") or payload.get("_benchmark_status") or "unknown"),
        "payload_bytes": _dashboard_json_size_bytes(payload),
        "workflow_count": len(_sequence(payload.get("workflows"))),
        "json_model_count": len(json_models) if isinstance(json_models, Mapping) else 0,
        "jsonl_model_count": len(jsonl_models) if isinstance(jsonl_models, Mapping) else 0,
        "read_model_disk_reads": int(diagnostics.get("disk_reads") or 0),
        "read_model_disk_read_bytes": int(diagnostics.get("disk_read_bytes") or 0),
        "read_model_cache_hits": int(diagnostics.get("cache_hits") or 0),
        "skipped_legacy_run_summaries": int(diagnostics.get("skipped_legacy_run_summaries") or 0),
        "errors": [str(error) for error in _sequence(payload.get("errors") or payload.get("_benchmark_errors"))],
    }


def _dashboard_benchmark_etag_stats(payload: Mapping[str, Any]) -> dict[str, Any]:
    etag = str(payload.get("etag") or "")
    return {
        "ok": bool(etag),
        "status": "ok" if etag else "missing_etag",
        "etag": etag,
        "payload_bytes": len(etag.encode("utf-8")),
        "errors": [] if etag else ["etag was not generated"],
    }


def _dashboard_benchmark_rebuild_stats(payload: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = _mapping(payload.get("diagnostics"))
    counts = _mapping(diagnostics.get("counts"))
    return {
        "ok": bool(payload.get("ok")),
        "status": str(payload.get("status") or "unknown"),
        "payload_bytes": _dashboard_json_size_bytes(payload),
        "events_loaded": counts.get("events_loaded"),
        "run_records": counts.get("run_records"),
        "written_files": len(_sequence(payload.get("written_files"))),
        "errors": [str(error) for error in _sequence(payload.get("errors"))],
    }


def _dashboard_json_size_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def _dashboard_maxrss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def format_dashboard_benchmark_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane dashboard benchmark: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"iterations: {result.get('iterations')}",
        f"warmups: {result.get('warmups')}",
    ]
    operations = result.get("operations")
    if isinstance(operations, Mapping):
        lines.append("operations:")
        for name, operation in operations.items():
            summary = _mapping(_mapping(operation).get("summary"))
            elapsed = _mapping(summary.get("elapsed_ms"))
            payload_bytes = _mapping(summary.get("payload_bytes"))
            disk_read_bytes = _mapping(summary.get("read_model_disk_read_bytes"))
            lines.append(
                "  - "
                + str(name)
                + ": elapsed_mean_ms="
                + str(elapsed.get("mean"))
                + " elapsed_max_ms="
                + str(elapsed.get("max"))
                + " payload_max_bytes="
                + str(payload_bytes.get("max"))
                + " disk_read_max_bytes="
                + str(disk_read_bytes.get("max"))
            )
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in result.items() if not str(key).startswith("_")}


def _dashboard_default_workflow_id(project: Path, requested_workflow_id: str | None) -> str | None:
    if requested_workflow_id is not None and str(requested_workflow_id).strip():
        return str(requested_workflow_id).strip()
    current_record = _load_project_json(project / ".loopplane" / "current_workflow.json")
    current_workflow_id = str(current_record.get("current_workflow_id") or "").strip()
    registry_path = project / ".loopplane" / "workflow_registry.json"
    if not registry_path.is_file():
        return current_workflow_id or requested_workflow_id
    registry = _load_project_json(registry_path)
    workflows = registry.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)) or len(workflows) < 2:
        return current_workflow_id or requested_workflow_id
    indexed_records = [
        (index, _mapping(record))
        for index, record in enumerate(workflows)
        if isinstance(record, Mapping) and str(record.get("workflow_id") or "").strip()
    ]
    if len(indexed_records) < 2:
        return current_workflow_id or requested_workflow_id
    if current_workflow_id:
        for _index, record in indexed_records:
            if str(record.get("workflow_id") or "").strip() == current_workflow_id and _dashboard_workflow_has_read_models(project, record):
                return current_workflow_id
    visible_records = [
        (index, record)
        for index, record in indexed_records
        if _dashboard_workflow_has_read_models(project, record)
    ]
    selection_records = visible_records or indexed_records
    _latest_index, latest_record = max(
        selection_records,
        key=lambda item: _dashboard_workflow_recency_key(project, item[1], fallback_index=item[0]),
    )
    return str(latest_record.get("workflow_id") or "").strip() or requested_workflow_id


def _dashboard_workflow_has_read_models(project: Path, record: Mapping[str, Any]) -> bool:
    read_models_dir = record.get("read_models_dir")
    if not isinstance(read_models_dir, str) or not read_models_dir.strip():
        return False
    try:
        read_models_path = _resolve_project_relative_file(project, read_models_dir)
    except WorkflowPathError:
        return False
    return all(
        (read_models_path / filename).is_file()
        for filename in READ_MODEL_FILES
        if filename not in READ_MODEL_COMPAT_OPTIONAL_FILES
    )


def _dashboard_workflow_recency_key(project: Path, record: Mapping[str, Any], *, fallback_index: int) -> tuple[float, int]:
    timestamps: list[datetime] = []
    for key in ("last_seen_at", "updated_at", "generated_at", "created_at"):
        parsed = _parse_dashboard_timestamp(record.get(key))
        if parsed is not None:
            timestamps.append(parsed)
    read_models_dir = record.get("read_models_dir")
    if isinstance(read_models_dir, str) and read_models_dir.strip():
        try:
            read_models_path = _resolve_project_relative_file(project, read_models_dir)
        except WorkflowPathError:
            read_models_path = None
    else:
        read_models_path = None
    if read_models_path is not None:
        for filename in WORKFLOW_RECENCY_READ_MODEL_FILES:
            model_path = read_models_path / filename
            payload = _read_optional_json(model_path)
            parsed = _parse_dashboard_timestamp(payload.get("generated_at"))
            if parsed is not None:
                timestamps.append(parsed)
            try:
                if model_path.is_file():
                    timestamps.append(datetime.fromtimestamp(model_path.stat().st_mtime, tz=UTC))
            except OSError:
                pass
    latest = max(timestamps) if timestamps else datetime.fromtimestamp(0, tz=UTC)
    return (latest.timestamp(), fallback_index)


def _dashboard_payload_event_limit(
    paths: WorkflowPaths,
    *,
    explicit_limit: int | None,
    rebuild_result: Mapping[str, Any] | None,
) -> int:
    if explicit_limit is not None:
        return max(1, int(explicit_limit))
    if isinstance(rebuild_result, Mapping) and rebuild_result.get("max_dashboard_events") is not None:
        try:
            return max(1, int(rebuild_result["max_dashboard_events"]))
        except (TypeError, ValueError):
            pass
    dashboard_config = _load_project_json(paths.config_file("dashboard.json"))
    configured = dashboard_config.get("max_dashboard_events") if isinstance(dashboard_config, Mapping) else None
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    raw_value = os.environ.get("LOOPPLANE_MAX_DASHBOARD_EVENTS")
    if raw_value is not None:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass
    return 200


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _truthy_query_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_file_preview(path: Path, *, max_lines: int) -> tuple[str, bool]:
    lines: list[str] = []
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    truncated = True
                    break
                lines.append(line)
    except OSError:
        raise
    return "".join(lines), truncated


def _read_file_tail(path: Path, *, max_lines: int) -> tuple[str, bool]:
    lines: deque[str] = deque(maxlen=max_lines)
    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                count += 1
                lines.append(line)
    except OSError:
        raise
    return "".join(lines), count > max_lines


def _server_failure_from_prepared(result: Mapping[str, Any]) -> dict[str, Any]:
    public = _public_result(result)
    public["server_mode"] = True
    return public


def _dashboard_server_startup(context: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    host = str(context["host"])
    port = int(context["port"])
    workspace = _workspace_metadata(project)
    current_workflow_id = _current_workflow_id(context)
    selected_workflow_id = str(context["workflow_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "serving",
        "server_mode": True,
        "project_root": project.as_posix(),
        "workspace_id": workspace.get("workspace_id"),
        "workflow_id": selected_workflow_id,
        "selected_workflow_id": selected_workflow_id,
        "current_workflow_id": current_workflow_id,
        "selection_scope": "dashboard_visualization_only",
        "started_at": context["started_at"],
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}/",
        "api_base_url": f"http://{host}:{port}/api",
        "max_dashboard_events": _optional_positive_int(context.get("max_dashboard_events")) or 200,
        "token_required": bool(context.get("token_required")),
        "mutating_api_requires_token": bool(context.get("mutating_api_requires_token")),
        "same_origin_required": bool(context.get("same_origin_required")),
        "token_file": _project_relative(project, context["token_file"]) if context.get("token_file") else None,
        "server_state_file": _project_relative(project, context["server_state_file"]),
        "errors": [],
        "warnings": [],
    }


def _dashboard_server_status(context: Mapping[str, Any]) -> dict[str, Any]:
    current_workflow_id = _current_workflow_id(context)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "serving",
        "server_mode": True,
        "workflow_id": context["workflow_id"],
        "selected_workflow_id": context["workflow_id"],
        "current_workflow_id": current_workflow_id,
        "host": context["host"],
        "port": context["port"],
        "started_at": context["started_at"],
        "max_dashboard_events": _optional_positive_int(context.get("max_dashboard_events")) or 200,
        "token_required": bool(context.get("token_required")),
        "mutating_api_requires_token": bool(context.get("mutating_api_requires_token")),
        "same_origin_required": bool(context.get("same_origin_required")),
    }


def _write_dashboard_server_state(context: Mapping[str, Any], startup: Mapping[str, Any]) -> None:
    path = context["server_state_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(startup)
    payload["pid"] = os.getpid()
    payload["token"] = None
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_dashboard_access_link(context: Mapping[str, Any], startup: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    path = project / DASHBOARD_ACCESS_LINK_FILENAME
    access_url = _dashboard_access_url(
        str(startup.get("url") or ""),
        context.get("token"),
        workflow_id=startup.get("selected_workflow_id") or startup.get("workflow_id"),
    )
    content = f"[InternetShortcut]\nURL={access_url}\n"
    try:
        path.write_text(content, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as error:
        return {
            "path": path.resolve().as_posix(),
            "warning": f"Unable to write dashboard access link file {path}: {error}",
        }
    return {"path": path.resolve().as_posix()}


def _dashboard_access_url(base_url: str, token: Any, *, workflow_id: Any = None) -> str:
    url = base_url
    for key, value in (("token", token), ("workflow", workflow_id)):
        if not value:
            continue
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{quote(key, safe='')}={quote(str(value), safe='')}"
    return url


def _write_loopplane_home_dashboard_server_record(
    context: Mapping[str, Any],
    startup: Mapping[str, Any],
) -> dict[str, Any]:
    layout = loopplane_home_layout()
    result: dict[str, Any] = {
        "ok": False,
        "status": "not_written",
        "loopplane_home": layout.home.as_posix(),
        "servers_file": layout.dashboard_servers_file.as_posix(),
        "warnings": [],
    }
    try:
        ensure_loopplane_home_layout(layout.home)
        payload, warnings = _read_loopplane_home_dashboard_servers_payload(layout.dashboard_servers_file)
        now = utc_timestamp()
        entry = _loopplane_home_dashboard_server_entry(context, startup, updated_at=now)
        servers = _upsert_loopplane_home_dashboard_server(
            _loopplane_home_dashboard_server_records(payload),
            entry,
        )
        updated = {
            "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
            "authority": LOOPPLANE_HOME_AUTHORITY,
            "updated_at": now,
            "servers": servers,
        }
        _atomic_write_json_file(layout.dashboard_servers_file, updated)
    except (OSError, TypeError, ValueError) as error:
        result["status"] = "write_failed"
        result["warnings"] = [f"Unable to update LOOPPLANE_HOME dashboard server index: {error}"]
        return result

    result.update(
        {
            "ok": True,
            "status": "recorded",
            "warnings": warnings,
            "record": entry,
        }
    )
    return result


def _loopplane_home_dashboard_server_entry(
    context: Mapping[str, Any],
    startup: Mapping[str, Any],
    *,
    updated_at: str,
) -> dict[str, Any]:
    project = Path(context["project"])
    pid = os.getpid()
    return {
        "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
        "authority": LOOPPLANE_HOME_AUTHORITY,
        "record_type": "dashboard_server",
        "status": startup.get("status") or "serving",
        "server_mode": True,
        "workspace_id": startup.get("workspace_id"),
        "workflow_id": startup.get("workflow_id"),
        "selected_workflow_id": startup.get("selected_workflow_id"),
        "current_workflow_id": startup.get("current_workflow_id"),
        "selection_scope": startup.get("selection_scope"),
        "project_root": project.as_posix(),
        "server_state_file": startup.get("server_state_file"),
        "host": startup.get("host"),
        "port": startup.get("port"),
        "url": startup.get("url"),
        "api_base_url": startup.get("api_base_url"),
        "pid": pid,
        "started_at": startup.get("started_at"),
        "token_required": bool(startup.get("token_required")),
        "mutating_api_requires_token": bool(startup.get("mutating_api_requires_token")),
        "same_origin_required": bool(startup.get("same_origin_required")),
        "updated_at": updated_at,
    }


def _load_loopplane_home_dashboard_servers(project: Path, *, workspace_id: str) -> dict[str, Any]:
    layout = loopplane_home_layout()
    base: dict[str, Any] = {
        "loopplane_home": layout.home.as_posix(),
        "servers_file": layout.dashboard_servers_file.as_posix(),
        "server_record_count": 0,
        "active_server_count": 0,
        "servers": [],
        "warnings": [],
    }
    if not layout.dashboard_servers_file.exists():
        return base
    try:
        payload, warnings = _read_loopplane_home_dashboard_servers_payload(layout.dashboard_servers_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        base["warnings"] = [f"Unable to read LOOPPLANE_HOME dashboard server index: {error}"]
        return base

    servers = []
    for record in _loopplane_home_dashboard_server_records(payload):
        if not _loopplane_home_dashboard_record_matches_project(record, project, workspace_id):
            continue
        servers.append(_sanitize_loopplane_home_dashboard_server_record(project, record))
    active_count = sum(1 for server in servers if server.get("liveness") == "alive" and server.get("stale") is False)
    base.update(
        {
            "server_record_count": len(servers),
            "active_server_count": active_count,
            "servers": servers,
            "warnings": warnings,
        }
    )
    return base


def _read_loopplane_home_dashboard_servers_payload(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": LOOPPLANE_HOME_SCHEMA_VERSION, "authority": LOOPPLANE_HOME_AUTHORITY, "servers": []}, warnings
    except json.JSONDecodeError as error:
        warnings.append(f"Reinitialized invalid LOOPPLANE_HOME dashboard server index: {error}")
        return {"schema_version": LOOPPLANE_HOME_SCHEMA_VERSION, "authority": LOOPPLANE_HOME_AUTHORITY, "servers": []}, warnings
    if not isinstance(payload, Mapping):
        raise ValueError("LOOPPLANE_HOME dashboard server index must be a JSON object")
    return dict(payload), warnings


def _loopplane_home_dashboard_server_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(record) for record in _sequence(payload.get("servers")) if isinstance(record, Mapping)]


def _upsert_loopplane_home_dashboard_server(
    records: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    replaced = False
    for record in records:
        if _same_loopplane_home_dashboard_server(record, entry):
            updated.append(dict(entry))
            replaced = True
        else:
            updated.append(dict(record))
    if not replaced:
        updated.append(dict(entry))
    return updated


def _same_loopplane_home_dashboard_server(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for key in ("project_root", "workspace_id", "workflow_id", "server_state_file"):
        if str(left.get(key) or "") != str(right.get(key) or ""):
            return False
    return True


def _loopplane_home_dashboard_record_matches_project(
    record: Mapping[str, Any],
    project: Path,
    workspace_id: str,
) -> bool:
    record_project = str(record.get("project_root") or "").strip()
    if record_project:
        try:
            if Path(record_project).expanduser().resolve() == project:
                return True
        except OSError:
            pass
    return bool(workspace_id and str(record.get("workspace_id") or "") == workspace_id)


def _sanitize_loopplane_home_dashboard_server_record(project: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = dict(record)
    sanitized.pop("token", None)
    if "token_file" in sanitized:
        sanitized["token_file"] = "[redacted path]"
    if "server_state" in sanitized and isinstance(sanitized["server_state"], Mapping):
        nested = dict(sanitized["server_state"])
        nested.pop("token", None)
        if "token_file" in nested:
            nested["token_file"] = "[redacted path]"
        sanitized["server_state"] = nested

    pid = _coerce_int(sanitized.get("pid"))
    liveness = _dashboard_server_liveness(pid)
    stale_reasons = _dashboard_stale_reasons(sanitized, liveness)
    state_file_value = str(sanitized.get("server_state_file") or "").strip()
    if state_file_value:
        try:
            state_file = _resolve_project_relative_file(project, state_file_value)
            sanitized["server_state_file_exists"] = state_file.is_file()
            if not state_file.is_file():
                stale_reasons.append("project_server_state_missing")
        except (OSError, WorkflowPathError, ValueError):
            sanitized["server_state_file_exists"] = False
            stale_reasons.append("project_server_state_invalid")
    sanitized["pid"] = pid
    sanitized["liveness"] = liveness
    sanitized["stale"] = bool(stale_reasons)
    sanitized["stale_reasons"] = sorted(set(stale_reasons))
    sanitized["health_guidance"] = (
        "Restart the dashboard for this project or remove the stale LOOPPLANE_HOME dashboard record."
        if stale_reasons
        else "Dashboard PID is currently observable from this machine."
    )
    return sanitized


def _atomic_write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _dashboard_server_state_file(project: Path, paths: WorkflowPaths, dashboard_config: Mapping[str, Any]) -> Path:
    raw_value = dashboard_config.get("server_state_file") or f"{paths.value('runtime_dir')}/dashboard_server.json"
    return _resolve_project_relative_file(project, _expand_dashboard_path_value(paths, str(raw_value)))


def _load_project_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return dict(payload)


def _dashboard_redaction_config(security: Mapping[str, Any]) -> dict[str, Any]:
    redaction = _mapping(security.get("redaction"))
    enabled = redaction.get("enabled") is not False
    raw_patterns = redaction.get("redact_patterns")
    patterns = [str(pattern) for pattern in _sequence(raw_patterns) if str(pattern).strip()]
    if not patterns:
        patterns = ["API_KEY", "SECRET", "TOKEN", "PASSWORD"]
    redact_env_vars = redaction.get("redact_env_vars") is not False
    env_values: list[str] = []
    if enabled and redact_env_vars:
        for key, value in os.environ.items():
            if value and len(value) >= 4 and _key_matches_redaction_patterns(key, patterns):
                env_values.append(value)
    return {
        "enabled": enabled,
        "patterns": patterns,
        "redact_env_vars": redact_env_vars,
        "env_values": sorted(set(env_values), key=len, reverse=True),
    }


def _redact_dashboard_value(context: Mapping[str, Any], value: Any, *, key: str | None = None) -> Any:
    config = _mapping(context.get("redaction"))
    if config.get("enabled") is False:
        return value
    if key is not None and _is_sensitive_dashboard_key(key):
        return REDACTED if value is not None else None
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_dashboard_value(context, item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_dashboard_value(context, item) for item in value]
    if isinstance(value, str):
        return _redact_dashboard_text(config, value)
    return value


def _is_sensitive_dashboard_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in SAFE_TOKEN_KEY_NAMES:
        return False
    return normalized in SENSITIVE_KEY_NAMES or any(normalized.endswith(suffix) for suffix in SENSITIVE_KEY_SUFFIXES)


def _redact_dashboard_text(config: Mapping[str, Any], text: str) -> str:
    redacted = text
    for secret_value in _sequence(config.get("env_values")):
        secret_text = str(secret_value)
        if secret_text:
            redacted = redacted.replace(secret_text, REDACTED)
    redacted = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        REDACTED,
        redacted,
        flags=re.DOTALL,
    )
    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", f"Bearer {REDACTED}", redacted)
    for pattern in _sequence(config.get("patterns")):
        name = str(pattern).strip()
        if not name:
            continue
        escaped = re.escape(name)
        redacted = re.sub(
            rf"(?i)\b({escaped})\b(\s*[:=]\s*)([^\s,;]+)",
            rf"\1\2{REDACTED}",
            redacted,
        )
    redacted = re.sub(
        rf"(?i)\b(access token|api key|password|secret|token)(\s+)([A-Za-z0-9._~+/=-]{{8,}})",
        rf"\1\2{REDACTED}",
        redacted,
    )
    return redacted


def _key_matches_redaction_patterns(key: str, patterns: Sequence[Any]) -> bool:
    normalized = key.upper()
    return any(str(pattern).upper() in normalized for pattern in patterns)


def _dashboard_port_candidates(dashboard_config: Mapping[str, Any], requested: int | str) -> list[int]:
    raw = str(requested).strip().lower()
    if raw == "auto":
        preferred = _dashboard_preferred_port(dashboard_config)
        port_range = dashboard_config.get("port_range")
        if isinstance(port_range, Sequence) and not isinstance(port_range, (str, bytes)) and len(port_range) >= 2:
            try:
                start = int(port_range[0])
                end = int(port_range[1])
            except (TypeError, ValueError):
                start, end = preferred, preferred + 1000
        else:
            start, end = preferred, preferred + 1000
        if start > end:
            start, end = end, start
        start = max(1, start)
        end = min(65535, end)
        candidates = list(range(start, end + 1))
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
        return candidates
    try:
        port = int(requested)
    except (TypeError, ValueError) as error:
        raise ValueError("dashboard port must be an integer or 'auto'") from error
    if port < 0 or port > 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    return [port]


def _dashboard_preferred_port(dashboard_config: Mapping[str, Any]) -> int:
    for key in ("preferred_port", "port"):
        try:
            port = int(dashboard_config.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return 3766


def _dashboard_list_workflow_record(
    project: Path,
    record: Mapping[str, Any],
    *,
    current_workflow_id: str,
) -> dict[str, Any]:
    workflow_id = str(record.get("workflow_id") or "")
    workflow: dict[str, Any] = {
        "index": record.get("index"),
        "workflow_id": workflow_id,
        "name": str(record.get("name") or ""),
        "status": str(record.get("status") or ""),
        "workflow_root": str(record.get("workflow_root") or ""),
        "created_at": str(record.get("created_at") or ""),
        "last_seen_at": str(record.get("last_seen_at") or ""),
        "read_only": bool(record.get("read_only")),
        "archived": bool(record.get("archived")),
        "current": bool(record.get("current")) or bool(current_workflow_id and workflow_id == current_workflow_id),
        "labels": list(record.get("labels") or []),
        "summary": dict(record.get("summary")) if isinstance(record.get("summary"), Mapping) else {},
        "progress_summary": _dashboard_progress_summary(project, record),
        "completion_freshness": _completion_freshness(project, record),
        "git_checkpoint_status": _git_checkpoint_status(project, record),
    }
    paths_result = _dashboard_list_paths_for_workflow(project, record)
    if paths_result.get("ok"):
        paths = paths_result["_paths"]
        security = _load_project_json(paths.config_file("security.json"))
        dashboard_config = _load_project_json(paths.config_file("dashboard.json"))
        context = {
            "project": project,
            "paths": paths,
            "workflow_id": workflow_id,
            "redaction": _dashboard_redaction_config(security),
        }
        workflow["read_models_dir"] = paths.value("read_models_dir")
        workflow["runtime_dir"] = paths.value("runtime_dir")
        workflow["dashboard_server"] = _dashboard_server_summary(project, context, dashboard_config)
        health = run_health_probe(project, workflow_id=workflow_id, strict=False, write=False)
        workflow["runtime_health"] = {
            "status": health.get("status"),
            "ok": health.get("ok"),
            "requires_attention": list(health.get("requires_attention") or []),
            "checked_workflow_id": health.get("workflow_id") or workflow_id,
        }
    else:
        workflow["read_models_dir"] = str(record.get("read_models_dir") or "")
        workflow["runtime_dir"] = str(record.get("runtime_dir") or "")
        workflow["dashboard_server"] = {
            "exists": False,
            "status": "invalid_state_path",
            "state_file": None,
            "state_file_exists": False,
            "errors": list(paths_result.get("errors") or []),
            "warnings": [],
        }
        workflow["runtime_health"] = {
            "status": "unavailable",
            "ok": False,
            "requires_attention": list(paths_result.get("errors") or []),
        }
    return workflow


def _dashboard_list_paths_for_workflow(project: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    workflow_id = str(record.get("workflow_id") or "").strip()
    try:
        workflow_root = _workflow_root_value(record)
        config_file = str(record.get("workflow_config_file") or f"{workflow_root}/config/workflow.json")
        config_path = _resolve_project_relative_file(project, config_file)
        loaded = _load_project_json(config_path) if config_path.is_file() else {}
        workflow_config = {**default_workflow_path_values(workflow_root=workflow_root), **loaded}
        workflow_config["workflow_id"] = workflow_id
        workflow_config["workflow_root"] = workflow_root
        workflow_config["workflow_config_file"] = config_file
        for field in (
            "brief_file",
            "plan_file",
            "shared_context_file",
            "results_dir",
            "runtime_dir",
            "read_models_dir",
            "requests_dir",
            "planning_dir",
            "version_control_config_file",
        ):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                workflow_config[field] = value
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError, ValueError) as error:
        return {"ok": False, "errors": [str(error)]}
    return {"ok": True, "_paths": paths, "_workflow_config": workflow_config}


def _dashboard_server_summary(
    project: Path,
    context: Mapping[str, Any],
    dashboard_config: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        state_file = _dashboard_server_state_file(project, context["paths"], dashboard_config)
    except (OSError, WorkflowPathError, ValueError) as error:
        return {
            "exists": False,
            "status": "invalid_state_path",
            "state_file": None,
            "state_file_exists": False,
            "errors": [str(error)],
            "warnings": [],
        }
    relative_state_file = _safe_project_path(project, state_file)
    base: dict[str, Any] = {
        "exists": state_file.is_file(),
        "status": "missing",
        "state_file": relative_state_file,
        "state_file_exists": state_file.is_file(),
        "state_file_mtime": _timestamp_for_path(state_file) if state_file.exists() else None,
        "errors": [],
        "warnings": [],
    }
    if not state_file.exists():
        return base
    if not state_file.is_file():
        base.update(
            {
                "status": "invalid_state_path",
                "errors": ["dashboard server state path exists but is not a file"],
            }
        )
        return base
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, Mapping):
            raise ValueError("dashboard server state must be a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        base.update({"status": "invalid_json", "errors": [str(error)]})
        return base

    sanitized = _sanitize_dashboard_server_state(project, context, state)
    pid = _coerce_int(state.get("pid"))
    liveness = _dashboard_server_liveness(pid)
    stale_reasons = _dashboard_stale_reasons(state, liveness)
    base.update(
        {
            "status": "recorded",
            "parseable": True,
            "server_state": sanitized,
            "workspace_id": sanitized.get("workspace_id"),
            "workflow_id": sanitized.get("workflow_id"),
            "selected_workflow_id": sanitized.get("selected_workflow_id"),
            "current_workflow_id": sanitized.get("current_workflow_id"),
            "selection_scope": sanitized.get("selection_scope"),
            "host": sanitized.get("host"),
            "port": sanitized.get("port"),
            "url": sanitized.get("url"),
            "api_base_url": sanitized.get("api_base_url"),
            "pid": pid,
            "started_at": sanitized.get("started_at"),
            "token_file": sanitized.get("token_file"),
            "token_required": sanitized.get("token_required"),
            "mutating_api_requires_token": sanitized.get("mutating_api_requires_token"),
            "liveness": liveness,
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
        }
    )
    return base


def _sanitize_dashboard_server_state(
    project: Path,
    context: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    redacted = _redact_dashboard_value(context, state)
    sanitized = dict(redacted) if isinstance(redacted, Mapping) else {}
    for key in ("project_root", "token_file", "server_state_file"):
        value = sanitized.get(key)
        if isinstance(value, str) and value:
            sanitized[key] = _safe_project_path_value(project, value)
    if "token" in sanitized and sanitized["token"] is not None:
        sanitized["token"] = REDACTED
    return sanitized


def _dashboard_server_liveness(pid: int | None) -> str:
    if pid is None or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def _discover_orphan_dashboard_processes(project: Path, *, known_pids: set[int]) -> list[dict[str, Any]]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    current_pid = os.getpid()
    protected_pids = {current_pid, *_current_process_ancestor_pids()}
    orphans: list[dict[str, Any]] = []
    try:
        entries = sorted(proc.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in protected_pids or pid in known_pids:
            continue
        cmdline_path = entry / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        args = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if not _cmdline_is_loopplane_dashboard(args):
            continue
        project_arg = _cmdline_project_arg(args)
        if project_arg is None:
            continue
        try:
            candidate_project = Path(project_arg).expanduser().resolve()
        except OSError:
            continue
        if candidate_project != project:
            continue
        ppid = _proc_ppid(entry)
        started_at = _proc_started_at(entry)
        port = _cmdline_port_arg(args)
        orphans.append(
            {
                "pid": pid,
                "ppid": ppid,
                "port": port,
                "started_at": started_at,
                "status": "unregistered_process",
                "project_root": project.as_posix(),
                "registration": "unregistered",
                "command": _redacted_cmdline(args),
            }
        )
    return orphans


def _orphan_dashboard_process_label(process: Mapping[str, Any]) -> str:
    parts = [f"pid={process.get('pid')}"]
    if process.get("ppid") is not None:
        parts.append(f"ppid={process.get('ppid')}")
    if process.get("port"):
        parts.append(f"port={process.get('port')}")
    if process.get("started_at"):
        parts.append(f"started_at={process.get('started_at')}")
    return " ".join(parts)


def _cmdline_is_loopplane_dashboard(args: Sequence[str]) -> bool:
    dashboard_index = next((index for index, arg in enumerate(args) if arg == "dashboard"), None)
    if dashboard_index is None:
        return False
    if not any(_cmdline_arg_is_loopplane(arg) for arg in args[: dashboard_index + 1]):
        return False
    if dashboard_index + 1 < len(args) and args[dashboard_index + 1] in {"cleanup", "list", "stop"}:
        return False
    return _cmdline_port_arg(args) is not None


def _cmdline_arg_is_loopplane(arg: str) -> bool:
    name = Path(arg).name
    return name == "loopplane" or arg.endswith("/scripts/loopplane")


def _current_process_ancestor_pids() -> set[int]:
    ancestors: set[int] = set()
    proc = Path("/proc")
    pid = os.getpid()
    for _ in range(64):
        ppid = _proc_ppid(proc / str(pid))
        if ppid is None or ppid <= 0 or ppid in ancestors:
            break
        ancestors.add(ppid)
        pid = ppid
    return ancestors


def _is_current_process_or_ancestor(pid: int | None) -> bool:
    if pid is None:
        return False
    return pid == os.getpid() or pid in _current_process_ancestor_pids()


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cmdline_project_arg(args: Sequence[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--project" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--project="):
            return arg.split("=", 1)[1]
    return None


def _cmdline_port_arg(args: Sequence[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--port="):
            return arg.split("=", 1)[1]
    return None


def _proc_ppid(proc_entry: Path) -> int | None:
    try:
        status = (proc_entry / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in status.splitlines():
        if not line.startswith("PPid:"):
            continue
        try:
            return int(line.split(":", 1)[1].strip())
        except (TypeError, ValueError):
            return None
    return None


def _proc_started_at(proc_entry: Path) -> str | None:
    try:
        stat_text = (proc_entry / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = stat_text.split()
    if len(fields) <= 21:
        return None
    try:
        start_ticks = int(fields[21])
        clock_ticks = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
    except (OSError, TypeError, ValueError):
        return None
    boot_time = _linux_boot_time()
    if boot_time is None or not clock_ticks:
        return None
    started = datetime.fromtimestamp(boot_time + (start_ticks / clock_ticks), tz=UTC)
    return started.isoformat().replace("+00:00", "Z")


def _linux_boot_time() -> float | None:
    try:
        stat = Path("/proc/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in stat.splitlines():
        if not line.startswith("btime "):
            continue
        try:
            return float(line.split()[1])
        except (IndexError, ValueError):
            return None
    return None


def _terminate_dashboard_pid(value: Any, *, reason: str) -> dict[str, Any] | None:
    pid = _coerce_int(value)
    if pid is None or pid <= 0 or pid == os.getpid():
        return None
    record = {"pid": pid, "reason": reason}
    try:
        os.kill(pid, signal.SIGTERM)
        record["signal"] = "SIGTERM"
        record["status"] = "signaled"
    except ProcessLookupError:
        record["status"] = "already_dead"
    except PermissionError as error:
        record["status"] = "permission_denied"
        record["error"] = str(error)
    except OSError as error:
        record["status"] = "signal_failed"
        record["error"] = str(error)
    return record


def _cleanup_loopplane_home_dashboard_records(
    project: Path,
    *,
    workspace_id: str,
    stop: bool,
) -> list[dict[str, Any]]:
    layout = loopplane_home_layout()
    if not layout.dashboard_servers_file.exists():
        return []
    try:
        payload, _warnings = _read_loopplane_home_dashboard_servers_payload(layout.dashboard_servers_file)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for record in _loopplane_home_dashboard_server_records(payload):
        if not _loopplane_home_dashboard_record_matches_project(record, project, workspace_id):
            kept.append(record)
            continue
        sanitized = _sanitize_loopplane_home_dashboard_server_record(project, record)
        should_remove = bool(stop or sanitized.get("stale"))
        if should_remove:
            removed.append(
                {
                    "workflow_id": sanitized.get("workflow_id"),
                    "server_state_file": sanitized.get("server_state_file"),
                    "pid": sanitized.get("pid"),
                    "liveness": sanitized.get("liveness"),
                    "stale": sanitized.get("stale"),
                    "stale_reasons": sanitized.get("stale_reasons", []),
                }
            )
        else:
            kept.append(record)
    if len(kept) != len(_loopplane_home_dashboard_server_records(payload)):
        updated = dict(payload)
        updated["servers"] = kept
        _atomic_write_json_file(layout.dashboard_servers_file, updated)
    return removed


def _redacted_cmdline(args: Sequence[str]) -> str:
    redacted: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            redacted.append("[REDACTED]")
            skip_next = False
            continue
        if arg in {"--token", "--api-key", "--password"}:
            redacted.append(arg)
            skip_next = True
            continue
        if any(arg.startswith(prefix) for prefix in ("--token=", "--api-key=", "--password=")):
            redacted.append(arg.split("=", 1)[0] + "=[REDACTED]")
            continue
        redacted.append(arg)
    text = " ".join(redacted)
    return text if len(text) <= 240 else text[:237] + "..."


def _dashboard_stale_reasons(state: Mapping[str, Any], liveness: str) -> list[str]:
    reasons: list[str] = []
    if liveness == "dead":
        reasons.append("pid_not_running")
    elif liveness == "unknown":
        reasons.append("pid_unknown")
    if str(state.get("status") or "") not in {"serving", "running"}:
        reasons.append("status_not_serving")
    if not state.get("started_at"):
        reasons.append("started_at_missing")
    if not state.get("url") and not (state.get("host") and state.get("port")):
        reasons.append("url_missing")
    return reasons


def _dashboard_progress_summary(project: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    summary = _mapping(record.get("summary"))
    progress = {
        "one_line": summary.get("one_line"),
        "tasks_total": summary.get("tasks_total", 0),
        "tasks_completed": summary.get("tasks_completed", 0),
        "tasks_blocked": summary.get("tasks_blocked", 0),
    }
    read_models_dir = _workflow_default_path_value(record, "read_models_dir")
    if read_models_dir:
        try:
            status_path = _resolve_project_relative_file(project, f"{read_models_dir.rstrip('/')}/workflow_status.json")
        except (OSError, WorkflowPathError, ValueError):
            status_path = None
        status = _load_project_json(status_path) if status_path is not None else {}
        model_progress = _mapping(status.get("progress"))
        if model_progress:
            progress.update(model_progress)
        if status.get("summary") and not progress.get("one_line"):
            progress["one_line"] = status.get("summary")
    return {str(key): value for key, value in progress.items() if value is not None and value != ""}


def _completion_freshness(project: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    marker_value = str(record.get("completion_marker") or "").strip()
    if not marker_value:
        runtime_dir = _workflow_default_path_value(record, "runtime_dir")
        marker_value = f"{runtime_dir.rstrip('/')}/plan_loop_complete.json" if runtime_dir else ""
    if not marker_value:
        return {"status": "unknown", "marker": None, "exists": False}
    try:
        marker_path = _resolve_project_relative_file(project, marker_value)
    except (OSError, WorkflowPathError, ValueError) as error:
        return {"status": "invalid_path", "marker": marker_value, "exists": False, "error": str(error)}
    result: dict[str, Any] = {
        "status": "missing",
        "marker": _safe_project_path(project, marker_path),
        "exists": marker_path.is_file(),
        "modified_at": _timestamp_for_path(marker_path) if marker_path.exists() else None,
    }
    if not marker_path.exists():
        return result
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("completion marker must be a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        result.update({"status": "invalid", "error": str(error)})
        return result
    result.update(
        {
            "status": "present",
            "workflow_id": payload.get("workflow_id"),
            "completed_at": payload.get("completed_at") or payload.get("created_at") or payload.get("generated_at"),
            "final_status": payload.get("status") or payload.get("final_status"),
            "freshness": "metadata_available",
        }
    )
    return result


def _git_checkpoint_status(project: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    read_models_dir = _workflow_default_path_value(record, "read_models_dir")
    if read_models_dir:
        try:
            vc_path = _resolve_project_relative_file(project, f"{read_models_dir.rstrip('/')}/version_control_status.json")
        except (OSError, WorkflowPathError, ValueError):
            vc_path = None
        if vc_path is not None and vc_path.is_file():
            try:
                payload = json.loads(vc_path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("version_control_status.json must be a JSON object")
            except (OSError, json.JSONDecodeError, ValueError) as error:
                return {
                    "status": "invalid",
                    "source": _safe_project_path(project, vc_path),
                    "error": str(error),
                }
            latest = _mapping(payload.get("latest_checkpoint"))
            repository = _mapping(payload.get("repository"))
            return {
                "status": payload.get("status") or "unknown",
                "ok": payload.get("ok"),
                "provider": payload.get("provider"),
                "git_available": payload.get("git_available"),
                "dirty": repository.get("dirty"),
                "dirty_files_count": repository.get("dirty_files_count"),
                "latest_checkpoint": _safe_checkpoint(latest) if latest else None,
                "generated_at": payload.get("generated_at"),
                "source": _safe_project_path(project, vc_path),
            }
    runtime_dir = _workflow_default_path_value(record, "runtime_dir")
    if not runtime_dir:
        return {"status": "unknown", "latest_checkpoint": None}
    try:
        checkpoint_log = _resolve_project_relative_file(project, f"{runtime_dir.rstrip('/')}/git_checkpoints.jsonl")
    except (OSError, WorkflowPathError, ValueError) as error:
        return {"status": "invalid_path", "latest_checkpoint": None, "error": str(error)}
    if not checkpoint_log.is_file():
        return {"status": "no_checkpoints", "latest_checkpoint": None, "source": _safe_project_path(project, checkpoint_log)}
    latest: Mapping[str, Any] | None = None
    for line in checkpoint_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            latest = payload
    return {
        "status": "checkpoint_log_present" if latest else "no_checkpoints",
        "latest_checkpoint": _safe_checkpoint(latest) if latest else None,
        "source": _safe_project_path(project, checkpoint_log),
    }


def _safe_checkpoint(record: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "checkpoint_id",
        "created_at",
        "reason",
        "status",
        "provider",
        "backend",
        "checkpoint_backend",
        "task_id",
        "run_id",
    )
    return {key: record.get(key) for key in allowed if record.get(key) is not None}


def _workflow_default_path_value(record: Mapping[str, Any], field: str) -> str:
    value = str(record.get(field) or "").strip()
    if value:
        return value
    try:
        workflow_root = _workflow_root_value(record)
        return str(default_workflow_path_values(workflow_root=workflow_root).get(field) or "")
    except (WorkflowPathError, ValueError):
        return ""


def _dashboard_progress_text(progress: Any) -> str:
    data = _mapping(progress)
    if not data:
        return ""
    completed = data.get("completed_tasks", data.get("tasks_completed"))
    total = data.get("total_tasks", data.get("tasks_total"))
    blocked = data.get("blocked_tasks", data.get("tasks_blocked"))
    percent = data.get("progress_percent")
    parts: list[str] = []
    if completed is not None or total is not None:
        parts.append(f"{completed or 0}/{total or 0}")
    if blocked not in {None, 0, "0"}:
        parts.append(f"blocked={blocked}")
    if percent is not None:
        parts.append(f"{percent}%")
    one_line = str(data.get("one_line") or "").strip()
    if one_line:
        parts.append(one_line)
    return " ".join(parts)


def _safe_project_path(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except (OSError, ValueError):
        return "[redacted path]"


def _safe_project_path_value(project: Path, value: str) -> str:
    try:
        path = Path(value)
        if path.is_absolute():
            return _safe_project_path(project, path)
        return _safe_project_path(project, project / path)
    except (OSError, ValueError):
        return "[redacted path]"


def _timestamp_for_path(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def _allowed_origin_netlocs(context: Mapping[str, Any]) -> set[str]:
    host = str(context.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(context.get("port") or 0)
    hosts = {_normalize_host(host)}
    if _normalize_host(host) in {"127.0.0.1", "localhost", "::1"}:
        hosts.update({"127.0.0.1", "localhost", "::1"})
    return {_normalize_netloc(_netloc_for_host(candidate, port)) for candidate in hosts if candidate and port}


def _normalize_host(value: str) -> str:
    host = value.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def _netloc_for_host(host: str, port: int) -> str:
    normalized = _normalize_host(host)
    if ":" in normalized and not normalized.startswith("["):
        return f"[{normalized}]:{port}"
    return f"{normalized}:{port}"


def _normalize_netloc(value: str | None) -> str:
    if not value:
        return ""
    netloc = str(value).strip().lower()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            return netloc.rstrip(".")
        host = netloc[: end + 1]
        rest = netloc[end + 1 :]
        port = rest[1:] if rest.startswith(":") else ""
        return f"{host.rstrip('.')}:{port}" if port else host.rstrip(".")
    host = netloc
    port = ""
    if ":" in netloc:
        maybe_host, maybe_port = netloc.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host
            port = maybe_port
    host = host.rstrip(".")
    return f"{host}:{port}" if port else host


def _dashboard_token_file(project: Path, paths: WorkflowPaths, security: Mapping[str, Any]) -> Path:
    dashboard = _mapping(security.get("dashboard"))
    raw_value = dashboard.get("token_file") or f"{paths.value('runtime_dir')}/dashboard_token"
    return _resolve_project_relative_file(project, _expand_dashboard_path_value(paths, str(raw_value)))


def _expand_dashboard_path_value(paths: WorkflowPaths, value: str) -> str:
    expanded = str(value)
    for key, replacement in paths.template_variables().items():
        expanded = expanded.replace("{{" + key + "}}", replacement)
        expanded = expanded.replace("{" + key + "}", replacement)
    return expanded


def _resolve_project_relative_file(project: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise WorkflowPathError("dashboard token_file must be project-relative")
    resolved = (project / path).resolve()
    try:
        resolved.relative_to(project)
    except ValueError as error:
        raise WorkflowPathError("dashboard token_file must stay inside the project root") from error
    return resolved


def _resolve_dashboard_project_file(context: Mapping[str, Any], value: str) -> Path | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    project = context["project"].resolve()
    path = Path(raw_value)
    if path.is_absolute():
        return None
    candidate = (project / path).resolve()
    try:
        candidate.relative_to(project)
    except ValueError:
        return None
    if ".git" in PurePosixPath(candidate.relative_to(project).as_posix()).parts:
        return None
    paths = context["paths"]
    allowed_roots = [
        paths.results_dir,
        paths.runtime_dir,
        paths.read_models_dir,
        paths.requests_dir,
        paths.planning_dir,
    ]
    for root in allowed_roots:
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except (OSError, ValueError):
            continue
    for file_path in (paths.brief_file, paths.plan_file, paths.shared_context_file):
        try:
            if candidate == file_path.resolve():
                return candidate
        except OSError:
            continue
    try:
        relative = PurePosixPath(candidate.relative_to(project).as_posix())
    except ValueError:
        return None
    if _is_safe_dashboard_workspace_file(relative):
        return candidate
    return None


def _is_safe_dashboard_workspace_file(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == ".loopplane":
        return False
    denied_names = {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".gnupg",
        ".env",
        ".env.local",
        ".envrc",
    }
    denied_parts = {
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
    }
    for part in parts:
        lowered = part.lower()
        if lowered in denied_names or lowered in denied_parts:
            return False
        if part.startswith("."):
            return False
    return True


def _first_query_value(query: Mapping[str, Sequence[str]], *keys: str) -> str:
    for key in keys:
        values = query.get(key)
        if not values:
            continue
        value = str(values[0] or "").strip()
        if value:
            return value
    return ""


def _load_or_create_dashboard_token(path: Path) -> str:
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def _workspace_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    workflows = _workflow_records(context)
    workspace = _workspace_metadata(project)
    current_workflow_id = _current_workflow_id(context)
    workspace.update(
        {
            "project_root": project.as_posix(),
            "workspace_files": _workspace_files(project),
            "current_workflow_id": current_workflow_id,
            "workflow_count": len(workflows),
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "workspace": workspace,
        "workspaces": [workspace],
        "workflows": workflows,
    }


def _workflow_list_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "current_workflow_id": _current_workflow_id(context),
        "workflows": _workflow_records(context),
    }


def _workspace_workflow_detail_response(context: Mapping[str, Any], resolved: Mapping[str, Any]) -> dict[str, Any]:
    workflow_context = _workflow_context(context, resolved)
    payload = _dashboard_data_response(workflow_context)
    payload["workflow"] = dict(_mapping(resolved.get("workflow")))
    payload["workspace_endpoint"] = f"/api/workspace/workflows/{workflow_context['workflow_id']}"
    payload["mutation_boundary"] = (
        "GET /api/workspace/workflows/<workflow_id> is visualization-only; it resolves the "
        "workflow through project-local .loopplane/workflow_registry.json and does not update "
        ".loopplane/current_workflow.json."
    )
    return payload


def _create_workspace_workflow(context: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    brief = str(body.get("brief") or body.get("project_brief") or body.get("text") or "").strip()
    if not brief:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "invalid_brief",
            "project_root": project.as_posix(),
            "workspace_id": _workspace_metadata(project).get("workspace_id"),
            "current_workflow_id": _current_workflow_id(context),
            "errors": ["brief must be a non-empty string."],
            "warnings": [],
            "mutation_boundary": (
                "POST /api/workspace/workflows delegates to the same safe workflow-create policy "
                "as loopplane workflow create; no workspace truth is changed for invalid requests."
            ),
        }
    result = dict(create_workflow(project, brief))
    result["workspace_api_endpoint"] = "/api/workspace/workflows"
    result["source"] = "dashboard_api"
    result["request"] = {
        "schema_version": SCHEMA_VERSION,
        "source": "dashboard_api",
        "requested_at": utc_timestamp(),
        "brief_sha256": sha256(brief.encode("utf-8")).hexdigest(),
        "body": _json_safe_object({key: value for key, value in body.items() if key != "brief"}),
    }
    if result.get("ok"):
        result["workflow_endpoint"] = f"/api/workspace/workflows/{result.get('workflow_id')}"
        result["effect"] = "workflow_history_created"
        result["mutation_boundary"] = (
            "POST /api/workspace/workflows is an explicit workflow-create request. It uses "
            "loopplane workflow create semantics, allocates a new workflow history only after "
            "policy checks pass, and updates .loopplane/current_workflow.json only as part of "
            "that explicit create operation."
        )
    else:
        result.setdefault(
            "mutation_boundary",
            (
                "POST /api/workspace/workflows delegates to the same safe workflow-create policy "
                "as loopplane workflow create; failed requests do not update workspace truth."
            ),
        )
    return result


def _workspace_create_http_status(result: Mapping[str, Any]) -> HTTPStatus:
    if result.get("ok"):
        return HTTPStatus.CREATED
    status = str(result.get("status") or "")
    if status in {"missing_project", "missing_workspace", "missing_registry"}:
        return HTTPStatus.NOT_FOUND
    if "conflict" in status or "blocked" in status or status in {"read_only_workflow", "archived_workflow"}:
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


def _dashboard_data_response(context: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    workflows = _workflow_records(context)
    workspace = _workspace_selection_metadata(context, workflows=workflows, selected_workflow_id=str(context["workflow_id"]))
    workflow = _workflow_record_for_id(workflows, str(context["workflow_id"]))
    models, freshness, rebuild_result = _load_live_read_models(
        project,
        paths,
        workflow_id=str(context["workflow_id"]),
        allow_rebuild=not _workflow_mutation_blockers(workflow),
        max_dashboard_events=_optional_positive_int(context.get("max_dashboard_events")),
        cache=_read_model_cache_from_context(context),
        include_legacy_run_summaries=False,
    )
    if models["errors"]:
        return _lazy_file_content_payload({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "missing_read_models",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "read_models_dir": _project_relative(project, paths.read_models_dir),
            "workspace": workspace,
            "workflows": workflows,
            "runner_configuration": _runner_configuration_payload(context),
            "planning_controls": _planning_controls_payload(
                context,
                workflow=_workflow_record_for_id(workflows, str(context["workflow_id"])),
            ),
            "execution_controls": _execution_controls_payload(
                context,
                workflow=_workflow_record_for_id(workflows, str(context["workflow_id"])),
            ),
            "approval_controls": _approval_controls_payload(
                context,
                workflow=_workflow_record_for_id(workflows, str(context["workflow_id"])),
            ),
            "inspector_console": _inspector_console_payload(
                context,
                workflow=_workflow_record_for_id(workflows, str(context["workflow_id"])),
            ),
            "read_models": {},
            "jsonl_models": {},
            "plan_markdown": _plan_markdown_payload(project, paths, None),
            "node_details": {},
            "read_model_freshness": {},
            "read_model_rebuild": {},
            "read_model_diagnostics": dict(models.get("diagnostics") or {}),
            "errors": models["errors"],
            "warnings": _read_model_rebuild_result_warnings(rebuild_result),
            "rebuild_read_models": dict(rebuild_result) if isinstance(rebuild_result, Mapping) else None,
        })
    warnings = [*_freshness_warning_messages(freshness), *_read_model_rebuild_result_warnings(rebuild_result)]
    dashboard_event_limit = _dashboard_payload_event_limit(
        paths,
        explicit_limit=_optional_positive_int(context.get("max_dashboard_events")),
        rebuild_result=rebuild_result,
    )
    return _lazy_file_content_payload({
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "server_mode": True,
        "rendered_at": utc_timestamp(),
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "max_dashboard_events": dashboard_event_limit,
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "read_model_files": list(READ_MODEL_FILES),
        "read_models": models["json"],
        "jsonl_models": models["jsonl"],
        "plan_markdown": _plan_markdown_payload(project, paths, models["json"].get("plan_index.json")),
        "node_details": _node_details_payload(context, models),
        "read_model_freshness": freshness,
        "read_model_rebuild": _read_model_rebuild_payload(
            context,
            workflow=workflow,
            freshness=freshness,
        ),
        "read_model_diagnostics": dict(models.get("diagnostics") or {}),
        "rebuild_read_models": dict(rebuild_result) if isinstance(rebuild_result, Mapping) else None,
        "workspace": workspace,
        "workflows": workflows,
        "runner_configuration": _runner_configuration_payload(context),
        "planning_controls": _planning_controls_payload(
            context,
            workflow=workflow,
        ),
        "execution_controls": _execution_controls_payload(
            context,
            workflow=workflow,
        ),
        "approval_controls": _approval_controls_payload(
            context,
            workflow=workflow,
        ),
        "inspector_console": _inspector_console_payload(
            context,
            workflow=workflow,
        ),
        "errors": [],
        "warnings": warnings,
    })


def _dashboard_data_etag(context: Mapping[str, Any]) -> str:
    project = context["project"]
    paths = context["paths"]
    fingerprint = {
        "schema_version": SCHEMA_VERSION,
        "resource": "dashboard-data",
        "workflow_id": str(context.get("workflow_id") or ""),
        "max_dashboard_events": _optional_positive_int(context.get("max_dashboard_events")),
        "workflow_paths": dict(paths.values),
        "workspace": [
            _path_stat_fingerprint(project, project / ".loopplane" / "workspace.json"),
            _path_stat_fingerprint(project, project / ".loopplane" / "workflow_registry.json"),
            _path_stat_fingerprint(project, project / ".loopplane" / "current_workflow.json"),
        ],
        "configuration": [
            _path_stat_fingerprint(project, paths.workflow_config_file),
            _path_stat_fingerprint(project, paths.config_file("security.json")),
            _path_stat_fingerprint(project, paths.config_file("dashboard.json")),
            _path_stat_fingerprint(project, paths.config_file("agent_runners.json")),
            _path_stat_fingerprint(project, paths.version_control_config_file),
            _path_stat_fingerprint(project, project_local_agent_runner_override_file(project)),
            _path_stat_fingerprint(project, loopplane_home_layout().agent_runners_local_file),
        ],
        "read_models": [
            _path_stat_fingerprint(project, paths.read_models_dir / filename)
            for filename in _dashboard_data_read_model_filenames(paths)
        ],
        "plan_markdown": [
            _path_stat_fingerprint(project, path)
            for path in _dashboard_plan_markdown_fingerprint_paths(paths)
        ],
        "runtime": [
            _path_stat_fingerprint(project, paths.runtime_dir / filename)
            for filename in DASHBOARD_ETAG_RUNTIME_FILES
        ],
        "requests": [
            _path_stat_fingerprint(project, paths.requests_dir / filename)
            for filename in DASHBOARD_ETAG_REQUEST_FILES
        ],
        "event_log": _dashboard_event_log_fingerprint(project, paths),
        "active_run_leases": _active_run_leases_etag_fingerprint(project, paths),
    }
    digest = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f'"dashboard-{digest[:32]}"'


def _dashboard_elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _dashboard_data_read_model_filenames(paths: WorkflowPaths) -> list[str]:
    split_run_models_available = (paths.read_models_dir / "run_index.jsonl").is_file()
    filenames: list[str] = []
    for filename in DASHBOARD_LIVE_READ_MODEL_FILES:
        if filename == "run_summaries.jsonl" and split_run_models_available:
            continue
        filenames.append(filename)
    return filenames


def _dashboard_plan_markdown_candidate_paths(project: Path, paths: WorkflowPaths) -> list[Path]:
    candidates = [paths.plan_file]
    plan_index = _read_optional_json(paths.read_models_dir / "plan_index.json")
    plan_file_value = str(_mapping(plan_index).get("plan_file") or "").strip()
    if plan_file_value:
        candidates.append(_resolve_dashboard_record_path(project, plan_file_value))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _dashboard_plan_markdown_fingerprint_paths(paths: WorkflowPaths) -> list[Path]:
    return [paths.plan_file, paths.planning_dir / "PLAN_DRAFT.md"]


def _dashboard_event_log_fingerprint(project: Path, paths: WorkflowPaths) -> dict[str, Any]:
    events_dir = paths.runtime_dir / "events"
    manifest_path = events_dir / EVENTS_MANIFEST_FILENAME
    fingerprint: dict[str, Any] = {
        "manifest": _path_stat_fingerprint(project, manifest_path),
    }
    if not manifest_path.is_file():
        fingerprint["segments"] = _glob_file_stat_fingerprints(project, events_dir, "*.jsonl")
    return fingerprint


def _active_run_leases_etag_fingerprint(project: Path, paths: WorkflowPaths) -> dict[str, Any]:
    lease_dir = paths.runtime_dir / "active_run_leases"
    stamp_path = paths.runtime_dir / ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME
    directory = _path_stat_fingerprint(project, lease_dir)
    stamp = _path_stat_fingerprint(project, stamp_path)
    if stamp.get("exists"):
        return {
            "mode": "stamp",
            "directory": directory,
            "stamp": stamp,
        }
    return {
        "mode": "bounded_legacy_directory",
        "directory": directory,
        "legacy_files": _bounded_directory_name_fingerprints(project, lease_dir, "*.json", limit=32),
    }


def _path_stat_fingerprint(project: Path, path: Path) -> dict[str, Any]:
    relative = _project_relative(project, path)
    try:
        stat_result = path.stat()
    except OSError as error:
        return {
            "path": relative,
            "exists": False,
            "error": error.__class__.__name__,
        }
    return {
        "path": relative,
        "exists": True,
        "is_file": path.is_file(),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
    }


def _glob_file_stat_fingerprints(project: Path, directory: Path, pattern: str) -> dict[str, Any]:
    directory_ref = _path_stat_fingerprint(project, directory)
    try:
        files = sorted(path for path in directory.glob(pattern) if path.is_file())
    except OSError as error:
        return {
            "directory": directory_ref,
            "files": [],
            "error": error.__class__.__name__,
        }
    return {
        "directory": directory_ref,
        "files": [_path_stat_fingerprint(project, path) for path in files],
    }


def _bounded_directory_name_fingerprints(project: Path, directory: Path, pattern: str, *, limit: int) -> dict[str, Any]:
    directory_ref = _path_stat_fingerprint(project, directory)
    try:
        with os.scandir(directory) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if fnmatch.fnmatch(entry.name, pattern) and entry.is_file(follow_symlinks=False)
            )
    except OSError as error:
        return {
            "directory": directory_ref,
            "file_count": 0,
            "sample_limit": max(0, int(limit)),
            "sample_names": [],
            "truncated": False,
            "error": error.__class__.__name__,
        }
    sample_limit = max(0, int(limit))
    if sample_limit and len(names) > sample_limit:
        head_count = sample_limit // 2
        tail_count = sample_limit - head_count
        sample_names = [*names[:head_count], *names[-tail_count:]]
    else:
        sample_names = names
    return {
        "directory": directory_ref,
        "file_count": len(names),
        "sample_limit": sample_limit,
        "sample_names": sample_names,
        "truncated": len(names) > len(sample_names),
    }


def _request_etag_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    for raw_token in header_value.split(","):
        token = raw_token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag:
            return True
    return False


def _runner_configuration_response(context: Mapping[str, Any]) -> dict[str, Any]:
    payload = _runner_configuration_payload(context)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": payload.get("ok") is not False,
        "status": payload.get("status") or "ok",
        "workflow_id": context["workflow_id"],
        "runner_configuration": payload,
        "errors": list(payload.get("errors") or []),
        "warnings": list(payload.get("warnings") or []),
    }


def _runner_configuration_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    security = _load_project_json(paths.config_file("security.json"))
    trusted_local = _dashboard_trusted_local_enabled(security)
    config_path = paths.config_file("agent_runners.json")
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "trusted_local" if trusted_local else "read_only",
        "trusted_local_mode": trusted_local,
        "configuration_requests_allowed": trusted_local,
        "request_endpoint": f"/api/workflows/{context['workflow_id']}/runners/configuration-requests",
        "config_path": _project_relative(project, config_path),
        "default_runner": None,
        "runner_count": 0,
        "runners": [],
        "errors": [],
        "warnings": [],
    }
    try:
        config = load_agent_runners(project)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError, WorkflowPathError) as error:
        base.update(
            {
                "ok": False,
                "status": "unavailable",
                "errors": [str(error)],
                "warnings": ["Runner configuration could not be loaded."],
            }
        )
        return base

    runners = [
        _runner_configuration_summary(context, runner, trusted_local=trusted_local)
        for runner in sorted(
            config.runners.values(),
            key=lambda item: _runner_configuration_sort_key(item, config.default_runner),
        )
    ]
    base.update(
        {
            "default_runner": config.default_runner,
            "runner_count": len(runners),
            "runners": runners,
        }
    )
    if not trusted_local:
        base["warnings"] = [
            "Trusted local mode is disabled; runner commands are hidden and browser configuration changes are unavailable."
        ]
    return base


def _runner_configuration_sort_key(runner: RunnerConfig, default_runner: str) -> tuple[int, str]:
    return (0 if runner.runner_id == default_runner else 1, runner.runner_id)


def _dashboard_trusted_local_enabled(security: Mapping[str, Any]) -> bool:
    dashboard = _mapping(security.get("dashboard"))
    return any(
        dashboard.get(key) is True
        for key in (
            "trusted_local_mode",
            "trusted_local",
            "allow_runner_configuration",
            "allow_runner_configuration_requests",
        )
    )


def _runner_configuration_summary(
    context: Mapping[str, Any],
    runner: RunnerConfig,
    *,
    trusted_local: bool,
) -> dict[str, Any]:
    prompt_delivery = _mapping(runner.prompt_delivery)
    adapter_options = _runner_dashboard_adapter_options(runner.adapter_options)
    return {
        "runner_id": runner.runner_id,
        "role": runner.role,
        "adapter": runner.adapter,
        "enabled": runner.enabled,
        "command": _dashboard_runner_command_label(context, runner.command, trusted_local=trusted_local),
        "command_hidden": not trusted_local,
        "prompt_delivery_mode": _text(prompt_delivery.get("mode") or "unknown"),
        "timeout_seconds": runner.timeout_seconds,
        "model": adapter_options.get("model"),
        "reasoning_effort": adapter_options.get("reasoning_effort"),
        "adapter_options": adapter_options,
        "doctor": _runner_doctor_summary(runner),
    }


def _runner_dashboard_adapter_options(adapter_options: Mapping[str, Any]) -> dict[str, Any]:
    model = _first_text(adapter_options, ("model", "codex_model", "claude_model"))
    effort = _first_text(
        adapter_options,
        ("reasoning_effort", "model_reasoning_effort", "codex_reasoning_effort", "thinking_effort", "effort"),
    )
    summary: dict[str, Any] = {
        "model": model or None,
        "reasoning_effort": effort or None,
    }
    sandbox = _first_text(adapter_options, ("codex_sandbox",))
    if sandbox:
        summary["codex_sandbox"] = sandbox
    return summary


def _first_text(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _dashboard_runner_command_label(context: Mapping[str, Any], command: str, *, trusted_local: bool) -> str:
    if not trusted_local:
        return "[hidden until trusted local mode]"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "[unparseable command hidden]"
    if not parts:
        return "none"
    program = parts[0]
    display_program = _redacted_program_label(program)
    if len(parts) > 1:
        display_program = f"{display_program} [arguments hidden]"
    return _redact_dashboard_text(_mapping(context.get("redaction")), display_program)


def _redacted_program_label(program: str) -> str:
    if "/" not in program and "\\" not in program:
        return program
    name = re.split(r"[\\/]+", program.rstrip("/\\"))[-1] or "command"
    return f"[local path redacted]/{name}"


def _runner_doctor_summary(runner: RunnerConfig) -> dict[str, Any]:
    doctor = _mapping(runner.doctor)
    diagnostics: list[str] = []
    if doctor.get("check_command"):
        diagnostics.append("version check configured")
    else:
        diagnostics.append("version check missing")
    if doctor.get("requires_auth") is True:
        diagnostics.append("authentication check may be required")
    if not runner.enabled:
        diagnostics.append("runner is disabled")
    diagnostics.append(f"run: loopplane doctor-agent --runner {runner.runner_id}")
    return {
        "status": "disabled" if not runner.enabled else "not_run",
        "requires_auth": doctor.get("requires_auth") is True,
        "check_configured": bool(doctor.get("check_command")),
        "diagnostics": diagnostics,
    }


def _record_runner_configuration_request(
    context: Mapping[str, Any],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    security = _load_project_json(context["paths"].config_file("security.json"))
    if not _dashboard_trusted_local_enabled(security):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "trusted_local_required",
            "workflow_id": context["workflow_id"],
            "errors": ["Runner configuration requests require dashboard trusted_local_mode."],
            "warnings": [],
        }
    prepared = _prepare_runner_configuration_request(body)
    if not prepared.get("ok"):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": prepared.get("status") or "invalid_runner_configuration_request",
            "workflow_id": context["workflow_id"],
            "errors": list(prepared.get("errors") or ["Invalid runner configuration request."]),
            "warnings": [],
        }
    apply_result = _apply_runner_configuration_request(context, prepared["requested_configuration"])
    if not apply_result.get("ok"):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": apply_result.get("status") or "runner_configuration_apply_failed",
            "workflow_id": context["workflow_id"],
            "errors": list(apply_result.get("errors") or ["Unable to apply runner configuration."]),
            "warnings": list(apply_result.get("warnings") or []),
        }
    payload = {
        "requested_configuration": prepared["requested_configuration"],
        "safe_command_path": prepared.get("safe_command_path"),
        "applied_configuration": apply_result.get("applied_configuration"),
        "mutation_policy": "applied_project_local_override",
        "trusted_local_mode": True,
        "local_override_path": apply_result.get("local_override_path"),
    }
    result = _record_dashboard_request(context, "runner_configuration", payload)
    result["runner_configuration_applied"] = True
    result["applied_configuration"] = apply_result.get("applied_configuration")
    if apply_result.get("local_override_path"):
        result.setdefault("files_written", []).append(str(apply_result["local_override_path"]))
    return result


def _prepare_runner_configuration_request(body: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = sorted(str(key) for key in body if str(key) in RUNNER_CONFIGURATION_FORBIDDEN_FIELDS)
    if forbidden:
        return {
            "ok": False,
            "status": "forbidden_runner_configuration_field",
            "errors": [f"Browser runner configuration requests cannot include: {', '.join(forbidden)}."],
        }
    unknown = sorted(str(key) for key in body if str(key) not in RUNNER_CONFIGURATION_REQUEST_FIELDS)
    if unknown:
        return {
            "ok": False,
            "status": "unknown_runner_configuration_field",
            "errors": [f"Unknown runner configuration field(s): {', '.join(unknown)}."],
        }
    runner_id = str(body.get("runner_id") or "").strip()
    if not runner_id:
        return {"ok": False, "status": "missing_runner_id", "errors": ["runner_id is required."]}
    requested: dict[str, Any] = {"runner_id": runner_id}
    for field in ("role", "adapter", "reason"):
        value = str(body.get(field) or "").strip()
        if value:
            requested[field] = value
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return {"ok": False, "status": "invalid_enabled", "errors": ["enabled must be boolean."]}
        requested["enabled"] = body["enabled"]
    if body.get("prompt_delivery_mode") is not None:
        mode = str(body.get("prompt_delivery_mode") or "").strip()
        if mode not in PROMPT_DELIVERY_MODES:
            return {
                "ok": False,
                "status": "invalid_prompt_delivery_mode",
                "errors": [f"prompt_delivery_mode must be one of: {', '.join(sorted(PROMPT_DELIVERY_MODES))}."],
            }
        requested["prompt_delivery_mode"] = mode
    if body.get("timeout_seconds") is not None:
        try:
            timeout = int(body.get("timeout_seconds"))
        except (TypeError, ValueError):
            return {"ok": False, "status": "invalid_timeout", "errors": ["timeout_seconds must be an integer."]}
        if timeout <= 0:
            return {"ok": False, "status": "invalid_timeout", "errors": ["timeout_seconds must be positive."]}
        requested["timeout_seconds"] = timeout
    adapter_options_result = _prepare_runner_adapter_options(body)
    if not adapter_options_result.get("ok"):
        return adapter_options_result
    if adapter_options_result.get("adapter_options"):
        requested["adapter_options"] = adapter_options_result["adapter_options"]
    if body.get("command") is not None:
        command_text = str(body.get("command") or "").strip()
        if command_text:
            command_result = _safe_runner_command_request(command_text)
            if not command_result.get("ok"):
                return command_result
            requested["command"] = command_result["command"]
    if set(requested) == {"runner_id"}:
        return {
            "ok": False,
            "status": "empty_runner_configuration_request",
            "errors": ["At least one runner configuration field is required."],
        }
    safe_command = _runner_configuration_cli_equivalent(requested)
    return {
        "ok": True,
        "requested_configuration": requested,
        "safe_command_path": safe_command,
    }


def _prepare_runner_adapter_options(body: Mapping[str, Any]) -> dict[str, Any]:
    adapter_options: dict[str, Any] = {}
    raw_adapter_options = body.get("adapter_options")
    if raw_adapter_options is not None:
        if not isinstance(raw_adapter_options, Mapping):
            return {"ok": False, "status": "invalid_adapter_options", "errors": ["adapter_options must be an object."]}
        for key, value in raw_adapter_options.items():
            text_key = str(key)
            if text_key not in {"model", "reasoning_effort", "effort"}:
                return {
                    "ok": False,
                    "status": "invalid_adapter_options",
                    "errors": [f"adapter_options.{text_key} cannot be changed from the dashboard."],
                }
            text = str(value or "").strip()
            if text:
                adapter_options["reasoning_effort" if text_key == "effort" else text_key] = text
    for field in ("model", "reasoning_effort"):
        if body.get(field) is None:
            continue
        text = str(body.get(field) or "").strip()
        if text:
            adapter_options[field] = text
    effort = adapter_options.get("reasoning_effort")
    if effort is not None and effort not in RUNNER_REASONING_EFFORT_VALUES:
        return {
            "ok": False,
            "status": "invalid_reasoning_effort",
            "errors": [f"reasoning_effort must be one of: {', '.join(sorted(RUNNER_REASONING_EFFORT_VALUES))}."],
        }
    return {"ok": True, "adapter_options": adapter_options}


def _apply_runner_configuration_request(context: Mapping[str, Any], requested: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    runner_id = str(requested.get("runner_id") or "")
    try:
        config = load_agent_runners(project)
        runner = config.runner(runner_id)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "ok": False,
            "status": "runner_configuration_unavailable",
            "errors": [str(error)],
            "warnings": [],
        }
    override: dict[str, Any] = {}
    for field in ("role", "adapter", "command", "enabled", "timeout_seconds"):
        if field in requested:
            override[field] = requested[field]
    if requested.get("prompt_delivery_mode"):
        prompt_delivery = dict(runner.prompt_delivery)
        prompt_delivery["mode"] = str(requested["prompt_delivery_mode"])
        override["prompt_delivery"] = prompt_delivery
    if isinstance(requested.get("adapter_options"), Mapping):
        adapter_options = dict(runner.adapter_options)
        adapter_options.update(dict(_mapping(requested["adapter_options"])))
        override["adapter_options"] = adapter_options
    if not override:
        return {"ok": False, "status": "empty_runner_configuration_request", "errors": ["No runner fields to apply."]}
    try:
        merged_override = _project_local_runner_override(project, runner_id, override)
        write_result = write_project_local_agent_runner_overrides(project, {runner_id: merged_override})
        reloaded = load_agent_runners(project)
        updated = reloaded.runner(runner_id)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "ok": False,
            "status": "runner_configuration_apply_failed",
            "errors": [str(error)],
            "warnings": [],
        }
    return {
        "ok": True,
        "status": "applied",
        "runner_id": runner_id,
        "local_override_path": _project_relative(project, Path(write_result["path"])),
        "applied_configuration": _runner_configuration_summary(context, updated, trusted_local=True),
        "warnings": [],
    }


def _project_local_runner_override(project: Path, runner_id: str, override: Mapping[str, Any]) -> dict[str, Any]:
    path = project_local_agent_runner_override_file(project)
    existing: dict[str, Any] = {}
    if path.exists():
        payload = _load_project_json(path)
        runners = payload.get("runners")
        if isinstance(runners, Mapping) and isinstance(runners.get(runner_id), Mapping):
            existing = dict(_mapping(runners[runner_id]))
    return _deep_merge_mapping(existing, override)


def _deep_merge_mapping(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(parent)
    for key, value in child.items():
        parent_value = merged.get(key)
        if isinstance(parent_value, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge_mapping(parent_value, value)
        else:
            merged[key] = value
    return merged


def _safe_runner_command_request(command: str) -> dict[str, Any]:
    value = command.strip()
    if not value:
        return {"ok": False, "status": "invalid_command", "errors": ["command must be non-empty."]}
    if any(token in value for token in RUNNER_COMMAND_DENIED_TOKENS):
        return {
            "ok": False,
            "status": "unsafe_command",
            "errors": ["command must be a simple executable name, not a shell expression."],
        }
    try:
        parts = shlex.split(value)
    except ValueError as error:
        return {"ok": False, "status": "invalid_command", "errors": [f"command cannot be parsed: {error}."]}
    if len(parts) != 1:
        return {
            "ok": False,
            "status": "unsafe_command",
            "errors": ["command must be a single executable name; configure arguments outside the dashboard."],
        }
    program = parts[0]
    if "/" in program or "\\" in program:
        return {
            "ok": False,
            "status": "local_path_command_rejected",
            "errors": ["absolute or relative command paths must be configured through the local CLI, not the browser."],
        }
    return {"ok": True, "command": program}


def _runner_configuration_cli_equivalent(requested: Mapping[str, Any]) -> list[str] | None:
    if not all(requested.get(field) for field in ("runner_id", "role", "adapter", "command")):
        return None
    return [
        "loopplane",
        "configure-agent",
        "--runner",
        str(requested["runner_id"]),
        "--role",
        str(requested["role"]),
        "--adapter",
        str(requested["adapter"]),
        "--command",
        str(requested["command"]),
    ]


def _workspace_selection_metadata(
    context: Mapping[str, Any],
    *,
    workflows: Sequence[Mapping[str, Any]],
    selected_workflow_id: str,
) -> dict[str, Any]:
    project = context["project"]
    workspace = _workspace_metadata(project)
    current_workflow_id = _current_workflow_id(context)
    workspace.update(
        {
            "project_root": project.as_posix(),
            "workspace_files": _workspace_files(project),
            "current_workflow_id": current_workflow_id,
            "selected_workflow_id": selected_workflow_id,
            "workflow_count": len(workflows),
        }
    )
    return workspace


def _workflow_snapshots(
    context: Mapping[str, Any],
    workflows: Sequence[Mapping[str, Any]],
    *,
    read_model_cache: "_ReadModelCache | None" = None,
) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    for record in workflows:
        workflow_id = str(record.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        resolved = _resolve_workflow(context, workflow_id)
        if not resolved.get("ok"):
            snapshots[workflow_id] = {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "status": resolved.get("status") or "workflow_unavailable",
                "workflow_id": workflow_id,
                "workflow": dict(record),
                "errors": list(resolved.get("errors") or ["workflow could not be resolved"]),
                "warnings": [],
            }
            continue
        workflow_context = _workflow_context(context, resolved)
        snapshot = _workflow_snapshot(workflow_context, workflow=dict(record), read_model_cache=read_model_cache)
        snapshots[workflow_id] = snapshot
    return snapshots


def _workflow_snapshot(
    context: Mapping[str, Any],
    *,
    workflow: Mapping[str, Any] | None = None,
    read_model_cache: "_ReadModelCache | None" = None,
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    models = _load_read_models(
        paths.read_models_dir,
        cache=read_model_cache,
        cache_scope=_read_model_cache_scope(project, paths, str(context["workflow_id"])),
    )
    if models["errors"]:
        workflow_title = _workflow_display_title(str(context["workflow_id"]), workflow=workflow or {}, read_models={})
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "missing_read_models",
            "workflow_id": context["workflow_id"],
            "workflow_title": workflow_title,
            "workflow": dict(workflow or {}),
            "runner_configuration": _runner_configuration_payload(context),
            "planning_controls": _planning_controls_payload(context, workflow=workflow),
            "execution_controls": _execution_controls_payload(context, workflow=workflow),
            "approval_controls": _approval_controls_payload(context, workflow=workflow),
            "inspector_console": _inspector_console_payload(context, workflow=workflow),
            "read_models_dir": _project_relative(project, paths.read_models_dir),
            "read_models": {},
            "jsonl_models": {},
            "plan_markdown": _plan_markdown_payload(project, paths, None),
            "node_details": {},
            "read_model_freshness": {},
            "read_model_rebuild": {},
            "errors": models["errors"],
            "warnings": [],
        }
    freshness = _read_model_freshness(project, paths, models["json"])
    workflow_title = _workflow_display_title(str(context["workflow_id"]), workflow=workflow or {}, read_models=models["json"])
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "workflow_id": context["workflow_id"],
        "workflow_title": workflow_title,
        "workflow": dict(workflow or {}),
        "runner_configuration": _runner_configuration_payload(context),
        "planning_controls": _planning_controls_payload(context, workflow=workflow),
        "execution_controls": _execution_controls_payload(context, workflow=workflow),
        "approval_controls": _approval_controls_payload(context, workflow=workflow),
        "inspector_console": _inspector_console_payload(context, workflow=workflow),
        "read_models_dir": _project_relative(project, paths.read_models_dir),
        "read_models": models["json"],
        "jsonl_models": models["jsonl"],
        "plan_markdown": _plan_markdown_payload(project, paths, models["json"].get("plan_index.json")),
        "node_details": _node_details_payload(context, models, include_split_run_details=True),
        "read_model_freshness": freshness,
        "read_model_rebuild": _read_model_rebuild_payload(context, workflow=workflow, freshness=freshness),
        "errors": [],
        "warnings": _freshness_warning_messages(freshness),
    }


def _workflow_summaries(context: Mapping[str, Any], workflows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    project = context["project"]
    summaries: dict[str, Any] = {}
    for record in workflows:
        workflow = _mapping(record)
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        summary = _mapping(workflow.get("summary"))
        summaries[workflow_id] = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "name": workflow.get("name"),
            "status": workflow.get("status") or "unknown",
            "archived": workflow.get("archived") is True,
            "read_only": workflow.get("read_only") is True,
            "created_at": workflow.get("created_at"),
            "updated_at": workflow.get("updated_at"),
            "last_seen_at": workflow.get("last_seen_at"),
            "read_models_available": _dashboard_workflow_has_read_models(project, workflow),
            "summary": dict(summary) if summary else {},
        }
    return summaries


def _workspace_metadata(project: Path) -> dict[str, Any]:
    workspace = _load_project_json(project / ".loopplane" / "workspace.json")
    if not workspace:
        workspace = {
            "workspace_id": f"ws_{sha256(project.as_posix().encode('utf-8')).hexdigest()[:16]}",
            "name": project.name or "workspace",
        }
    return dict(workspace)


def _current_workflow_id(context: Mapping[str, Any]) -> str:
    current = _load_project_json(context["project"] / ".loopplane" / "current_workflow.json")
    return str(current.get("current_workflow_id") or context["workflow_id"])


def _workspace_files(project: Path) -> dict[str, Any]:
    registry = project / ".loopplane" / "workflow_registry.json"
    current = project / ".loopplane" / "current_workflow.json"
    return {
        "workflow_registry": _project_relative(project, registry) if registry.exists() else None,
        "current_workflow": _project_relative(project, current) if current.exists() else None,
    }


def _workflow_records(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    project = context["project"]
    registry_path = project / ".loopplane" / "workflow_registry.json"
    records: list[dict[str, Any]] = []
    if registry_path.is_file():
        registry = _load_project_json(registry_path)
        workflows = registry.get("workflows")
        if isinstance(workflows, Sequence) and not isinstance(workflows, (str, bytes)):
            records = [dict(record) for record in workflows if isinstance(record, Mapping)]
    if records:
        return records
    status = _read_optional_json(context["paths"].read_models_dir / "workflow_status.json")
    progress = _mapping(status.get("progress"))
    return [
        {
            "workflow_id": context["workflow_id"],
            "name": "current workflow",
            "status": status.get("status") or "unknown",
            "workflow_root": ".loopplane",
            "plan_file": context["paths"].value("plan_file"),
            "read_models_dir": context["paths"].value("read_models_dir"),
            "completion_marker": f"{context['paths'].value('runtime_dir')}/plan_loop_complete.json",
            "read_only": False,
            "archived": False,
            "summary": {
                "one_line": status.get("summary") or "Current LoopPlane workflow.",
                "tasks_total": progress.get("total_tasks", 0),
                "tasks_completed": progress.get("completed_tasks", 0),
                "tasks_blocked": progress.get("blocked_tasks", 0),
            },
        }
    ]


def _workflow_record_for_id(workflows: Sequence[Mapping[str, Any]], workflow_id: str) -> Mapping[str, Any]:
    selected = str(workflow_id or "")
    for record in workflows:
        if str(_mapping(record).get("workflow_id") or "") == selected:
            return _mapping(record)
    return {}


def _workflow_display_title(
    workflow_id: str,
    *,
    workflow: Mapping[str, Any] | None,
    read_models: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> str:
    snapshot_map = _mapping(snapshot)
    workflow_map = _mapping(workflow)
    plan_index = _mapping(read_models.get("plan_index.json"))
    workflow_status = _mapping(read_models.get("workflow_status.json"))
    candidates = (
        snapshot_map.get("workflow_title"),
        plan_index.get("workflow_title"),
        workflow_status.get("workflow_title"),
        workflow_map.get("workflow_title"),
        workflow_map.get("name"),
    )
    for candidate in candidates:
        title = re.sub(r"\s+", " ", _text(candidate)).strip()
        if title and title != "none" and title != workflow_id:
            return title[:120]
    return "Workflow"


def _resolve_workflow(context: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
    selected = str(workflow_id or "")
    if selected in {"current", "default"}:
        selected = _current_workflow_id(context)
    for record in _workflow_records(context):
        if str(record.get("workflow_id") or "") != selected:
            continue
        try:
            workflow_config = _workflow_config_for_record(context, record, selected)
            paths = WorkflowPaths.from_config(context["project"], workflow_config)
        except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "status": "invalid_workflow_root",
                "workflow_id": selected,
                "errors": [str(error)],
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "ok",
            "workflow_id": selected,
            "workflow": record,
            "_paths": paths,
            "_workflow_config": workflow_config,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "workflow_not_found",
        "workflow_id": workflow_id,
        "errors": [f"workflow {workflow_id!r} is not in the workspace registry"],
    }


def _workflow_context(context: Mapping[str, Any], resolved: Mapping[str, Any]) -> dict[str, Any]:
    selected = dict(context)
    selected["workflow_id"] = str(resolved.get("workflow_id") or context["workflow_id"])
    selected["paths"] = resolved.get("_paths") if isinstance(resolved.get("_paths"), WorkflowPaths) else context["paths"]
    selected["workflow_config"] = dict(_mapping(resolved.get("_workflow_config")) or _mapping(context.get("workflow_config")))
    return selected


def _public_workflow_resolution(resolved: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in resolved.items() if not str(key).startswith("_")}


def _workflow_config_for_record(
    context: Mapping[str, Any],
    record: Mapping[str, Any],
    workflow_id: str,
) -> dict[str, Any]:
    workflow_root = _workflow_root_value(record)
    config_file = str(record.get("workflow_config_file") or f"{workflow_root}/config/workflow.json")
    config_path = _resolve_project_relative_file(context["project"], config_file)
    loaded = _load_project_json(config_path) if config_path.is_file() else {}
    defaults = _workflow_path_defaults_for_root(context, workflow_root)
    workflow_config = {**defaults, **loaded}
    workflow_config["workflow_id"] = workflow_id
    workflow_config["workflow_root"] = workflow_root
    for field in (
        "brief_file",
        "plan_file",
        "shared_context_file",
        "results_dir",
        "runtime_dir",
        "read_models_dir",
        "requests_dir",
        "planning_dir",
        "version_control_config_file",
    ):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            workflow_config[field] = value
    return workflow_config


def _workflow_root_value(record: Mapping[str, Any]) -> str:
    raw = str(record.get("workflow_root") or ".loopplane").strip().rstrip("/") or ".loopplane"
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise WorkflowPathError(f"workflow_root must stay inside the project root: {raw}")
    return path.as_posix()


def _workflow_path_defaults_for_root(context: Mapping[str, Any], workflow_root: str) -> dict[str, Any]:
    if workflow_root == ".loopplane":
        current = dict(_mapping(context.get("workflow_config")))
        current.setdefault("workflow_id", context["workflow_id"])
        return current
    root = workflow_root.rstrip("/")
    return {
        "brief_file": f"{root}/PROJECT_BRIEF.md",
        "plan_file": f"{root}/PLAN.md",
        "shared_context_file": f"{root}/SHARED_CONTEXT.md",
        "results_dir": f"{root}/results",
        "runtime_dir": f"{root}/runtime",
        "read_models_dir": f"{root}/read_models",
        "requests_dir": f"{root}/requests",
        "planning_dir": f"{root}/planning",
        "version_control_config_file": f"{root}/config/version_control.json",
    }


def _workflow_allows_mutation(resolved: Mapping[str, Any]) -> bool:
    workflow = _mapping(resolved.get("workflow"))
    return not _workflow_mutation_blockers(workflow)


def _workflow_mutation_blockers(workflow: Mapping[str, Any] | None) -> list[str]:
    record = _mapping(workflow)
    blockers: list[str] = []
    if record.get("read_only") is True:
        blockers.append("read_only")
    if record.get("archived") is True:
        blockers.append("archived")
    if record.get("immutable") is True:
        blockers.append("immutable")
    status = str(record.get("status") or "").strip().lower()
    if status in {"archived", "read_only_imported", "superseded"} and status not in blockers:
        blockers.append(status)
    if record.get("policy_eligible") is False or record.get("mutation_policy") in {"disabled", "read_only"}:
        blockers.append("policy_ineligible")
    return blockers


def _planning_controls_payload(context: Mapping[str, Any], *, workflow: Mapping[str, Any] | None) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    request_path = paths.requests_dir / "dashboard_requests.jsonl"
    records: list[dict[str, Any]] = []
    if request_path.is_file():
        records = [
            _planning_request_summary(record)
            for record in _read_jsonl(request_path)
            if _is_planning_dashboard_request(record, workflow_id)
        ]
    latest = sorted(records, key=lambda item: str(item.get("created_at") or ""))[-1] if records else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "available" if not blockers else "disabled",
        "workflow_id": workflow_id,
        "mutation_allowed": not blockers,
        "mutation_blockers": blockers,
        "request_record_only": True,
        "endpoints": {
            "plan": f"/api/workflows/{workflow_id}/plan",
            "audit": f"/api/workflows/{workflow_id}/audit",
            "activate_plan": f"/api/workflows/{workflow_id}/activate-plan",
        },
        "requests_path": _project_relative(project, request_path),
        "pending_count": sum(1 for record in records if str(record.get("status") or "pending") == "pending"),
        "recent": records[-8:],
        "latest_request_id": latest.get("request_id"),
        "latest_type": latest.get("type"),
        "latest_status": latest.get("status"),
        "commands": ["loopplane plan", "loopplane audit-plan", "loopplane activate-plan"],
        "warnings": _planning_control_warnings(blockers),
    }


def _is_planning_dashboard_request(record: Mapping[str, Any], workflow_id: str) -> bool:
    request_type = str(record.get("type") or "")
    if request_type not in {"plan", "audit", "activate_plan"}:
        return False
    record_workflow_id = str(record.get("workflow_id") or "")
    return not record_workflow_id or record_workflow_id == workflow_id


def _planning_request_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(record.get("payload"))
    return {
        "request_id": record.get("request_id"),
        "created_at": record.get("created_at"),
        "type": record.get("type"),
        "status": str(record.get("status") or "pending"),
        "runner_id": payload.get("runner_id"),
        "reason": payload.get("reason"),
        "source": record.get("source"),
    }


def _planning_control_warnings(blockers: Sequence[str]) -> list[str]:
    if not blockers:
        return []
    return [f"Planning controls are disabled because this workflow is {', '.join(blockers)}."]


def _execution_controls_payload(context: Mapping[str, Any], *, workflow: Mapping[str, Any] | None) -> dict[str, Any]:
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    status = _control_status_response(context)
    controls = [_control_request_summary(record) for record in _sequence(status.get("controls"))]
    latest = controls[-1] if controls else {}
    endpoint = f"/api/workflows/{workflow_id}/control-requests"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "available" if not blockers else "disabled",
        "workflow_id": workflow_id,
        "mutation_allowed": not blockers,
        "mutation_blockers": blockers,
        "request_record_only": True,
        "control_types": ["start", "pause", "resume", "stop"],
        "endpoints": {control_type: endpoint for control_type in ("start", "pause", "resume", "stop")},
        "requests_path": status.get("requests_path"),
        "responses_path": status.get("responses_path"),
        "runtime_status": status.get("runtime_status"),
        "scheduler": status.get("scheduler") if isinstance(status.get("scheduler"), Mapping) else {},
        "pending_count": status.get("pending_count", 0),
        "applied_count": status.get("applied_count", 0),
        "rejected_count": status.get("rejected_count", 0),
        "recent": controls[-8:],
        "latest_request_id": latest.get("request_id"),
        "latest_type": latest.get("type"),
        "latest_status": latest.get("status"),
        "commands": _execution_control_commands(),
        "warnings": _execution_control_warnings(blockers),
    }


def _control_request_summary(record: Any) -> dict[str, Any]:
    item = _mapping(record)
    return {
        "request_id": item.get("request_id"),
        "created_at": item.get("created_at"),
        "type": item.get("type"),
        "status": str(item.get("status") or "pending"),
        "source": item.get("source"),
        "handled_at": item.get("handled_at"),
        "resulting_workflow_status": item.get("resulting_workflow_status"),
    }


def _execution_control_commands() -> list[str]:
    return [
        "loopplane start --detach --project <project>",
        "loopplane pause --project <project>",
        "loopplane resume --project <project>",
        "loopplane stop --project <project>",
        "loopplane status --project <project>",
        "loopplane logs --project <project>",
        "loopplane attach --project <project>",
    ]


def _execution_control_warnings(blockers: Sequence[str]) -> list[str]:
    if not blockers:
        return []
    return [f"Execution controls are disabled because this workflow is {', '.join(blockers)}."]


def _read_model_rebuild_payload(
    context: Mapping[str, Any],
    *,
    workflow: Mapping[str, Any] | None,
    freshness: Mapping[str, Any],
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    status = _control_status_response(context)
    rebuild_records = [
        _control_request_summary(record)
        for record in _sequence(status.get("controls"))
        if _control_record_type(record) == "rebuild_read_models"
    ]
    latest = rebuild_records[-1] if rebuild_records else {}
    pending_count = sum(
        1
        for record in rebuild_records
        if _read_model_rebuild_status(record.get("status"), default="pending") == "pending"
    )
    rebuild_in_progress = _read_model_rebuild_in_progress(
        {
            "pending_count": pending_count,
            "latest_status": latest.get("status"),
            "recent": rebuild_records[-8:],
        }
    )
    endpoint = f"/api/workflows/{workflow_id}/rebuild-read-models"
    command = _text(freshness.get("rebuild_command") or "loopplane rebuild-read-models --project <project>")
    dashboard_command = _text(
        freshness.get("rebuild_dashboard_command") or "loopplane dashboard --project <project> --rebuild-read-models"
    )
    payload_status = "disabled" if blockers else ("rebuilding" if rebuild_in_progress else "available")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": payload_status,
        "workflow_id": workflow_id,
        "freshness_status": freshness.get("status") or "unknown",
        "freshness_summary": freshness.get("summary"),
        "mutation_allowed": not blockers,
        "request_allowed": not blockers and not rebuild_in_progress,
        "rebuild_in_progress": rebuild_in_progress,
        "in_progress_summary": (
            "A read-model rebuild request is already pending. The runtime will refresh dashboard data when it processes the request."
            if rebuild_in_progress
            else None
        ),
        "mutation_blockers": blockers,
        "request_record_only": True,
        "control_type": "rebuild_read_models",
        "endpoint": endpoint,
        "control_endpoint": f"/api/workflows/{workflow_id}/control-requests",
        "requests_path": status.get("requests_path") or _project_relative(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": status.get("responses_path") or _project_relative(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "pending_count": pending_count,
        "recent": rebuild_records[-8:],
        "latest_request_id": latest.get("request_id"),
        "latest_status": latest.get("status"),
        "commands": [command, dashboard_command],
        "warnings": _read_model_rebuild_warnings(blockers, freshness),
    }


def _read_model_rebuild_status(value: Any, *, default: str = "") -> str:
    return str(value or default).strip().lower().replace("-", "_")


def _read_model_rebuild_in_progress(read_model_rebuild: Mapping[str, Any]) -> bool:
    try:
        if int(read_model_rebuild.get("pending_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if _read_model_rebuild_status(read_model_rebuild.get("latest_status")) in READ_MODEL_REBUILD_ACTIVE_STATUSES:
        return True
    for raw_record in _sequence(read_model_rebuild.get("recent")):
        if _read_model_rebuild_status(_mapping(raw_record).get("status"), default="pending") in READ_MODEL_REBUILD_ACTIVE_STATUSES:
            return True
    return False


def _read_model_live_refresh_expected(
    freshness: Mapping[str, Any],
    read_model_rebuild: Mapping[str, Any],
    *,
    server_mode: bool,
    rebuild_in_progress: bool,
) -> bool:
    return False


def _control_record_type(record: Any) -> str:
    item = _mapping(record)
    return str(item.get("type") or item.get("action") or "").strip().lower().replace("-", "_")


def _read_model_rebuild_warnings(blockers: Sequence[str], freshness: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if blockers:
        warnings.append(f"Read-model rebuild requests are disabled because this workflow is {', '.join(blockers)}.")
    if freshness.get("status") == "unknown":
        warnings.append("Read-model freshness metadata is incomplete; rebuild the derived read models before relying on dashboard status.")
    return warnings


def _approval_controls_payload(context: Mapping[str, Any], *, workflow: Mapping[str, Any] | None) -> dict[str, Any]:
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    status = _approval_status_response(context, include_all=True)
    policy = _mapping(status.get("approval_policy"))
    mutation_blockers = list(blockers)
    if policy.get("enabled") is not True:
        mutation_blockers.append("approval_disabled")
    records = [_mapping(record) for record in _sequence(status.get("approvals"))]
    pending = [record for record in records if str(record.get("status") or "") == "pending"]
    recent = records[-8:]
    endpoint_base = f"/api/workflows/{workflow_id}/approvals"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "available" if not mutation_blockers else "disabled",
        "workflow_id": workflow_id,
        "mutation_allowed": not mutation_blockers,
        "mutation_blockers": mutation_blockers,
        "response_record_only": True,
        "approval_policy": policy,
        "requests_path": status.get("requests_path"),
        "responses_path": status.get("responses_path"),
        "list_endpoint": endpoint_base,
        "pending_count": len(pending),
        "approved_count": sum(1 for record in records if record.get("status") == "approved"),
        "rejected_count": sum(1 for record in records if record.get("status") == "rejected"),
        "expired_count": sum(1 for record in records if record.get("status") == "expired"),
        "superseded_count": sum(1 for record in records if record.get("status") == "superseded"),
        "pending": pending,
        "recent": recent,
        "approvals": records,
        "commands": _approval_commands(records),
        "warnings": _approval_control_warnings(mutation_blockers, policy),
    }


def _approval_commands(records: Sequence[Mapping[str, Any]]) -> list[str]:
    commands = ["loopplane approvals --project <project>"]
    for record in records:
        if str(record.get("status") or "") != "pending":
            continue
        approval_id = _text(record.get("approval_id"))
        if not approval_id:
            continue
        commands.extend(
            [
                f"loopplane approve {approval_id} --project <project>",
                f"loopplane reject {approval_id} --project <project>",
            ]
        )
        if len(commands) >= 7:
            break
    return commands


def _approval_control_warnings(blockers: Sequence[str], policy: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if policy.get("enabled") is not True:
        warnings.append("Interactive approval is disabled in security.json; approval responses are read-only.")
    workflow_blockers = [blocker for blocker in blockers if blocker != "approval_disabled"]
    if workflow_blockers:
        warnings.append(f"Approval responses are disabled because this workflow is {', '.join(workflow_blockers)}.")
    return warnings


def _inspector_console_payload(context: Mapping[str, Any], *, workflow: Mapping[str, Any] | None) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    blockers = _workflow_mutation_blockers(workflow)
    runner_id = "inspector"
    runner_problem = _inspector_runner_problem(context, runner_id)
    chat_blockers = list(blockers)
    if runner_problem:
        chat_blockers.append("inspector_runner_unavailable")
    chat_records = _chat_conversation_summaries(context)
    change_records = _change_request_summaries(context)
    latest_chat = sorted(chat_records, key=lambda item: str(item.get("ts") or item.get("response_ts") or ""))[-1] if chat_records else {}
    latest_change = sorted(change_records, key=lambda item: str(item.get("created_at") or ""))[-1] if change_records else {}
    endpoint_base = f"/api/workflows/{workflow_id}"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "available" if not blockers else "disabled",
        "workflow_id": workflow_id,
        "mode": INSPECTION_MODE,
        "runner_id": runner_id,
        "mutation_allowed": not blockers,
        "mutation_blockers": blockers,
        "chat_allowed": not chat_blockers,
        "chat_blockers": chat_blockers,
        "change_request_allowed": not blockers,
        "request_record_only": False,
        "read_only_inspection": False,
        "access_policy": "full_agent_access",
        "endpoints": {
            "chat": f"{endpoint_base}/chat",
            "change_request": f"{endpoint_base}/change-requests",
        },
        "allowed_paths": default_allowed_paths(paths),
        "context_paths": default_allowed_paths(paths),
        "chat_requests_path": _project_relative(project, paths.requests_dir / CHAT_REQUESTS_FILENAME),
        "chat_responses_path": _project_relative(project, paths.requests_dir / CHAT_RESPONSES_FILENAME),
        "change_requests_path": _project_relative(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME),
        "change_request_responses_path": _project_relative(project, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME),
        "chat_count": len(chat_records),
        "chat_pending_count": sum(1 for record in chat_records if record.get("status") == "pending"),
        "chat_answered_count": sum(1 for record in chat_records if record.get("status") == "answered"),
        "chat_change_request_count": sum(
            1 for record in chat_records if record.get("status") == "change_request_created" or record.get("change_request_id")
        ),
        "recent_chat": chat_records[-8:],
        "latest_chat": latest_chat,
        "latest_chat_request_id": latest_chat.get("request_id"),
        "latest_chat_status": latest_chat.get("status"),
        "change_request_count": len(change_records),
        "pending_review_count": sum(1 for record in change_records if record.get("status") == "pending_review"),
        "needs_user_approval_count": sum(1 for record in change_records if record.get("status") == "needs_user_approval"),
        "approved_count": sum(1 for record in change_records if record.get("status") == "approved"),
        "applied_count": sum(1 for record in change_records if record.get("status") == "applied"),
        "recent_change_requests": change_records[-8:],
        "latest_change_request_id": latest_change.get("change_request_id"),
        "latest_change_request_status": latest_change.get("status"),
        "commands": _inspector_console_commands(change_records),
        "warnings": _inspector_console_warnings(blockers, runner_problem),
    }


def _inspector_runner_problem(context: Mapping[str, Any], runner_id: str) -> str | None:
    project = context["project"]
    try:
        runner = load_agent_runners(project).runner(runner_id)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError) as error:
        return f"Inspector runner configuration is not usable: {error}"
    if runner.role != "inspector":
        return f"Runner {runner_id!r} has role {runner.role!r}, expected 'inspector'."
    if not runner.enabled:
        return f"Runner {runner_id!r} is disabled."
    return None


def _chat_conversation_summaries(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    requests = [
        _mapping(record)
        for record in _read_jsonl_if_present(paths.requests_dir / CHAT_REQUESTS_FILENAME)
        if _record_matches_workflow(record, workflow_id)
    ]
    responses = [
        _mapping(record)
        for record in _read_jsonl_if_present(paths.requests_dir / CHAT_RESPONSES_FILENAME)
        if _record_matches_workflow(record, workflow_id)
    ]
    responses_by_request: dict[str, list[Mapping[str, Any]]] = {}
    for response in responses:
        request_id = str(response.get("request_id") or "")
        if request_id:
            responses_by_request.setdefault(request_id, []).append(response)
    summaries: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("request_id") or "")
        matching = sorted(
            responses_by_request.get(request_id, []),
            key=lambda item: str(item.get("ts") or item.get("created_at") or ""),
        )
        response = _mapping(matching[-1]) if matching else {}
        status = str(response.get("status") or "pending")
        summaries.append(
            {
                "request_id": request_id,
                "response_id": response.get("response_id"),
                "ts": request.get("ts") or request.get("created_at"),
                "response_ts": response.get("ts") or response.get("created_at"),
                "status": status,
                "mode": request.get("mode") or INSPECTION_MODE,
                "runner_id": request.get("runner_id") or "inspector",
                "user_message": request.get("user_message"),
                "answer": response.get("answer") or response.get("summary"),
                "summary": response.get("summary"),
                "refs": list(_sequence(response.get("refs"))),
                "read_only": response.get("read_only") is True if response else None,
                "change_request_id": response.get("change_request_id"),
            }
        )
    return sorted(summaries, key=lambda item: str(item.get("ts") or item.get("response_ts") or ""))


def _change_request_summaries(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths = context["paths"]
    workflow_id = str(context["workflow_id"])
    summaries: list[dict[str, Any]] = []
    for raw_request in _read_jsonl_if_present(paths.requests_dir / CHANGE_REQUESTS_FILENAME):
        if not _record_matches_workflow(raw_request, workflow_id):
            continue
        record = change_request_status_record(paths, raw_request)
        latest_response = _mapping(record.get("latest_response"))
        summaries.append(
            {
                "change_request_id": record.get("change_request_id"),
                "created_at": record.get("created_at"),
                "source": record.get("source"),
                "status": record.get("status") or "pending_review",
                "user_request": record.get("user_request"),
                "approval_request_id": record.get("approval_request_id") or latest_response.get("approval_request_id"),
                "originating_chat_request_id": record.get("originating_chat_request_id"),
                "response_count": len(_sequence(record.get("responses"))),
                "latest_response_id": latest_response.get("response_id"),
            }
        )
    return sorted(summaries, key=lambda item: str(item.get("created_at") or ""))


def _record_matches_workflow(record: Mapping[str, Any], workflow_id: str) -> bool:
    record_workflow_id = str(record.get("workflow_id") or "")
    return not record_workflow_id or record_workflow_id == workflow_id


def _read_jsonl_if_present(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        return []
    return _read_jsonl(path)


def _inspector_console_commands(change_records: Sequence[Mapping[str, Any]]) -> list[str]:
    commands = [
        'loopplane ask --project <project> "Where is the workflow currently blocked?"',
        'loopplane change-request submit --project <project> "Describe the requested plan change"',
    ]
    for record in change_records:
        change_request_id = _text(record.get("change_request_id"))
        if not change_request_id:
            continue
        commands.append(f"loopplane change-request review {change_request_id} --project <project>")
        if len(commands) >= 6:
            break
    return commands


def _inspector_console_warnings(blockers: Sequence[str], runner_problem: str | None) -> list[str]:
    warnings: list[str] = []
    if blockers:
        warnings.append(f"Inspector console request creation is disabled because this workflow is {', '.join(blockers)}.")
    if runner_problem:
        warnings.append(runner_problem)
    return warnings


def _node_details_payload(
    context: Mapping[str, Any],
    models: Mapping[str, Any],
    *,
    include_split_run_details: bool = False,
) -> dict[str, Any]:
    json_models = _mapping(models.get("json"))
    jsonl_models = _mapping(models.get("jsonl"))
    workflow_graph = _mapping(json_models.get("workflow_graph.json"))
    nodes = [_mapping(node) for node in _sequence(workflow_graph.get("nodes"))]
    run_summaries = [_mapping(record) for record in _sequence(jsonl_models.get("run_summaries.jsonl"))]
    run_index = [_mapping(record) for record in _sequence(jsonl_models.get("run_index.jsonl"))]
    run_records = run_summaries or run_index
    run_source = "run_summaries.jsonl" if run_summaries else "run_index.jsonl"
    runs_by_id = {
        str(record.get("run_id") or ""): record
        for record in run_records
        if str(record.get("run_id") or "")
    }
    run_sources_by_id = {run_id: run_source for run_id in runs_by_id}
    if include_split_run_details:
        visible_run_ids = {
            str(node.get("run_id") or "")
            for node in nodes
            if str(node.get("run_id") or "")
        }
        for split_run_id, detail_record in _split_run_detail_records(context, visible_run_ids).items():
            summary = dict(_mapping(runs_by_id.get(split_run_id)))
            detail_metadata = {str(key): value for key, value in detail_record.items() if key != "details"}
            summary.update(detail_metadata)
            summary["details"] = _mapping(detail_record.get("details"))
            runs_by_id[split_run_id] = summary
            run_sources_by_id[split_run_id] = "run_details"
    node_details: dict[str, Any] = {}
    run_details: dict[str, Any] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        run_id = str(node.get("run_id") or "")
        detail = _node_detail_from_read_models(
            context,
            node,
            runs_by_id,
            run_source=run_sources_by_id.get(run_id, run_source),
        )
        node_details[node_id] = detail
        if run_id and run_id not in run_details and detail.get("run"):
            run_details[run_id] = detail
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": context["workflow_id"],
        "source": "read_models",
        "nodes": node_details,
        "runs": run_details,
    }


def _split_run_detail_records(context: Mapping[str, Any], run_ids: set[str]) -> dict[str, Mapping[str, Any]]:
    wanted = {run_id for run_id in run_ids if run_id}
    if not wanted:
        return {}
    project = context["project"]
    paths = context["paths"]
    manifest = _read_optional_json(paths.read_models_dir / "run_details_manifest.json")
    try:
        detail_root = (paths.read_models_dir / READ_MODEL_DETAIL_DIR).resolve()
    except OSError:
        return {}
    records: dict[str, Mapping[str, Any]] = {}
    for entry in _sequence(manifest.get("runs")):
        manifest_record = _mapping(entry)
        run_id = str(manifest_record.get("run_id") or "")
        if run_id not in wanted:
            continue
        detail_path_value = _text(manifest_record.get("path"))
        if not detail_path_value:
            continue
        try:
            detail_path = _resolve_project_relative_file(project, detail_path_value)
            resolved_detail = detail_path.resolve()
            if resolved_detail != detail_root and detail_root not in resolved_detail.parents:
                continue
        except (OSError, WorkflowPathError):
            continue
        detail_record = _read_optional_json(detail_path)
        if detail_record and str(detail_record.get("run_id") or "") == run_id:
            records[run_id] = detail_record
    return records


def _lazy_file_content_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    read_models = dict(_mapping(result.get("read_models")))
    for filename in ("plan_index.json", "workflow_graph.json"):
        if filename in read_models:
            read_models[filename] = _strip_path_file_content(read_models.get(filename))
    result["read_models"] = read_models
    if "node_details" in result:
        result["node_details"] = _strip_path_file_content(result.get("node_details"))
    jsonl_models = dict(_mapping(result.get("jsonl_models")))
    if "run_summaries.jsonl" in jsonl_models:
        jsonl_models["run_summaries.jsonl"] = _strip_path_file_content(jsonl_models.get("run_summaries.jsonl"))
    result["jsonl_models"] = jsonl_models
    snapshots = _mapping(result.get("workflow_snapshots"))
    if snapshots:
        result["workflow_snapshots"] = {
            str(key): _lazy_file_content_payload(value) if isinstance(value, Mapping) else value
            for key, value in snapshots.items()
        }
    return result


def _strip_path_file_content(value: Any) -> Any:
    if isinstance(value, Mapping):
        has_file_path = bool(_text(value.get("path") or value.get("markdown_path") or ""))
        stripped: dict[str, Any] = {}
        for key, child in value.items():
            if has_file_path and key == "content":
                continue
            stripped[str(key)] = _strip_path_file_content(child)
        return stripped
    if isinstance(value, list):
        return [_strip_path_file_content(item) for item in value]
    return value


def _node_detail_from_read_models(
    context: Mapping[str, Any],
    node: Mapping[str, Any],
    runs_by_id: Mapping[str, Mapping[str, Any]],
    *,
    run_source: str,
) -> dict[str, Any]:
    run_id = str(node.get("run_id") or "")
    node_type = _status_value(node.get("type"))
    if node_type == "event":
        run = {}
        sections = _event_node_sections(node)
    else:
        run = _mapping(runs_by_id.get(run_id))
        run_details = _mapping(run.get("details"))
        sections = _sequence(run_details.get("sections"))
        if not sections:
            sections = _non_run_node_sections(node)
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": context["workflow_id"],
        "node_id": node.get("node_id"),
        "run_id": run_id or None,
        "task_id": node.get("task_id") or run.get("task_id"),
        "source": run_source if run else "workflow_graph.json",
        "run": run if run else None,
        "sections": list(sections),
        "available_sections": [str(_mapping(section).get("key")) for section in sections if _mapping(section).get("available")],
        "missing_sections": [str(_mapping(section).get("key")) for section in sections if not _mapping(section).get("available")],
    }


def _non_run_node_sections(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary = _mapping(node.get("summary"))
    return [
        {
            "key": "summary",
            "title": "Summary",
            "available": bool(summary),
            "content": _text(summary.get("one_line") or "No summary available."),
        }
    ]


def _event_node_sections(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary = _mapping(node.get("summary"))
    details = {
        "event_sequence": node.get("event_sequence"),
        "event_id": node.get("event_id"),
        "event_type": node.get("event_type") or node.get("status"),
        "task_id": node.get("task_id"),
        "run_id": node.get("run_id"),
        "runner_id": node.get("runner_id"),
        "agent_role": node.get("agent_role"),
        "actor": node.get("actor_label"),
        "context": node.get("context_label"),
        "timestamp": node.get("started_at") or node.get("created_at") or node.get("ended_at"),
    }
    return [
        {
            "key": "summary",
            "title": "Summary",
            "available": bool(summary),
            "content": _text(summary.get("one_line") or "No summary available."),
        },
        {
            "key": "event_details",
            "title": "Event Details",
            "available": True,
            "status": details.get("event_type") or "event",
            "summary": {key: value for key, value in details.items() if value not in (None, "")},
        },
    ]


def _read_model_response(context: Mapping[str, Any], filename: str) -> dict[str, Any]:
    payload = _read_optional_json(context["paths"].read_models_dir / filename)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(payload),
        "status": "ok" if payload else "not_found",
        "workflow_id": context["workflow_id"],
        "file": filename,
        "data": payload,
    }


def _run_detail_payload(context: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    split_payload = _run_detail_payload_from_split(context, run_id)
    if split_payload is not None:
        return split_payload
    workflow = _workflow_record_for_id(_workflow_records(context), str(context["workflow_id"]))
    models, _freshness, _rebuild_result = _load_live_read_models(
        context["project"],
        context["paths"],
        workflow_id=str(context["workflow_id"]),
        allow_rebuild=not _workflow_mutation_blockers(workflow),
        max_dashboard_events=_optional_positive_int(context.get("max_dashboard_events")),
        cache=_read_model_cache_from_context(context),
    )
    summaries = _sequence(_mapping(models.get("jsonl")).get("run_summaries.jsonl"))
    run_source = "run_summaries.jsonl"
    matches = [record for record in summaries if str(record.get("run_id") or "") == run_id]
    if not matches:
        run_source = "run_index.jsonl"
        run_index = _sequence(_mapping(models.get("jsonl")).get("run_index.jsonl"))
        matches = [record for record in run_index if str(record.get("run_id") or "") == run_id]
    if not matches:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "run_not_found",
            "workflow_id": context["workflow_id"],
            "run_id": run_id,
            "errors": [f"run {run_id!r} was not found in run_summaries.jsonl or run_index.jsonl"],
        }
    run = _mapping(matches[-1])
    run_details = _mapping(run.get("details"))
    if not run_details and (
        run_source == "run_index.jsonl"
        or run.get("compatibility_mode") == "split_details"
        or run.get("details_externalized") is True
        or "detail_status" in run
    ):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "detail_missing",
            "workflow_id": context["workflow_id"],
            "run_id": run_id,
            "run": _strip_path_file_content(run),
            "details": {},
            "node_detail": {},
            "source": run_source,
            "errors": [f"run {run_id!r} detail was externalized but no split detail record was available"],
        }
    node_details = _node_details_payload(context, models) if not models.get("errors") else {}
    run_detail = _mapping(_mapping(node_details.get("runs")).get(run_id))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "workflow_id": context["workflow_id"],
        "run_id": run_id,
        "run": _strip_path_file_content(run),
        "details": _strip_path_file_content(run_details),
        "node_detail": _strip_path_file_content(run_detail),
        "source": run_source,
    }


def _run_detail_payload_from_split(context: Mapping[str, Any], run_id: str) -> dict[str, Any] | None:
    project = context["project"]
    paths = context["paths"]
    manifest = _read_optional_json(paths.read_models_dir / "run_details_manifest.json")
    matches = [
        _mapping(record)
        for record in _sequence(manifest.get("runs"))
        if str(_mapping(record).get("run_id") or "") == run_id
    ]
    if not matches:
        return None
    record = matches[-1]
    detail_path_value = _text(record.get("path"))
    if not detail_path_value:
        return None
    try:
        detail_path = _resolve_project_relative_file(project, detail_path_value)
        detail_root = (paths.read_models_dir / READ_MODEL_DETAIL_DIR).resolve()
        resolved_detail = detail_path.resolve()
        if resolved_detail != detail_root and detail_root not in resolved_detail.parents:
            return None
    except (OSError, WorkflowPathError):
        return None
    detail_record = _read_optional_json(detail_path)
    if not detail_record:
        return None
    run = {str(key): value for key, value in detail_record.items() if key != "details"}
    details = _mapping(detail_record.get("details"))
    node_detail = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": context["workflow_id"],
        "node_id": detail_record.get("node_id"),
        "run_id": run_id,
        "task_id": detail_record.get("task_id"),
        "source": "run_details",
        "run": run,
        "sections": list(_sequence(details.get("sections"))),
        "available_sections": list(_sequence(details.get("available_sections"))),
        "missing_sections": list(_sequence(details.get("missing_sections"))),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "workflow_id": context["workflow_id"],
        "run_id": run_id,
        "run": _strip_path_file_content(run),
        "details": _strip_path_file_content(details),
        "node_detail": _strip_path_file_content(node_detail),
        "source": "run_details",
    }


def _control_status_response(context: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    state = _read_optional_json(paths.runtime_dir / "state.json")
    requests = read_control_requests(paths)
    responses = read_control_responses(paths)
    controls = control_request_statuses(requests, responses)
    latest_response = sorted(responses, key=lambda item: str(item.get("handled_at") or ""))[-1] if responses else None
    runtime_status = str(state.get("status") or "unknown")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": runtime_status,
        "runtime_status": runtime_status,
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "runtime_state": state,
        "scheduler": dict(state.get("scheduler") or {}) if isinstance(state.get("scheduler"), Mapping) else {},
        "requests_path": _project_relative(project, paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "responses_path": _project_relative(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "pending_count": sum(1 for record in controls if record.get("status") == "pending"),
        "applied_count": sum(1 for record in controls if record.get("status") == "applied"),
        "rejected_count": sum(1 for record in controls if record.get("status") == "rejected"),
        "controls": controls,
        "latest_response": latest_response,
        "errors": [],
        "warnings": [],
    }


def _record_control_request(
    context: Mapping[str, Any],
    request_type: str,
    *,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    normalized_type = str(request_type or "").strip().lower().replace("-", "_")
    if normalized_type not in CONTROL_REQUEST_TYPES:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "invalid_control_type",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "errors": [f"Control request type must be one of: {', '.join(sorted(CONTROL_REQUEST_TYPES))}."],
            "warnings": [],
        }
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": new_control_request_id(),
        "created_at": utc_timestamp(),
        "type": normalized_type,
        "source": "dashboard_api",
        "workflow_id": context["workflow_id"],
        "status": "pending",
    }
    if payload:
        request["payload"] = _json_safe_object(payload)
    path = paths.runtime_dir / CONTROL_REQUESTS_FILENAME
    _append_jsonl(path, request)
    status = _control_status_response(context)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "pending",
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "requests_path": _project_relative(project, path),
        "responses_path": _project_relative(project, paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "request": request,
        "pending_count": status["pending_count"],
        "files_written": [_project_relative(project, path)],
        "errors": [],
        "warnings": [],
    }


def _record_read_model_rebuild_request(context: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    paths = context["paths"]
    models = _load_read_models(
        paths.read_models_dir,
        cache_scope=_read_model_cache_scope(context["project"], paths, str(context["workflow_id"])),
    )
    freshness: Mapping[str, Any] = {}
    if not models["errors"]:
        freshness = _read_model_freshness(context["project"], paths, models["json"])
    reason = _bounded_text(body.get("reason"), limit=500)
    payload: dict[str, Any] = {
        "source": str(body.get("source") or "dashboard_api"),
        "request_channel": "control_requests",
        "requested_action": "rebuild_read_models",
        "mutation_policy": "request_record_only",
        "dashboard_must_not_rebuild_directly": True,
        "max_dashboard_events": _optional_positive_int(context.get("max_dashboard_events")) or _dashboard_payload_event_limit(
            paths,
            explicit_limit=None,
            rebuild_result=None,
        ),
        "freshness": _freshness_request_summary(freshness, errors=models["errors"]),
    }
    if reason:
        payload["reason"] = reason
    result = _record_control_request(context, "rebuild_read_models", payload=payload)
    result["read_model_rebuild_request_only"] = True
    return result


def _freshness_request_summary(freshness: Mapping[str, Any], *, errors: Sequence[Any]) -> dict[str, Any]:
    warning_summaries: list[dict[str, Any]] = []
    for warning in _sequence(freshness.get("warnings"))[:12]:
        item = _mapping(warning)
        warning_summaries.append(
            {
                "code": item.get("code"),
                "file": item.get("file"),
                "severity": item.get("severity"),
                "missing_fields": list(_sequence(item.get("missing_fields")))[:8],
                "reasons": [_bounded_text(reason, limit=240) for reason in _sequence(item.get("reasons"))[:8]],
            }
        )
    return {
        "status": freshness.get("status") or ("missing_read_models" if errors else "unknown"),
        "summary": freshness.get("summary") or "; ".join(str(error) for error in errors[:4]),
        "read_model": dict(_mapping(freshness.get("read_model"))),
        "event_log": dict(_mapping(freshness.get("event_log"))),
        "checked_files": [str(filename) for filename in _sequence(freshness.get("checked_files"))[:16]],
        "warnings": warning_summaries,
    }


def _bounded_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def _approval_status_response(context: Mapping[str, Any], *, include_all: bool) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    policy = load_approval_policy(paths)
    requests = read_approval_requests(paths)
    responses = read_approval_responses(paths)
    records = [
        _approval_record_summary(
            context,
            approval_record_status(request, responses=responses, now=utc_timestamp()),
        )
        for request in requests
    ]
    if not include_all:
        records = [record for record in records if record["status"] == "pending"]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "approval_disabled" if not policy["enabled"] else "ok",
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "approval_policy": policy,
        "requests_path": _project_relative(project, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME),
        "responses_path": _project_relative(project, paths.runtime_dir / APPROVAL_RESPONSES_FILENAME),
        "pending_count": sum(1 for record in records if record["status"] == "pending"),
        "approvals": records,
        "errors": [],
        "warnings": [] if policy["enabled"] else ["Interactive approval is disabled by security.json."],
    }


def _approval_record_summary(context: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    item = _mapping(record)
    response = _mapping(item.get("response"))
    approval_id = str(item.get("approval_id") or item.get("request_id") or "").strip()
    workflow_id = str(item.get("workflow_id") or context["workflow_id"] or "").strip()
    summary = {
        "schema_version": item.get("schema_version") or SCHEMA_VERSION,
        "approval_id": approval_id,
        "workflow_id": workflow_id,
        "status": _text(item.get("status") or "pending"),
        "decision": item.get("decision") or response.get("decision"),
        "type": item.get("type"),
        "task_id": item.get("task_id"),
        "run_id": item.get("run_id"),
        "message": item.get("message"),
        "scope": item.get("scope"),
        "created_at": item.get("created_at") or item.get("requested_at"),
        "requested_at": item.get("requested_at") or item.get("created_at"),
        "expires_at": item.get("expires_at"),
        "responded_at": item.get("responded_at") or response.get("responded_at"),
        "approved_by": response.get("approved_by") or item.get("approved_by"),
        "source": item.get("source"),
        "evidence_refs": _approval_evidence_refs(context, item),
        "respond_endpoint": f"/api/workflows/{workflow_id}/approvals/{approval_id}/respond" if approval_id else None,
    }
    if response:
        summary["response"] = {
            "approval_id": approval_id,
            "responded_at": response.get("responded_at"),
            "decision": response.get("decision") or response.get("status"),
            "approved_by": response.get("approved_by"),
            "scope": response.get("scope"),
            "notes": response.get("notes"),
            "source": response.get("source"),
        }
    return {key: value for key, value in summary.items() if value is not None}


def _approval_evidence_refs(context: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in (
        "evidence",
        "evidence_path",
        "evidence_paths",
        "source_path",
        "source_paths",
        "source_file",
        "source_files",
        "validation_path",
        "validation_file",
        "run_dir",
        "report_path",
        "artifact_path",
        "artifact_paths",
        "prompt_path",
        "final_output_path",
        "request_path",
        "response_path",
    ):
        value = record.get(key)
        refs.extend(_approval_ref_values(context, value))
    return _dedupe_text(refs)


def _approval_ref_values(context: Mapping[str, Any], value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        refs: list[str] = []
        for item_value in value.values():
            refs.extend(_approval_ref_values(context, item_value))
        return refs
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        refs = []
        for item in value:
            refs.extend(_approval_ref_values(context, item))
        return refs
    text_value = str(value).strip()
    if not text_value:
        return []
    return [_dashboard_path_label(context, text_value)]


def _dashboard_path_label(context: Mapping[str, Any], value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(context["project"].resolve()).as_posix()
        except (OSError, ValueError):
            return "[redacted path]"
    posix_path = PurePosixPath(value)
    if ".." in posix_path.parts:
        return "[redacted path]"
    return posix_path.as_posix()


def _dedupe_text(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _record_approval_response(
    context: Mapping[str, Any],
    approval_id: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    policy = load_approval_policy(paths)
    decision = str(body.get("decision") or body.get("status") or "").strip().lower()
    if decision not in ALLOWED_APPROVAL_DECISIONS:
        return _approval_response_failure(
            context,
            "invalid_decision",
            f"Decision must be one of: {', '.join(sorted(ALLOWED_APPROVAL_DECISIONS))}.",
            approval_policy=policy,
        )
    if not approval_id.strip():
        return _approval_response_failure(context, "missing_approval_id", "approval_id is required.", approval_policy=policy)
    if not policy["enabled"]:
        return _approval_response_failure(
            context,
            "approval_disabled",
            "Interactive approval is disabled in security.json; refusing to record an approval decision.",
            approval_policy=policy,
        )

    requests = read_approval_requests(paths)
    responses = read_approval_responses(paths)
    request = _approval_request_by_id(requests, approval_id)
    if request is None:
        return _approval_response_failure(
            context,
            "approval_not_found",
            f"No approval request exists for {approval_id}.",
            approval_policy=policy,
        )
    status = approval_record_status(request, responses=responses, now=utc_timestamp())
    if status["status"] != "pending":
        return _approval_response_failure(
            context,
            "approval_already_closed",
            f"Approval {approval_id} is already {status['status']}.",
            approval_policy=policy,
            approval=status,
        )

    response = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "responded_at": utc_timestamp(),
        "decision": decision,
        "approved_by": str(body.get("approved_by") or body.get("responded_by") or "dashboard_user"),
        "scope": str(body.get("scope") or request.get("scope") or ""),
        "notes": str(body.get("notes") or ""),
        "source": "dashboard_api",
        "workflow_id": str(request.get("workflow_id") or context["workflow_id"]),
    }
    for field in ("task_id", "run_id", "type"):
        if request.get(field) is not None:
            response[field] = request[field]
    path = paths.runtime_dir / APPROVAL_RESPONSES_FILENAME
    _append_jsonl(path, response)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": decision,
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "approval_policy": policy,
        "approval": _approval_record_summary(
            context,
            approval_record_status(request, responses=[*responses, response], now=response["responded_at"]),
        ),
        "response": response,
        "files_written": [_project_relative(project, path)],
        "errors": [],
        "warnings": [],
    }


def _approval_response_failure(
    context: Mapping[str, Any],
    status: str,
    message: str,
    *,
    approval_policy: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": context["project"].as_posix(),
        "workflow_id": context["workflow_id"],
        "errors": [message],
        "warnings": [],
    }
    if approval_policy is not None:
        result["approval_policy"] = dict(approval_policy)
    if approval is not None:
        result["approval"] = _approval_record_summary(context, approval)
    return result


def _approval_request_by_id(requests: Sequence[Mapping[str, Any]], approval_id: str) -> dict[str, Any] | None:
    for request in requests:
        if str(request.get("approval_id") or request.get("request_id") or "") == approval_id:
            return dict(request)
    return None


def _record_chat_request(context: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    project = context["project"]
    message = str(body.get("message") or body.get("user_message") or "").strip()
    if not message:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "invalid_request",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "errors": ["Inspection question is required."],
            "warnings": [],
        }
    runner_id = str(body.get("runner_id") or "inspector")
    return answer_inspection(
        project,
        message,
        runner_id=runner_id,
        allowed_paths=_sequence(body.get("allowed_paths")),
        source="dashboard_api",
    )


def _record_change_request(
    context: Mapping[str, Any],
    text: str,
    *,
    source: str,
    metadata: Mapping[str, Any] | None = None,
    originating_chat_request_id: str | None = None,
) -> dict[str, Any]:
    project = context["project"]
    paths = context["paths"]
    user_request = str(text or "").strip()
    if not user_request:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "invalid_request",
            "project_root": project.as_posix(),
            "workflow_id": context["workflow_id"],
            "errors": ["Change request text is required."],
            "warnings": [],
        }
    record = {
        "schema_version": SCHEMA_VERSION,
        "change_request_id": new_change_request_id(),
        "created_at": utc_timestamp(),
        "source": source or "dashboard_api",
        "workflow_id": context["workflow_id"],
        "user_request": user_request,
        "status": "pending_review",
        "impact": {
            "scope_change": True,
            "requires_new_tasks": True,
            "requires_approval": True,
            "analysis_required": True,
        },
        "planner_response": None,
        "approval_request_id": None,
        "applied_plan_update_event_id": None,
    }
    if originating_chat_request_id:
        record["originating_chat_request_id"] = originating_chat_request_id
    if metadata:
        record["metadata"] = dict(metadata)
    path = paths.requests_dir / CHANGE_REQUESTS_FILENAME
    _append_jsonl(path, record)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "pending_review",
        "project_root": project.as_posix(),
        "workflow_id": context["workflow_id"],
        "change_request": record,
        "change_request_id": record["change_request_id"],
        "files_written": [_project_relative(project, path)],
        "dashboard_must_not_mutate_plan_directly": True,
        "errors": [],
        "warnings": [],
    }


def _record_dashboard_request(
    context: Mapping[str, Any],
    request_type: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    paths = context["paths"]
    project = context["project"]
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": _new_dashboard_request_id(),
        "created_at": utc_timestamp(),
        "type": request_type,
        "source": "dashboard_api",
        "workflow_id": context["workflow_id"],
        "status": "pending",
        "payload": _json_safe_object(_redact_dashboard_value(context, payload or {})),
    }
    path = paths.requests_dir / "dashboard_requests.jsonl"
    _append_jsonl(path, request)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "pending",
        "workflow_id": context["workflow_id"],
        "request": request,
        "files_written": [_project_relative(project, path)],
        "planner_must_not_mutate_state_directly": True,
        "errors": [],
        "warnings": [],
    }


def _new_dashboard_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"dash_{stamp}_{uuid.uuid4().hex[:8]}"


def _json_safe_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), default=str))


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _bearer_token(value: str | None) -> str:
    if not value:
        return ""
    prefix = "Bearer "
    return value[len(prefix) :].strip() if value.startswith(prefix) else ""


def _render_error_html(result: Mapping[str, Any]) -> str:
    errors = _sequence(result.get("errors"))
    rows = "".join(f"<li>{_escape(error)}</li>" for error in errors) or "<li>Unable to load dashboard.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LoopPlane Dashboard Error</title>
</head>
<body>
  <main>
    <h1>LoopPlane Dashboard Error</h1>
    <p>{_escape(result.get("status") or "failed")}</p>
    <ul>{rows}</ul>
  </main>
</body>
</html>
"""


class _ReadModelCache:
    def __init__(
        self,
        *,
        max_entries: int | None = None,
        max_scopes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._max_entries = _read_model_cache_limit(
            max_entries,
            env_name="LOOPPLANE_READ_MODEL_CACHE_MAX_ENTRIES",
            default=READ_MODEL_CACHE_DEFAULT_MAX_ENTRIES,
        )
        self._max_scopes = _read_model_cache_limit(
            max_scopes,
            env_name="LOOPPLANE_READ_MODEL_CACHE_MAX_SCOPES",
            default=READ_MODEL_CACHE_DEFAULT_MAX_SCOPES,
        )
        self._max_bytes = _read_model_cache_limit(
            max_bytes,
            env_name="LOOPPLANE_READ_MODEL_CACHE_MAX_BYTES",
            default=READ_MODEL_CACHE_DEFAULT_MAX_BYTES,
        )
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[str, str, str], tuple[tuple[int, int], int, Any]] = OrderedDict()
        self._total_bytes = 0

    def read_json(self, path: Path, *, scope: str = "", stats: dict[str, int] | None = None) -> Any:
        return self._read(path, kind="json", scope=scope, stats=stats)

    def read_jsonl(self, path: Path, *, scope: str = "", stats: dict[str, int] | None = None) -> Any:
        return self._read(path, kind="jsonl", scope=scope, stats=stats)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "scopes": len({key[0] for key in self._entries}),
                "total_bytes": self._total_bytes,
                "max_entries": self._max_entries,
                "max_scopes": self._max_scopes,
                "max_bytes": self._max_bytes,
            }

    def _read(self, path: Path, *, kind: str, scope: str, stats: dict[str, int] | None) -> Any:
        stat_result = path.stat()
        byte_size = int(stat_result.st_size)
        stamp = (byte_size, int(stat_result.st_mtime_ns))
        key = (scope or "default", _cache_path_key(path), kind)
        _increment_cache_stats(stats, "read_attempts")
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == stamp:
                self._entries.move_to_end(key)
                _increment_cache_stats(stats, "cache_hits")
                _increment_cache_stats(stats, "cache_hit_bytes", byte_size)
                return cached[2]
        _increment_cache_stats(stats, "disk_reads")
        _increment_cache_stats(stats, "disk_read_bytes", byte_size)
        if kind == "jsonl":
            value = _read_jsonl(path)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
        if byte_size > self._max_bytes:
            _increment_cache_stats(stats, "skipped_oversize")
            return value
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._total_bytes -= previous[1]
            self._entries[key] = (stamp, byte_size, value)
            self._entries.move_to_end(key)
            self._total_bytes += byte_size
            self._evict_locked(stats)
        return value

    def _evict_locked(self, stats: dict[str, int] | None) -> None:
        while (
            len(self._entries) > self._max_entries
            or self._total_bytes > self._max_bytes
            or len({key[0] for key in self._entries}) > self._max_scopes
        ):
            _key, removed = self._entries.popitem(last=False)
            self._total_bytes -= removed[1]
            _increment_cache_stats(stats, "evictions")


def _read_model_cache_limit(value: int | None, *, env_name: str, default: int) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(default))
    raw_value = os.environ.get(env_name)
    if raw_value is not None:
        try:
            return max(1, int(raw_value))
        except ValueError:
            return max(1, int(default))
    return max(1, int(default))


def _increment_cache_stats(stats: dict[str, int] | None, key: str, amount: int = 1) -> None:
    if stats is None:
        return
    stats[key] = int(stats.get(key) or 0) + int(amount)


def _cache_path_key(path: Path) -> str:
    try:
        return path.resolve().as_posix()
    except OSError:
        return path.absolute().as_posix()


def _read_model_cache_scope(project: Path, paths: WorkflowPaths, workflow_id: str) -> str:
    return "|".join(
        (
            _cache_path_key(project),
            str(workflow_id or ""),
            _cache_path_key(paths.read_models_dir),
        )
    )


def _read_model_cache_diagnostics(
    cache: "_ReadModelCache | None",
    stats: Mapping[str, int],
    *,
    scope: str,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "cache_enabled": cache is not None,
        "scope": scope,
        "read_attempts": int(stats.get("read_attempts") or 0),
        "cache_hits": int(stats.get("cache_hits") or 0),
        "disk_reads": int(stats.get("disk_reads") or 0),
        "cache_hit_bytes": int(stats.get("cache_hit_bytes") or 0),
        "disk_read_bytes": int(stats.get("disk_read_bytes") or 0),
        "skipped_oversize": int(stats.get("skipped_oversize") or 0),
        "skipped_legacy_run_summaries": int(stats.get("skipped_legacy_run_summaries") or 0),
        "evictions": int(stats.get("evictions") or 0),
    }
    if cache is not None:
        diagnostics["cache_snapshot"] = cache.snapshot()
    return diagnostics


def _read_model_cache_from_context(context: Mapping[str, Any]) -> "_ReadModelCache | None":
    cache = context.get("read_model_cache")
    return cache if isinstance(cache, _ReadModelCache) else None


def _load_read_models(
    read_models_dir: Path,
    *,
    cache: "_ReadModelCache | None" = None,
    cache_scope: str = "",
    include_legacy_run_summaries: bool = True,
    read_model_files: Sequence[str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    json_models: dict[str, Mapping[str, Any]] = {}
    jsonl_models: dict[str, list[Mapping[str, Any]]] = {}
    cache_stats: dict[str, int] = {}
    for filename in (tuple(read_model_files) if read_model_files is not None else READ_MODEL_FILES):
        path = read_models_dir / filename
        if filename == "run_summaries.jsonl" and not include_legacy_run_summaries:
            _increment_cache_stats(cache_stats, "skipped_legacy_run_summaries")
            continue
        if not path.is_file():
            if filename in READ_MODEL_COMPAT_OPTIONAL_FILES:
                continue
            errors.append(f"{filename}: missing from {read_models_dir}")
            continue
        try:
            if filename.endswith(".jsonl"):
                if cache is not None:
                    jsonl_models[filename] = cache.read_jsonl(path, scope=cache_scope, stats=cache_stats)
                else:
                    _increment_cache_stats(cache_stats, "read_attempts")
                    stat_result = path.stat()
                    _increment_cache_stats(cache_stats, "disk_reads")
                    _increment_cache_stats(cache_stats, "disk_read_bytes", int(stat_result.st_size))
                    jsonl_models[filename] = _read_jsonl(path)
            else:
                if cache is not None:
                    payload = cache.read_json(path, scope=cache_scope, stats=cache_stats)
                else:
                    _increment_cache_stats(cache_stats, "read_attempts")
                    stat_result = path.stat()
                    _increment_cache_stats(cache_stats, "disk_reads")
                    _increment_cache_stats(cache_stats, "disk_read_bytes", int(stat_result.st_size))
                    payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    errors.append(f"{filename}: expected JSON object")
                else:
                    json_models[filename] = payload
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{filename}: {error}")
    return {
        "json": json_models,
        "jsonl": jsonl_models,
        "errors": errors,
        "diagnostics": {
            "read_models": _read_model_cache_diagnostics(cache, cache_stats, scope=cache_scope),
        },
    }


def _load_live_read_models(
    project: Path,
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    allow_rebuild: bool,
    max_dashboard_events: int | None = None,
    cache: "_ReadModelCache | None" = None,
    include_legacy_run_summaries: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], Mapping[str, Any] | None]:
    models = _load_read_models(
        paths.read_models_dir,
        cache=cache,
        cache_scope=_read_model_cache_scope(project, paths, workflow_id),
        include_legacy_run_summaries=include_legacy_run_summaries,
        read_model_files=DASHBOARD_LIVE_READ_MODEL_FILES,
    )
    freshness = _read_model_freshness(project, paths, models["json"]) if not models["errors"] else {}
    rebuild_result: Mapping[str, Any] | None = None
    should_rebuild = bool(models["errors"]) or str(freshness.get("status") or "") in {"stale", "unknown"}
    if allow_rebuild and should_rebuild:
        rebuild_result = rebuild_read_models(
            project,
            write=True,
            workflow_id=workflow_id,
            max_dashboard_events=max_dashboard_events,
        )
        if rebuild_result.get("ok"):
            models = _load_read_models(
                paths.read_models_dir,
                cache=cache,
                cache_scope=_read_model_cache_scope(project, paths, workflow_id),
                include_legacy_run_summaries=include_legacy_run_summaries,
                read_model_files=DASHBOARD_LIVE_READ_MODEL_FILES,
            )
            freshness = _read_model_freshness(project, paths, models["json"]) if not models["errors"] else {}
    return models, freshness, rebuild_result


def _read_model_rebuild_result_warnings(rebuild_result: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(rebuild_result, Mapping):
        return []
    warnings = [str(warning) for warning in _sequence(rebuild_result.get("warnings"))]
    if rebuild_result.get("ok") is not True:
        errors = [str(error) for error in _sequence(rebuild_result.get("errors"))]
        if errors:
            warnings.append("Live dashboard read-model rebuild failed: " + "; ".join(errors[:4]))
    return warnings


def _plan_markdown_payload(project: Path, paths: WorkflowPaths, plan_index: Mapping[str, Any] | None = None) -> dict[str, Any]:
    index = _mapping(plan_index)
    plan_file_value = str(index.get("plan_file") or "").strip()
    plan_file = _resolve_dashboard_record_path(project, plan_file_value) if plan_file_value else paths.plan_file
    relative = _project_relative(project, plan_file)
    plan_source = _mapping(index.get("plan_source"))
    try:
        content = plan_file.read_text(encoding="utf-8")
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "missing",
            "path": relative,
            "active_plan_file": index.get("active_plan_file") or paths.value("plan_file"),
            "plan_source": dict(plan_source) if plan_source else {},
            "content": "",
            "size_bytes": 0,
            "errors": [str(error)],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "loaded",
        "path": relative,
        "active_plan_file": index.get("active_plan_file") or paths.value("plan_file"),
        "plan_source": dict(plan_source) if plan_source else {},
        "content": content,
        "size_bytes": len(content.encode("utf-8")),
        "errors": [],
    }


def _resolve_dashboard_record_path(project: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project / value


def _read_model_freshness(
    project: Path,
    paths: WorkflowPaths,
    json_models: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    event_log = _event_log_reference(paths)
    current_active_leases_hash = active_leases_source_hash(paths)
    workflow_arg = f" --workflow {paths.workflow_id}" if paths.workflow_id else ""
    rebuild_command = f"loopplane rebuild-read-models --project {project.as_posix()}{workflow_arg}"
    checked_files: list[str] = []
    model_refs: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for filename in sorted(json_models):
        payload = _mapping(json_models.get(filename))
        checked_files.append(filename)
        model_ref = _read_model_event_reference(filename, payload)
        model_refs.append(model_ref)
        missing = _missing_read_model_metadata(payload)
        if missing:
            warnings.append(
                {
                    "code": "read_model_metadata_missing",
                    "severity": "warning",
                    "file": filename,
                    "message": f"{filename} is missing freshness metadata.",
                    "missing_fields": missing,
                    "rebuild_command": rebuild_command,
                }
            )
        stale_reasons = _stale_reasons(model_ref, event_log)
        if stale_reasons:
            warnings.append(
                {
                    "code": "read_model_stale",
                    "severity": "warning",
                    "file": filename,
                    "message": f"{filename} was generated before the current event log head.",
                    "reasons": stale_reasons,
                    "read_model": model_ref,
                    "event_log": event_log,
                    "rebuild_command": rebuild_command,
                }
            )
        source_hashes = _mapping(payload.get("source_hashes"))
        if "active_leases" in source_hashes and source_hashes.get("active_leases") != current_active_leases_hash:
            warnings.append(
                {
                    "code": "read_model_active_leases_stale",
                    "severity": "warning",
                    "file": filename,
                    "message": f"{filename} was generated before the current active-run lease state.",
                    "read_model_active_leases": source_hashes.get("active_leases"),
                    "active_leases": current_active_leases_hash,
                    "rebuild_command": rebuild_command,
                }
            )

    representative = _representative_read_model(model_refs)
    live_drift = _benign_live_freshness_drift(paths, event_log, model_refs, warnings)
    stale = any(str(warning.get("code")) in {"read_model_stale", "read_model_active_leases_stale"} for warning in warnings)
    if stale and live_drift.get("benign") is True:
        suppressed_warning_codes = {"read_model_stale"}
        visible_warnings = [warning for warning in warnings if str(warning.get("code")) not in suppressed_warning_codes]
        status = "current_with_live_drift" if not visible_warnings else "unknown"
    else:
        visible_warnings = warnings
        status = "stale" if stale else ("unknown" if warnings else "current")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "checked_at": utc_timestamp(),
        "checked_files": checked_files,
        "read_model": representative,
        "read_models": model_refs,
        "event_log": event_log,
        "warnings": visible_warnings,
        "suppressed_warnings": warnings if live_drift.get("benign") is True else [],
        "live_drift": live_drift,
        "summary": _freshness_summary(status, representative, event_log, visible_warnings, live_drift=live_drift),
        "rebuild_command": rebuild_command,
        "rebuild_dashboard_command": f"loopplane dashboard --project {project.as_posix()}{workflow_arg} --rebuild-read-models",
    }


def _event_log_reference(paths: WorkflowPaths) -> dict[str, Any]:
    manifest = load_event_segment_manifest(paths.runtime_dir / "events")
    latest_from_manifest = _mapping(manifest.get("latest_event")) if isinstance(manifest, Mapping) else {}
    if latest_from_manifest:
        return {
            "last_event_seq": _event_sequence(latest_from_manifest),
            "source_event_id": _event_id(latest_from_manifest),
            "event_hash": _optional_string(latest_from_manifest.get("event_hash")),
            "events_count": _coerce_int(manifest.get("event_count")),
            "freshness_mode": "event_manifest",
        }
    latest_event = _latest_event_record(paths.runtime_dir / "events")
    if latest_event is None:
        return {
            "last_event_seq": None,
            "source_event_id": None,
            "event_hash": None,
            "freshness_mode": "event_tail",
        }
    return {
        "last_event_seq": _event_sequence(latest_event),
        "source_event_id": _event_id(latest_event),
        "event_hash": _optional_string(latest_event.get("event_hash")),
        "freshness_mode": "event_tail",
    }


def _read_model_event_reference(filename: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    source_hashes = _mapping(payload.get("source_hashes"))
    return {
        "file": filename,
        "generated_at": payload.get("generated_at"),
        "last_event_seq": _coerce_int(payload.get("last_event_seq")),
        "source_event_id": _optional_string(payload.get("source_event_id")),
        "events_head": _optional_string(source_hashes.get("events_head")),
        "events_sha256": _optional_string(source_hashes.get("events_sha256")),
        "events_count": _coerce_int(source_hashes.get("events_count")),
    }


def _missing_read_model_metadata(payload: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if "generated_at" not in payload:
        missing.append("generated_at")
    if not isinstance(payload.get("source_hashes"), Mapping):
        missing.append("source_hashes")
    if "last_event_seq" not in payload and "source_event_id" not in payload:
        missing.append("last_event_seq_or_source_event_id")
    return missing


def _stale_reasons(read_model: Mapping[str, Any], event_log: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    current_seq = _coerce_int(event_log.get("last_event_seq"))
    model_seq = _coerce_int(read_model.get("last_event_seq"))
    if current_seq is not None:
        if model_seq is None:
            reasons.append("read model lacks last_event_seq for a non-empty event log")
        elif model_seq != current_seq:
            relation = "behind" if model_seq < current_seq else "ahead of"
            reasons.append(f"read model sequence {model_seq} is {relation} event log sequence {current_seq}")

    current_event_id = _optional_string(event_log.get("source_event_id"))
    model_event_id = _optional_string(read_model.get("source_event_id"))
    model_events_head = _optional_string(read_model.get("events_head"))
    if current_event_id:
        if model_event_id is None and model_events_head is None:
            reasons.append("read model lacks source_event_id/events_head for a non-empty event log")
        elif model_event_id not in (None, current_event_id):
            reasons.append(f"source_event_id {model_event_id} does not match event log head {current_event_id}")
        elif model_events_head not in (None, current_event_id):
            reasons.append(f"source_hashes.events_head {model_events_head} does not match event log head {current_event_id}")

    current_events_sha = _optional_string(event_log.get("events_sha256"))
    model_events_sha = _optional_string(read_model.get("events_sha256"))
    if current_events_sha and model_events_sha and current_events_sha != model_events_sha:
        reasons.append("source_hashes.events_sha256 does not match current event log files")

    current_count = _coerce_int(event_log.get("events_count"))
    model_count = _coerce_int(read_model.get("events_count"))
    if current_count is not None and model_count is not None and current_count != model_count:
        reasons.append(f"source_hashes.events_count {model_count} does not match current event count {current_count}")
    return reasons


def _representative_read_model(model_refs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for model_ref in model_refs:
        if model_ref.get("file") == "workflow_status.json":
            return model_ref
    return model_refs[0] if model_refs else {}


def _benign_live_freshness_drift(
    paths: WorkflowPaths,
    event_log: Mapping[str, Any],
    model_refs: Sequence[Mapping[str, Any]],
    warnings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stale_warnings = [_mapping(warning) for warning in warnings if _mapping(warning).get("code") == "read_model_stale"]
    if not stale_warnings:
        return {"benign": False, "reason": "no_read_model_stale_warnings"}
    if any(_mapping(warning).get("code") != "read_model_stale" for warning in warnings):
        return {"benign": False, "reason": "non_event_log_freshness_warning_present"}
    current_seq = _coerce_int(event_log.get("last_event_seq"))
    if current_seq is None:
        return {"benign": False, "reason": "event_log_sequence_missing"}
    model_seqs = [
        seq
        for seq in (_coerce_int(_mapping(model_ref).get("last_event_seq")) for model_ref in model_refs)
        if seq is not None
    ]
    if len(model_seqs) != len(model_refs) or not model_seqs:
        return {"benign": False, "reason": "read_model_sequence_missing"}
    if any(seq > current_seq for seq in model_seqs):
        return {"benign": False, "reason": "read_model_sequence_ahead"}
    after_sequence = min(model_seqs)
    if after_sequence == current_seq:
        return {"benign": False, "reason": "no_sequence_drift"}
    tail, truncated = _event_records_after_sequence(
        paths.runtime_dir / "events",
        after_sequence=after_sequence,
        through_sequence=current_seq,
        limit=FRESHNESS_LIVE_DRIFT_EVENT_LIMIT,
    )
    if truncated:
        return {
            "benign": False,
            "reason": "drift_exceeds_event_limit",
            "from_sequence": after_sequence,
            "to_sequence": current_seq,
            "event_limit": FRESHNESS_LIVE_DRIFT_EVENT_LIMIT,
        }
    if not tail:
        return {"benign": False, "reason": "drift_events_unavailable"}
    event_types = _count_values(str(record.get("event_type") or "unknown") for record in tail)
    actions = _count_values(
        str(_mapping(record.get("data")).get("action") or "")
        for record in tail
        if str(record.get("event_type") or "") in {"scheduler_action_selected", "scheduler_wait_tick"}
    )
    statuses = _count_values(
        str(_mapping(record.get("data")).get("status") or "")
        for record in tail
        if str(record.get("event_type") or "") in {"scheduler_waiting", "scheduler_wait_tick"}
    )
    bad_events = [record for record in tail if not _is_benign_freshness_drift_event(record)]
    return {
        "benign": not bad_events,
        "reason": "only_scheduler_waiting_drift" if not bad_events else "non_benign_event_in_drift",
        "from_sequence": after_sequence,
        "to_sequence": current_seq,
        "event_count": len(tail),
        "event_types": event_types,
        "actions": actions,
        "statuses": statuses,
        "non_benign_event_types": [
            str(record.get("event_type") or "unknown")
            for record in bad_events[:10]
        ],
    }


def _event_records_after_sequence(
    events_dir: Path,
    *,
    after_sequence: int,
    through_sequence: int,
    limit: int,
) -> tuple[list[Mapping[str, Any]], bool]:
    if through_sequence <= after_sequence:
        return [], False
    if limit <= 0 or through_sequence - after_sequence > limit:
        return [], True
    records: list[Mapping[str, Any]] = []
    for path in sorted(events_dir.glob("*.jsonl"), reverse=True):
        if not path.is_file():
            continue
        for record in _iter_jsonl_records_reverse(path):
            sequence = _event_sequence(record)
            if sequence is None or sequence > through_sequence:
                continue
            if sequence <= after_sequence:
                return _sort_event_records(records), False
            records.append(record)
            if len(records) > limit:
                return _sort_event_records(records[:limit]), True
    return _sort_event_records(records), False


def _latest_event_record(events_dir: Path) -> Mapping[str, Any] | None:
    for path in sorted(events_dir.glob("*.jsonl"), reverse=True):
        if not path.is_file():
            continue
        for record in _iter_jsonl_records_reverse(path):
            return record
    return None


def _iter_jsonl_records_reverse(path: Path) -> Iterator[Mapping[str, Any]]:
    for line in _iter_jsonl_lines_reverse(path):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            yield record


def _iter_jsonl_lines_reverse(path: Path, *, chunk_size: int = 65536) -> Iterator[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            buffer = b""
            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                buffer = chunk + buffer
                parts = buffer.split(b"\n")
                buffer = parts[0]
                for raw_line in reversed(parts[1:]):
                    if raw_line.strip():
                        yield raw_line.decode("utf-8", errors="replace")
            if buffer.strip():
                yield buffer.decode("utf-8", errors="replace")
    except OSError:
        return


def _sort_event_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(records, key=lambda record: (_event_sequence(record) or 0, str(record.get("event_id") or "")))


def _is_benign_freshness_drift_event(record: Mapping[str, Any]) -> bool:
    event_type = str(record.get("event_type") or "")
    if event_type in BENIGN_FRESHNESS_DRIFT_EVENT_TYPES:
        return True
    data = _mapping(record.get("data"))
    if event_type == "scheduler_action_selected":
        return str(data.get("action") or "") in BENIGN_FRESHNESS_DRIFT_WAIT_ACTIONS
    if event_type == "scheduler_waiting":
        return str(data.get("status") or "") in BENIGN_FRESHNESS_DRIFT_WAIT_STATUSES
    if event_type == "scheduler_wait_tick":
        return (
            str(data.get("action") or "") in BENIGN_FRESHNESS_DRIFT_WAIT_ACTIONS
            or str(data.get("status") or "") in BENIGN_FRESHNESS_DRIFT_WAIT_STATUSES
        )
    return False


def _count_values(values: Sequence[str] | Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _freshness_summary(
    status: str,
    read_model: Mapping[str, Any],
    event_log: Mapping[str, Any],
    warnings: Sequence[Mapping[str, Any]],
    *,
    live_drift: Mapping[str, Any] | None = None,
) -> str:
    if status == "current":
        return "Read models match the current event log head."
    if status == "current_with_live_drift":
        drift = _mapping(live_drift)
        event_count = _coerce_int(drift.get("event_count")) or 0
        return (
            "Read models match meaningful workflow state; they lag by "
            f"{event_count} recent scheduler waiting/heartbeat event(s)."
        )
    stale_count = sum(1 for warning in warnings if str(warning.get("code")) == "read_model_stale")
    if status == "stale":
        return (
            f"{stale_count} read model file(s) are stale relative to event "
            f"{_event_ref_label(event_log)}; representative read model is at {_event_ref_label(read_model)}."
        )
    return "Read model freshness could not be fully verified because metadata is incomplete."


def _freshness_warning_messages(freshness: Mapping[str, Any]) -> list[str]:
    status = freshness.get("status")
    if status not in {"stale", "unknown"}:
        return []
    summary = _text(freshness.get("summary") or "Read model freshness warning.")
    command = _text(freshness.get("rebuild_command") or "")
    return [f"{summary} Rebuild with: {command}" if command else summary]


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, Mapping):
            raise ValueError(f"{path}:{line_number}: JSONL record must be an object")
        records.append(record)
    return records


def _dashboard_output_dir(project: Path, output_dir: Path | str | None) -> Path:
    if output_dir is None:
        return project / ".loopplane" / "dashboard_static"
    output = Path(output_dir).expanduser()
    if not output.is_absolute():
        output = project / output
    return output.resolve()


def _copy_read_models(source: Path, destination: Path) -> None:
    for filename in READ_MODEL_FILES:
        source_file = source / filename
        if not source_file.is_file():
            if filename in READ_MODEL_COMPAT_OPTIONAL_FILES:
                continue
            raise FileNotFoundError(source_file)
        shutil.copy2(source_file, destination / filename)
    source_details = source / READ_MODEL_DETAIL_DIR
    if source_details.is_dir():
        destination_details = destination / READ_MODEL_DETAIL_DIR
        if destination_details.exists():
            shutil.rmtree(destination_details)
        shutil.copytree(source_details, destination_details)


def _copy_static_assets(destination: Path) -> None:
    for filename in STATIC_ASSET_FILES:
        shutil.copy2(DASHBOARD_ASSET_DIR / filename, destination / filename)


def _render_index_html(payload: Mapping[str, Any], *, server_mode: bool) -> str:
    payload = {**dict(payload), "server_mode": bool(server_mode)}
    read_models = payload["read_models"]
    jsonl_models = payload["jsonl_models"]
    workflow_status = _mapping(read_models.get("workflow_status.json"))
    plan_index = _mapping(read_models.get("plan_index.json"))
    workflow_graph = _mapping(read_models.get("workflow_graph.json"))
    version_control = _mapping(read_models.get("version_control_status.json"))
    metrics = _mapping(read_models.get("metrics.json"))
    node_details = _mapping(payload.get("node_details"))
    change_requests = _mapping(workflow_status.get("change_requests"))
    runner_configuration = _mapping(payload.get("runner_configuration"))
    planning_controls = _mapping(payload.get("planning_controls"))
    execution_controls = _mapping(payload.get("execution_controls"))
    approval_controls = _mapping(payload.get("approval_controls"))
    inspector_console = _mapping(payload.get("inspector_console"))
    freshness = _mapping(payload.get("read_model_freshness"))
    read_model_rebuild = _mapping(payload.get("read_model_rebuild"))
    plan_markdown = _mapping(payload.get("plan_markdown"))
    feed = _sequence(jsonl_models.get("dashboard_feed.jsonl"))
    nodes = _sequence(workflow_graph.get("nodes"))
    selected_node = _mapping(nodes[0]) if nodes else {}
    workflow_title = _text(payload.get("workflow_title") or "Workflow")
    title = f"LoopPlane Dashboard - {workflow_title}"
    workspace_selector = _render_workspace_selector(payload)
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  <link rel="stylesheet" href="static_dashboard.css">
</head>
<body>
  {_render_dashboard_loading_overlay()}
  <div class="dashboard-shell" data-dashboard-mode="{'server' if server_mode else 'static'}">
    <header class="app-header" aria-label="LoopPlane dashboard">
      <div class="app-brand">
        <img class="app-logo" src="loopplane_logo_dark.png" data-logo-dark="loopplane_logo_dark.png" data-logo-light="loopplane_logo_light.png" alt="" aria-hidden="true">
        <strong>LoopPlane</strong>
      </div>
      <button id="theme-toggle" class="theme-toggle" type="button" aria-pressed="true">Light mode</button>
    </header>
    <div id="freshness-banner-shell">{_render_freshness_banner(freshness, read_model_rebuild, server_mode=server_mode)}</div>
    {_render_attention_banner(workflow_status)}
    <main class="dashboard-grid">
      {_render_panel_shell("plan", "plan-panel", "plan-title", "Plan", "Plan Checklist", "plan-panel-body", _render_plan_panel(plan_index, plan_markdown, workflow_graph=workflow_graph, payload=payload))}
      <header class="top-bar dashboard-live-panel">
        <div class="workspace-block">
          <p class="eyebrow dashboard-mode-label">{'Live' if server_mode else 'Static'} Dashboard</p>
          <h1 id="dashboard-workflow-title">{_escape(workflow_title)}</h1>
          {_render_workspace_identity(payload)}
        </div>
        {workspace_selector}
        <div class="status-strip" aria-label="Workflow status">
          {_metric("Workflow Status", workflow_status.get("status"), "status")}
          {_metric("Phase", workflow_status.get("phase"), "phase")}
          {_metric("Progress", _progress_label(workflow_status), "progress")}
          {_metric("Elapsed", _workflow_elapsed_label(payload, workflow_status, workflow_graph, feed), "elapsed")}
          {_metric("Git Checkpoint", _checkpoint_label(version_control), "checkpoint")}
          {_metric("Approvals", _approval_metric_label(approval_controls), "approvals")}
        </div>
        {_render_refresh_controls(payload, server_mode=server_mode)}
        <div id="approval-alert-shell">{_render_approval_top_alert(approval_controls)}</div>
      </header>
      {_render_panel_shell("graph", "graph-panel", "graph-title", "Runtime", "Workflow Graph", "graph-panel-body", _render_graph_panel(workflow_graph, plan_index))}
      {_render_panel_shell("details", "detail-panel", "detail-title", "Selected Node", "Node Details", "node-detail-body", _node_detail_html(selected_node, _detail_for_node(selected_node, node_details)) if selected_node else '<p class="empty-state">No node selected.</p>')}
      {_render_panel_shell("feed", "feed-panel", "feed-title", "Events", "Activity Feed", "activity-feed-body", _render_activity_feed(feed, payload))}
      {_render_panel_shell("vc", "vc-panel", "vc-title", "Version Control", "Git Checkpoint Status", "vc-panel-body", _render_git_status(version_control))}
      {_render_panel_shell("approval", "approval-panel", "approval-title", "Human Review", "Approval Panel", "approval-panel-body", _render_approval_panel(approval_controls, server_mode=server_mode))}
      {_render_panel_shell("runner", "runner-panel", "runner-title", "Trusted Local", "Runner Configuration", "runner-panel-body", _render_runner_panel(runner_configuration, server_mode=server_mode))}
      {_render_panel_shell("inspector", "inspector-panel", "inspector-title", "Inspector", "Inspector Chat / Change Request Console", "inspector-console-body", _render_inspector_console(inspector_console, fallback_change_requests=change_requests, server_mode=server_mode, execution_controls_html=_render_control_panel(workflow_status, planning_controls, execution_controls, server_mode=server_mode)))}
      {_render_panel_shell("metrics", "metrics-panel", "metrics-title", "Snapshot", "Read Model Snapshot", "metrics-panel-body", _render_read_model_snapshot(payload, metrics))}
    </main>
  </div>
  {_render_file_preview_modal()}
  <script id="loopplane-read-models" type="application/json">{_json_script(payload)}</script>
  <script src="static_dashboard.js"></script>
</body>
</html>
"""


def _render_panel_shell(
    key: str,
    panel_class: str,
    title_id: str,
    eyebrow: str,
    title: str,
    body_id: str,
    body_html: str,
    *,
    default_collapsed: bool = False,
) -> str:
    collapsed_class = " is-collapsed" if default_collapsed else ""
    expanded = "false" if default_collapsed else "true"
    hidden_attr = " hidden" if default_collapsed else ""
    default_attr = "true" if default_collapsed else "false"
    toggle_text = "Expand" if default_collapsed else "Collapse"
    return f"""<section class="panel {panel_class}{collapsed_class}" aria-labelledby="{_escape(title_id)}" data-panel-key="{_escape(key)}" data-collapsible="true" data-default-collapsed="{default_attr}">
  <div class="panel-heading" data-panel-heading="{_escape(key)}">
    <div>
      <p class="eyebrow">{_escape(eyebrow)}</p>
      <h2 id="{_escape(title_id)}">{_escape(title)}</h2>
    </div>
    <button class="panel-collapse-toggle" type="button" data-panel-toggle="{_escape(key)}" aria-controls="{_escape(body_id)}" aria-expanded="{expanded}">
      <span class="panel-toggle-text">{toggle_text}</span>
    </button>
  </div>
  <div id="{_escape(body_id)}" class="panel-body"{hidden_attr}>{body_html}</div>
</section>"""


def _render_file_preview_modal() -> str:
    return """<div id="file-preview-modal" class="file-preview-modal" hidden>
  <div class="file-preview-backdrop" data-file-preview-close></div>
  <section class="file-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="file-preview-title">
    <header>
      <div>
        <p class="eyebrow">Evidence File</p>
        <h2 id="file-preview-title">File Preview</h2>
        <p id="file-preview-path" class="detail-path"></p>
        <p id="file-preview-status" class="selector-status"></p>
      </div>
      <button type="button" class="file-preview-close" data-file-preview-close>Close</button>
    </header>
    <div id="file-preview-content" class="detail-pre file-preview-content"></div>
  </section>
</div>"""


def _render_dashboard_loading_overlay() -> str:
    return """<div id="dashboard-loading-overlay" class="dashboard-loading-overlay" role="status" aria-live="polite">
  <div class="dashboard-loading-card">
    <span class="dashboard-loading-spinner" aria-hidden="true"></span>
    <strong id="dashboard-loading-title">Loading dashboard</strong>
    <span id="dashboard-loading-message">Preparing the workflow snapshot and rendering the dashboard.</span>
  </div>
</div>"""


def _render_workspace_identity(payload: Mapping[str, Any]) -> str:
    workspace = _mapping(payload.get("workspace"))
    facts = [
        _detail_row("Workspace", workspace.get("name") or workspace.get("workspace_id") or "workspace"),
        _detail_row("Project", workspace.get("project_root") or payload.get("project_root")),
        _detail_row("Workflow ID", payload.get("workflow_id")),
        _detail_row("Current", workspace.get("current_workflow_id") or payload.get("workflow_id")),
    ]
    return f'<dl class="workspace-facts">{"".join(facts)}</dl>'


def _render_refresh_controls(payload: Mapping[str, Any], *, server_mode: bool) -> str:
    rendered_at = _text(payload.get("rendered_at") or utc_timestamp())
    disabled_attr = "" if server_mode else " disabled"
    mode = "live" if server_mode else "static"
    status = "Auto-refresh every 30s." if server_mode else "Static snapshot; live refresh unavailable."
    return f"""<div class="dashboard-refresh" data-refresh-mode="{mode}">
  <button id="dashboard-refresh-button" type="button"{disabled_attr}>Refresh</button>
  <span id="dashboard-refresh-status" role="status">{_escape(status)}</span>
  <time id="dashboard-last-refreshed" datetime="{_escape(rendered_at)}">Last refreshed: {_escape(rendered_at)}</time>
</div>"""


def _render_workspace_selector(payload: Mapping[str, Any]) -> str:
    workflows = _sequence(payload.get("workflows"))
    workspace = _mapping(payload.get("workspace"))
    snapshots = _mapping(payload.get("workflow_snapshots"))
    selected_id = _text(workspace.get("selected_workflow_id") or payload.get("workflow_id"))
    if not workflows:
        return """<section class="workflow-selector-shell" aria-labelledby="workflow-selector-title">
  <p class="eyebrow">Workflow History Selector</p>
  <h2 id="workflow-selector-title">No Workflows</h2>
  <p class="empty-state" id="workflow-selector-status">No workspace workflows are available.</p>
</section>"""
    options = []
    rows = []
    selected_record: Mapping[str, Any] = {}
    for raw_record in workflows:
        record = _mapping(raw_record)
        workflow_id = _text(record.get("workflow_id"))
        if not workflow_id:
            continue
        if workflow_id == selected_id:
            selected_record = record
        selected = " selected" if workflow_id == selected_id else ""
        disabled = ""
        label = _workflow_selector_label(record, snapshots=snapshots)
        options.append(
            f'<option value="{_escape(workflow_id)}"{selected}>{_escape(label)}</option>'
        )
        rows.append(_workflow_selector_row(record, snapshots=snapshots, selected=workflow_id == selected_id))
    if not selected_record and workflows:
        selected_record = _mapping(workflows[0])
    selector_status = _workflow_selector_status(selected_record, snapshots=snapshots, server_mode=bool(payload.get("server_mode")))
    toggle_html = (
        '<button id="workflow-history-toggle" class="workflow-history-toggle" type="button" '
        'aria-pressed="false" aria-controls="workflow-history-list">Show all workflows</button>'
        if len(options) > 1
        else ""
    )
    return f"""<section class="workflow-selector-shell" aria-labelledby="workflow-selector-title" data-workflow-history-mode="selected">
  <div class="workflow-selector-heading">
    <div>
      <p class="eyebrow">Workflow History Selector</p>
      <h2 id="workflow-selector-title">Workspace Workflows</h2>
    </div>
    <div class="workflow-selector-heading-actions">
      {toggle_html}
      <span class="workflow-count">{len(options)}</span>
    </div>
  </div>
  <label class="workflow-select-label" for="workflow-selector">Workflow</label>
  <select id="workflow-selector" name="workflow_id" autocomplete="off">
    {''.join(options)}
  </select>
  <p class="selector-status" id="workflow-selector-status" role="status">{_escape(selector_status)}</p>
  <ul class="workflow-history-list" id="workflow-history-list">
    {''.join(rows)}
  </ul>
</section>"""


def _workflow_selector_label(record: Mapping[str, Any], *, snapshots: Mapping[str, Any] | None = None) -> str:
    workflow_id = _text(record.get("workflow_id") or "")
    snapshot = _mapping(_mapping(snapshots or {}).get(workflow_id))
    read_models = _mapping(snapshot.get("read_models"))
    parts = [
        _workflow_display_title(workflow_id, workflow=record, read_models=read_models, snapshot=snapshot),
        _text(record.get("status") or "unknown"),
        workflow_id,
    ]
    badges = _workflow_badges(record)
    if badges:
        parts.append(", ".join(badges))
    return " - ".join(part for part in parts if part)


def _workflow_selector_row(
    record: Mapping[str, Any],
    *,
    snapshots: Mapping[str, Any],
    selected: bool,
) -> str:
    workflow_id = _text(record.get("workflow_id"))
    summary = _mapping(record.get("summary"))
    snapshot = _mapping(snapshots.get(workflow_id))
    read_models = _mapping(snapshot.get("read_models"))
    display_title = _workflow_display_title(workflow_id, workflow=record, read_models=read_models, snapshot=snapshot)
    registry_name = _text(record.get("name") or "")
    registry_name_row = (
        _detail_row("Registry Name", registry_name)
        if registry_name and registry_name != "none" and registry_name not in {display_title, workflow_id}
        else ""
    )
    badges = _workflow_badges(record)
    if snapshot and not snapshot.get("ok"):
        badges.append("unavailable")
    badge_html = "".join(f'<span class="workflow-badge">{_escape(badge)}</span>' for badge in badges)
    selected_text = " selected" if selected else ""
    return f"""<li class="workflow-history-row{selected_text}" data-workflow-id="{_escape(workflow_id)}">
  <div>
    <strong>{_escape(display_title)}</strong>
    <small>{_escape(workflow_id)}</small>
  </div>
  <dl>
    {_detail_row("Status", record.get("status") or "unknown")}
    {registry_name_row}
    {_detail_row("Created", record.get("created_at") or "unknown")}
    {_detail_row("Last Seen", record.get("last_seen_at") or "unknown")}
    {_detail_row("Progress", _workflow_progress_label(summary))}
  </dl>
  <p>{_escape(_text(summary.get("one_line") or "No workflow summary available."))}</p>
  <div class="workflow-badges">{badge_html}</div>
</li>"""


def _workflow_selector_status(
    record: Mapping[str, Any],
    *,
    snapshots: Mapping[str, Any],
    server_mode: bool,
) -> str:
    workflow_id = _text(record.get("workflow_id"))
    snapshot = _mapping(snapshots.get(workflow_id))
    if snapshot and not snapshot.get("ok"):
        return f"{workflow_id} is listed in the workspace registry, but its read models are unavailable."
    mode_label = "API" if server_mode else "static read-model snapshot"
    return f"Viewing {workflow_id or 'workflow'} from {mode_label}. Selection does not update current_workflow.json."


def _workflow_badges(record: Mapping[str, Any]) -> list[str]:
    badges: list[str] = []
    if record.get("read_only") is True:
        badges.append("read-only")
    if record.get("archived") is True:
        badges.append("archived")
    return badges


def _workflow_progress_label(summary: Mapping[str, Any]) -> str:
    total = summary.get("tasks_total")
    completed = summary.get("tasks_completed")
    blocked = summary.get("tasks_blocked")
    parts = []
    if completed is not None or total is not None:
        parts.append(f"{completed or 0}/{total or 0}")
    if blocked:
        parts.append(f"{blocked} blocked")
    return ", ".join(parts) if parts else "unknown"


def _render_freshness_banner(
    freshness: Mapping[str, Any],
    read_model_rebuild: Mapping[str, Any],
    *,
    server_mode: bool,
) -> str:
    status = str(freshness.get("status") or "")
    if status not in {"stale", "unknown"}:
        return ""
    warnings = _sequence(freshness.get("warnings"))
    problem_files = [
        _text(_mapping(warning).get("file"))
        for warning in warnings
        if _mapping(warning).get("code") in {"read_model_stale", "read_model_metadata_missing"}
        and _mapping(warning).get("file")
    ]
    file_label = ", ".join(problem_files[:5])
    if len(problem_files) > 5:
        file_label = f"{file_label}, and {len(problem_files) - 5} more"
    read_model = _mapping(freshness.get("read_model"))
    event_log = _mapping(freshness.get("event_log"))
    rebuild_in_progress = _read_model_rebuild_in_progress(read_model_rebuild)
    live_refresh_expected = _read_model_live_refresh_expected(
        freshness,
        read_model_rebuild,
        server_mode=server_mode,
        rebuild_in_progress=rebuild_in_progress,
    )
    if rebuild_in_progress:
        eyebrow = "Rebuild In Progress"
        title = "Read Models Are Rebuilding"
        summary = _text(
            read_model_rebuild.get("in_progress_summary")
            or "A read-model rebuild is already queued or running. Dashboard data may lag until it finishes; refresh shortly."
        )
        aria_label = "Read model rebuild in progress"
    elif live_refresh_expected:
        eyebrow = "Live Refresh In Progress"
        title = "Read Models Are Refreshing"
        summary = "The live dashboard is rebuilding its derived read models now. This page will update automatically when the refresh finishes."
        aria_label = "Read model live refresh in progress"
    else:
        eyebrow = "Freshness Warning"
        title = "Read Models May Be Stale" if status == "stale" else "Read Model Freshness Needs Rebuild"
        summary = _text(freshness.get("summary") or "Read models require a rebuild before dashboard status can be trusted.")
        aria_label = "Read model freshness warning"
    warning_items = []
    for warning in warnings[:6]:
        item = _mapping(warning)
        message = _text(item.get("message") or item.get("code"))
        if message:
            warning_items.append(f"<li>{_escape(message)}</li>")
    warning_list = f'<ul class="freshness-warning-list">{"".join(warning_items)}</ul>' if warning_items else ""
    return f"""<section class="freshness-banner" data-freshness-status="{_escape(status)}" data-rebuild-in-progress="{str(rebuild_in_progress).lower()}" role="status" aria-label="{_escape(aria_label)}">
  <div>
    <p class="eyebrow">{_escape(eyebrow)}</p>
    <h2>{_escape(title)}</h2>
    <p>{_escape(summary)}</p>
    <dl class="freshness-facts">
      {_detail_row("Read Model Event", _event_ref_label(read_model))}
      {_detail_row("Event Log Head", _event_ref_label(event_log))}
      {_detail_row("Affected Files", file_label or "unknown")}
    </dl>
    {warning_list}
  </div>
  <div class="rebuild-hint">
    <span>Rebuild command</span>
    <code>{_escape(_text(freshness.get("rebuild_command") or "loopplane rebuild-read-models"))}</code>
    {_render_read_model_rebuild_form(read_model_rebuild, server_mode=server_mode, live_refresh_expected=live_refresh_expected)}
  </div>
</section>"""


def _render_read_model_rebuild_form(
    read_model_rebuild: Mapping[str, Any],
    *,
    server_mode: bool,
    live_refresh_expected: bool = False,
) -> str:
    endpoint = _text(read_model_rebuild.get("endpoint") or "")
    blockers = [str(blocker) for blocker in _sequence(read_model_rebuild.get("mutation_blockers")) if str(blocker)]
    mutation_allowed = read_model_rebuild.get("mutation_allowed") is True
    request_allowed = read_model_rebuild.get("request_allowed") is not False
    rebuild_in_progress = _read_model_rebuild_in_progress(read_model_rebuild)
    disabled = not server_mode or not mutation_allowed or not request_allowed or rebuild_in_progress or live_refresh_expected
    disabled_attr = " disabled" if disabled else ""
    if rebuild_in_progress:
        button_label = "Rebuild Request Pending"
    elif live_refresh_expected:
        button_label = "Live Refresh Running"
    else:
        button_label = "Create Rebuild Request"
    command_html = "".join(
        f"<code>{_escape(_text(command))}</code>" for command in _sequence(read_model_rebuild.get("commands"))[1:2]
    )
    recent = _sequence(read_model_rebuild.get("recent"))
    recent_rows = []
    for raw_record in recent[-4:]:
        record = _mapping(raw_record)
        recent_rows.append(
            _status_feed_row(
                record.get("type") or "rebuild_read_models",
                record.get("status") or "pending",
                record.get("request_id") or "",
            )
        )
    if not recent_rows:
        recent_rows.append("<li><span>No rebuild requests recorded.</span></li>")
    return f"""<form id="read-model-rebuild-form" class="rebuild-request-form" data-endpoint="{_escape(endpoint)}">
  <label>Reason<input name="reason" value=""{disabled_attr}></label>
  <button type="submit" data-rebuild-action="rebuild_read_models"{disabled_attr}>{_escape(button_label)}</button>
  <p id="read-model-rebuild-status" class="selector-status" role="status">{_escape(_read_model_rebuild_status_message(server_mode=server_mode, mutation_allowed=mutation_allowed, blockers=blockers, rebuild_in_progress=rebuild_in_progress, live_refresh_expected=live_refresh_expected))}</p>
  <dl class="detail-list compact-detail-list">
    {_detail_row("Pending", read_model_rebuild.get("pending_count", 0))}
    {_detail_row("Latest", _latest_rebuild_request_label(read_model_rebuild))}
    {_detail_row("Request Path", read_model_rebuild.get("requests_path"))}
  </dl>
  <div class="attention-commands">{command_html}</div>
  <ol class="feed-list compact-feed">{''.join(recent_rows)}</ol>
</form>"""


def _read_model_rebuild_status_message(
    *,
    server_mode: bool,
    mutation_allowed: bool,
    blockers: Sequence[str],
    rebuild_in_progress: bool,
    live_refresh_expected: bool,
) -> str:
    if rebuild_in_progress:
        return "A read-model rebuild is already queued or running. Wait for the runtime to finish, then refresh the dashboard."
    if live_refresh_expected:
        return "The live dashboard is refreshing read models now. Wait for the page to update before requesting another rebuild."
    if not server_mode:
        return "Static dashboard is read-only; open server mode to create a rebuild request record."
    if not mutation_allowed:
        return f"Read-model rebuild requests are disabled for this workflow: {', '.join(blockers) or 'not mutable'}."
    return "This records a read-model rebuild request. Dashboard data will update after the runtime processes it."


def _latest_rebuild_request_label(read_model_rebuild: Mapping[str, Any]) -> str:
    request_id = read_model_rebuild.get("latest_request_id")
    if not request_id:
        return "none"
    return " ".join(
        _text(part)
        for part in ("rebuild_read_models", read_model_rebuild.get("latest_status"), request_id)
        if part
    )


def _render_attention_banner(workflow_status: Mapping[str, Any]) -> str:
    attention = _sequence(workflow_status.get("requires_attention"))
    if not attention:
        return ""
    rows: list[str] = []
    for raw_item in attention[:8]:
        item = _mapping(raw_item)
        command_values = _sequence(item.get("commands"))
        command_html = ""
        if command_values:
            command_html = '<div class="attention-commands">' + "".join(
                f"<code>{_escape(_text(command))}</code>" for command in command_values
            ) + "</div>"
        rows.append(
            f"""<li>
  <strong>{_escape(_text(item.get("type") or "requires_attention"))}</strong>
  <span>{_escape(_text(item.get("message") or "Runtime requires attention."))}</span>
  <small>{_escape(_text(item.get("request_id") or item.get("task_id") or ""))}</small>
  {command_html}
</li>"""
        )
    overflow = ""
    if len(attention) > 8:
        overflow = f"<li><span>{len(attention) - 8} more requires-attention item(s).</span></li>"
    return f"""<section class="attention-banner" role="status" aria-label="Requires attention">
  <div>
    <p class="eyebrow">Requires Attention</p>
    <h2>Workflow Needs Operator Action</h2>
  </div>
  <ul>{''.join(rows)}{overflow}</ul>
</section>"""


def _approval_metric_label(approval_controls: Mapping[str, Any]) -> str:
    pending = approval_controls.get("pending_count", 0)
    try:
        pending_count = int(pending)
    except (TypeError, ValueError):
        pending_count = 0
    if pending_count:
        return f"{pending_count} pending"
    closed = sum(
        int(approval_controls.get(key) or 0)
        for key in ("approved_count", "rejected_count", "expired_count", "superseded_count")
    )
    return f"0 pending, {closed} closed" if closed else "none pending"


def _render_approval_top_alert(approval_controls: Mapping[str, Any]) -> str:
    try:
        pending_count = int(approval_controls.get("pending_count") or 0)
    except (TypeError, ValueError):
        pending_count = 0
    if pending_count <= 0:
        return ""
    return f"""<div class="approval-top-alert" data-pending-approvals="{pending_count}" role="status">
  <strong>{pending_count} pending approval request{'s' if pending_count != 1 else ''}</strong>
  <span>Review the Approval Panel before continuing workflow execution.</span>
</div>"""


def _render_approval_panel(approval_controls: Mapping[str, Any], *, server_mode: bool) -> str:
    pending = _sequence(approval_controls.get("pending"))
    recent = _sequence(approval_controls.get("recent"))
    commands = _sequence(approval_controls.get("commands"))
    blockers = [str(blocker) for blocker in _sequence(approval_controls.get("mutation_blockers")) if str(blocker)]
    mutation_allowed = approval_controls.get("mutation_allowed") is True
    disabled = not server_mode or not mutation_allowed
    status_message = _approval_panel_status_message(
        server_mode=server_mode,
        mutation_allowed=mutation_allowed,
        blockers=blockers,
    )
    pending_html = "".join(
        _render_approval_card(_mapping(record), disabled=disabled, pending=True)
        for record in pending
    )
    if not pending_html:
        pending_html = '<p class="empty-state">No pending approvals.</p>'
    recent_records = recent[-8:]
    recent_html = "".join(
        _render_approval_card(_mapping(record), disabled=True, pending=False)
        for record in recent_records
    )
    if not recent_html:
        recent_html = '<p class="empty-state">No approval history recorded.</p>'
    command_html = "".join(f"<code>{_escape(_text(command))}</code>" for command in commands[:8])
    return f"""<div class="approval-summary" data-mutation-allowed="{_escape(str(mutation_allowed).lower())}">
  <p class="selector-status" id="approval-panel-status" role="status">{_escape(status_message)}</p>
  <dl class="detail-list">
    {_detail_row("Pending", approval_controls.get("pending_count", 0))}
    {_detail_row("Approved", approval_controls.get("approved_count", 0))}
    {_detail_row("Rejected", approval_controls.get("rejected_count", 0))}
    {_detail_row("Expired", approval_controls.get("expired_count", 0))}
    {_detail_row("Requests", approval_controls.get("requests_path"))}
    {_detail_row("Responses", approval_controls.get("responses_path"))}
  </dl>
  <div class="attention-commands">{command_html}</div>
  <div class="approval-list" id="approval-pending-list">
    <h3>Pending Approvals</h3>
    {pending_html}
  </div>
  <div class="approval-list" id="approval-recent-list">
    <h3>Recent Approval History</h3>
    {recent_html}
  </div>
</div>"""


def _approval_panel_status_message(*, server_mode: bool, mutation_allowed: bool, blockers: Sequence[str]) -> str:
    if not server_mode:
        return "Static dashboard is read-only; open server mode to approve or reject pending requests."
    if not mutation_allowed:
        return f"Approval responses are disabled for this workflow: {', '.join(blockers) or 'not mutable'}."
    return "Approve or reject pending human approval requests. Responses append records only; the scheduler observes them on its next tick."


def _render_approval_card(record: Mapping[str, Any], *, disabled: bool, pending: bool) -> str:
    approval_id = _text(record.get("approval_id") or "approval")
    status = _text(record.get("status") or "unknown")
    refs = _sequence(record.get("evidence_refs"))
    refs_html = "".join(f"<li>{_escape(_text(ref))}</li>" for ref in refs[:8])
    refs_block = f'<ul class="approval-ref-list">{refs_html}</ul>' if refs_html else '<p class="empty-state">No evidence or source paths recorded.</p>'
    response = _mapping(record.get("response"))
    response_detail = ""
    if response:
        response_detail = f"""<dl class="detail-list approval-response-detail">
    {_detail_row("Decision", response.get("decision"))}
    {_detail_row("Responder", response.get("approved_by"))}
    {_detail_row("Responded", response.get("responded_at"))}
    {_detail_row("Notes", response.get("notes"))}
  </dl>"""
    form = _render_approval_response_form(record, disabled=disabled) if pending else ""
    return f"""<article class="approval-card" {_status_attrs(status)} data-approval-id="{_escape(approval_id)}">
  <div class="approval-card-heading">
    <strong>{_escape(approval_id)}</strong>
    {_status_pill(status)}
  </div>
  <p>{_escape(_text(record.get("message") or "Approval requested."))}</p>
  <dl class="detail-list">
    {_detail_row("Type", record.get("type"))}
    {_detail_row("Task", record.get("task_id"))}
    {_detail_row("Run", record.get("run_id"))}
    {_detail_row("Scope", record.get("scope"))}
    {_detail_row("Created", record.get("created_at") or record.get("requested_at"))}
    {_detail_row("Expires", record.get("expires_at"))}
    {_detail_row("Source", record.get("source"))}
  </dl>
  <div class="approval-refs">
    <h4>Evidence And Source Paths</h4>
    {refs_block}
  </div>
  {response_detail}
  {form}
</article>"""


def _render_approval_response_form(record: Mapping[str, Any], *, disabled: bool) -> str:
    disabled_attr = " disabled" if disabled else ""
    endpoint = _text(record.get("respond_endpoint") or "")
    approval_id = _text(record.get("approval_id") or "")
    return f"""<form class="approval-response-form" data-approval-id="{_escape(approval_id)}" data-endpoint="{_escape(endpoint)}">
  <div class="form-grid">
    <label>Scope<input name="scope" value="{_escape(record.get("scope") or "")}"{disabled_attr}></label>
    <label>Notes<textarea name="notes" rows="3"{disabled_attr}></textarea></label>
  </div>
  <div class="approval-action-grid">
    <button type="submit" data-approval-decision="approved"{disabled_attr}>Approve</button>
    <button type="submit" data-approval-decision="rejected"{disabled_attr}>Reject</button>
  </div>
  <p class="selector-status approval-response-status" role="status"></p>
</form>"""


def _render_control_panel(
    workflow_status: Mapping[str, Any],
    planning_controls: Mapping[str, Any],
    execution_controls: Mapping[str, Any],
    *,
    server_mode: bool,
) -> str:
    control = execution_controls if execution_controls else _mapping(workflow_status.get("control"))
    recent = _sequence(control.get("recent"))
    rows = []
    for raw_record in recent[-6:]:
        record = _mapping(raw_record)
        rows.append(
            _status_feed_row(
                record.get("type") or "control",
                record.get("status") or "unknown",
                record.get("request_id") or "",
            )
        )
    if not rows:
        rows.append('<li><span>No control requests recorded.</span></li>')
    return f"""<div class="control-stack">
  {_render_planning_controls(planning_controls, server_mode=server_mode)}
  {_render_execution_controls(control, recent_rows=rows, server_mode=server_mode)}
</div>"""


def _render_execution_controls(
    execution_controls: Mapping[str, Any],
    *,
    recent_rows: Sequence[str],
    server_mode: bool,
) -> str:
    endpoints = _mapping(execution_controls.get("endpoints"))
    blockers = [str(blocker) for blocker in _sequence(execution_controls.get("mutation_blockers")) if str(blocker)]
    mutation_allowed = execution_controls.get("mutation_allowed") is True
    disabled = not server_mode or not mutation_allowed
    disabled_attr = " disabled" if disabled else ""
    status_message = _execution_control_status_message(
        server_mode=server_mode,
        mutation_allowed=mutation_allowed,
        blockers=blockers,
    )
    command_html = "".join(
        f"<code>{_escape(_text(command))}</code>" for command in _sequence(execution_controls.get("commands"))[:8]
    )
    buttons = "".join(
        f'<button type="submit" data-control-action="{action}" data-endpoint="{_escape(_text(endpoints.get(action)))}"{disabled_attr}>{label}</button>'
        for action, label in (
            ("start", "Start"),
            ("pause", "Pause"),
            ("resume", "Resume"),
            ("stop", "Stop"),
        )
    )
    return f"""<div class="execution-controls" data-mutation-allowed="{_escape(str(mutation_allowed).lower())}">
  <h3>Execution Requests</h3>
  <p class="selector-status" id="execution-control-status" role="status">{_escape(status_message)}</p>
  <form id="execution-control-form" class="execution-control-form">
    <div class="form-grid">
      <label>Reason<input name="reason" value=""{disabled_attr}></label>
    </div>
    <div class="execution-action-grid">
      {buttons}
    </div>
  </form>
  <dl class="detail-list">
    {_detail_row("Runtime", execution_controls.get("runtime_status") or execution_controls.get("status"))}
    {_detail_row("Pending", execution_controls.get("pending_count", 0))}
    {_detail_row("Applied", execution_controls.get("applied_count", 0))}
    {_detail_row("Rejected", execution_controls.get("rejected_count", 0))}
    {_detail_row("Latest", _latest_control_label(execution_controls))}
    {_detail_row("Request Path", execution_controls.get("requests_path"))}
  </dl>
  <div class="attention-commands">{command_html}</div>
  <ol class="feed-list compact-feed">{''.join(recent_rows)}</ol>
</div>"""


def _execution_control_status_message(*, server_mode: bool, mutation_allowed: bool, blockers: Sequence[str]) -> str:
    if not server_mode:
        return "Static dashboard is read-only; open server mode to create start, pause, resume, or stop request records."
    if not mutation_allowed:
        return f"Execution controls are disabled for this workflow: {', '.join(blockers) or 'not mutable'}."
    return "Creates start, pause, resume, and stop request records only; the scheduler or detached supervisor applies them at safe points."


def _render_planning_controls(planning_controls: Mapping[str, Any], *, server_mode: bool) -> str:
    endpoints = _mapping(planning_controls.get("endpoints"))
    recent = _sequence(planning_controls.get("recent"))
    blockers = [str(blocker) for blocker in _sequence(planning_controls.get("mutation_blockers")) if str(blocker)]
    mutation_allowed = planning_controls.get("mutation_allowed") is True
    disabled = not server_mode or not mutation_allowed
    disabled_attr = " disabled" if disabled else ""
    status_message = _planning_control_status_message(
        server_mode=server_mode,
        mutation_allowed=mutation_allowed,
        blockers=blockers,
    )
    rows = []
    for raw_record in recent[-6:]:
        record = _mapping(raw_record)
        rows.append(
            _status_feed_row(
                _planning_request_label(record.get("type")),
                record.get("status") or "pending",
                record.get("request_id") or "",
            )
        )
    if not rows:
        rows.append('<li><span>No planning requests recorded.</span></li>')
    return f"""<div class="planning-controls" data-mutation-allowed="{_escape(str(mutation_allowed).lower())}">
  <h3>Planning Controls</h3>
  <p class="selector-status" id="planning-control-status" role="status">{_escape(status_message)}</p>
  <form id="planning-control-form" class="planning-control-form">
    <div class="form-grid">
      <label>Planner Runner<input name="planner_runner_id" value="planner"{disabled_attr}></label>
      <label>Auditor Runner<input name="auditor_runner_id" value="auditor"{disabled_attr}></label>
      <label>Activation Source<input name="activation_source" value="PLAN_DRAFT.md"{disabled_attr}></label>
      <label>Reason<input name="reason" value=""{disabled_attr}></label>
    </div>
    <div class="planning-action-grid">
      <button type="submit" data-planning-action="plan" data-endpoint="{_escape(_text(endpoints.get("plan")))}"{disabled_attr}>Run Planner</button>
      <button type="submit" data-planning-action="audit" data-endpoint="{_escape(_text(endpoints.get("audit")))}"{disabled_attr}>Run Auditor</button>
      <button type="submit" data-planning-action="activate_plan" data-endpoint="{_escape(_text(endpoints.get("activate_plan")))}"{disabled_attr}>Activate Plan</button>
    </div>
  </form>
  <dl class="detail-list">
    {_detail_row("Pending", planning_controls.get("pending_count", 0))}
    {_detail_row("Latest", _latest_planning_label(planning_controls))}
    {_detail_row("Request Path", planning_controls.get("requests_path"))}
  </dl>
  <ol class="feed-list compact-feed">{''.join(rows)}</ol>
</div>"""


def _planning_control_status_message(*, server_mode: bool, mutation_allowed: bool, blockers: Sequence[str]) -> str:
    if not server_mode:
        return "Static dashboard is read-only; open server mode to create planner, auditor, or activation request records."
    if not mutation_allowed:
        return f"Planning controls are disabled for this workflow: {', '.join(blockers) or 'not mutable'}."
    return "Creates dashboard request records only; planner, auditor, and activation work is applied by LoopPlane runtime commands."


def _planning_request_label(value: Any) -> str:
    request_type = str(value or "")
    return {
        "plan": "planner",
        "audit": "auditor",
        "activate_plan": "activate plan",
    }.get(request_type, request_type or "planning")


def _latest_planning_label(planning_controls: Mapping[str, Any]) -> str:
    request_id = planning_controls.get("latest_request_id")
    request_type = planning_controls.get("latest_type")
    status = planning_controls.get("latest_status")
    if not request_id:
        return "none"
    return " ".join(_text(part) for part in (_planning_request_label(request_type), status, request_id) if part)


def _render_runner_panel(runner_configuration: Mapping[str, Any], *, server_mode: bool) -> str:
    runners = _sequence(runner_configuration.get("runners"))
    trusted_local = runner_configuration.get("trusted_local_mode") is True
    if runner_configuration.get("ok") is False:
        errors = _sequence(runner_configuration.get("errors"))
        return f"""<div class="runner-summary" data-trusted-local="false">
  <p class="empty-state">{_escape('; '.join(_text(error) for error in errors) or 'Runner configuration is unavailable.')}</p>
</div>"""
    rows = "".join(_render_runner_card(_mapping(runner)) for runner in runners)
    if not rows:
        rows = '<p class="empty-state">No configured runners were found.</p>'
    mode_label = "enabled" if trusted_local else "disabled"
    request_form = (
        _render_runner_request_form(runners, default_runner=runner_configuration.get("default_runner"))
        if trusted_local and server_mode
        else ""
    )
    static_note = ""
    if trusted_local and not server_mode:
        static_note = '<p class="selector-status">Trusted local mode is enabled; open the server dashboard to apply runner settings.</p>'
    if not trusted_local:
        static_note = '<p class="selector-status">Trusted local mode is disabled; commands are hidden and browser configuration changes are unavailable.</p>'
    return f"""<div class="runner-summary" data-trusted-local="{_escape(str(trusted_local).lower())}">
  <dl class="detail-list">
    {_detail_row("Trusted Local", mode_label)}
    {_detail_row("Default", runner_configuration.get("default_runner"))}
    {_detail_row("Runners", runner_configuration.get("runner_count", len(runners)))}
    {_detail_row("Config", runner_configuration.get("config_path"))}
  </dl>
  {static_note}
  <div class="runner-list">{rows}</div>
  {request_form}
</div>"""


def _render_runner_card(runner: Mapping[str, Any]) -> str:
    doctor = _mapping(runner.get("doctor"))
    diagnostics = _sequence(doctor.get("diagnostics"))
    diagnostic_html = "".join(f"<li>{_escape(_text(item))}</li>" for item in diagnostics[:4])
    runner_status = "enabled" if runner.get("enabled") is True else "disabled"
    return f"""<article class="runner-card" data-runner-id="{_escape(_text(runner.get("runner_id")))}" {_status_attrs(runner_status)}>
  <div class="runner-card-heading">
    <strong>{_escape(_text(runner.get("runner_id") or "runner"))}</strong>
    {_status_pill(runner_status)}
  </div>
    <dl class="detail-list">
    {_detail_row("Role", runner.get("role"))}
    {_detail_row("Adapter", runner.get("adapter"))}
    {_detail_row("Command", runner.get("command"))}
    {_detail_row("Model", runner.get("model") or "default")}
    {_detail_row("Effort", runner.get("reasoning_effort") or "default")}
    {_detail_row("Prompt", runner.get("prompt_delivery_mode"))}
    {_detail_row("Timeout", _runner_timeout_label(runner.get("timeout_seconds")))}
    {_detail_row("Doctor", doctor.get("status"))}
  </dl>
  <ul class="runner-diagnostics">{diagnostic_html}</ul>
</article>"""


def _render_runner_request_form(runners: Sequence[Any], *, default_runner: Any = None) -> str:
    options = []
    runner_records: list[Mapping[str, Any]] = []
    selected: Mapping[str, Any] = {}
    default_runner_id = _text(default_runner)
    for raw_runner in runners:
        runner = _mapping(raw_runner)
        runner_id = _text(runner.get("runner_id"))
        if not runner_id:
            continue
        runner_records.append(runner)
        if not selected and runner_id == default_runner_id:
            selected = runner
    if not runner_records:
        return ""
    if not selected:
        selected = runner_records[0]
    selected_runner_id = _text(selected.get("runner_id"))
    for runner in runner_records:
        runner_id = _text(runner.get("runner_id"))
        selected_attr = " selected" if runner_id == selected_runner_id else ""
        options.append(f'<option value="{_escape(runner_id)}"{selected_attr}>{_escape(runner_id)}</option>')
    return f"""<form id="runner-config-request-form" class="runner-config-form">
  <div class="form-grid">
    <label>Runner<select name="runner_id">{''.join(options)}</select></label>
    <label>Role<input name="role" value="{_escape(selected.get("role") or "")}"></label>
    <label>Adapter<input name="adapter" value="{_escape(selected.get("adapter") or "")}"></label>
    <label>Command<input name="command" value=""></label>
    <label>Model<input name="model" value="{_escape(selected.get("model") or "")}" placeholder="default"></label>
    <label>Effort<select name="reasoning_effort">{_reasoning_effort_options(selected.get("reasoning_effort"))}</select></label>
    <label>Prompt<select name="prompt_delivery_mode">{_prompt_delivery_options(selected.get("prompt_delivery_mode"))}</select></label>
    <label>Timeout<input name="timeout_seconds" type="number" min="1" value="{_escape(selected.get("timeout_seconds") or "")}"></label>
  </div>
  <button type="submit">Apply Runner Settings</button>
  <p id="runner-config-request-status" class="selector-status" role="status"></p>
</form>"""


def _prompt_delivery_options(selected: Any) -> str:
    selected_text = _text(selected)
    return "".join(
        f'<option value="{_escape(mode)}"{" selected" if mode == selected_text else ""}>{_escape(mode)}</option>'
        for mode in sorted(PROMPT_DELIVERY_MODES)
    )


def _reasoning_effort_options(selected: Any) -> str:
    selected_text = _text(selected)
    options = [("", "default"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh")]
    return "".join(
        f'<option value="{_escape(value)}"{" selected" if value == selected_text else ""}>{_escape(label)}</option>'
        for value, label in options
    )


def _runner_timeout_label(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return _text(value)
    if seconds <= 0:
        return str(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{seconds}s ({hours}h{f' {minutes}m' if minutes else ''} safety ceiling)"
    return f"{seconds}s safety ceiling"


def _render_change_request_panel(change_requests: Mapping[str, Any]) -> str:
    recent = _sequence(change_requests.get("recent"))
    commands = _sequence(change_requests.get("commands"))
    rows = []
    for raw_record in recent[-6:]:
        record = _mapping(raw_record)
        rows.append(
            _status_feed_row(
                record.get("change_request_id") or "change_request",
                record.get("status") or "unknown",
                record.get("user_request") or "",
            )
        )
    if not rows:
        rows.append('<li><span>No change requests recorded.</span></li>')
    command_html = "".join(f"<code>{_escape(_text(command))}</code>" for command in commands[:6])
    return f"""<div class="control-summary">
  <dl class="detail-list">
    {_detail_row("Pending Review", change_requests.get("pending_review_count", 0))}
    {_detail_row("Needs Approval", change_requests.get("needs_user_approval_count", 0))}
    {_detail_row("Approved", change_requests.get("approved_count", 0))}
    {_detail_row("Applied", change_requests.get("applied_count", 0))}
    {_detail_row("Latest", change_requests.get("latest_change_request_id") or "none")}
  </dl>
  <div class="attention-commands">{command_html}</div>
  <ol class="feed-list compact-feed">{''.join(rows)}</ol>
</div>"""


def _render_inspector_console(
    console: Mapping[str, Any],
    *,
    fallback_change_requests: Mapping[str, Any],
    server_mode: bool,
    execution_controls_html: str = "",
) -> str:
    endpoints = _mapping(console.get("endpoints"))
    blockers = [str(blocker) for blocker in _sequence(console.get("mutation_blockers")) if str(blocker)]
    chat_blockers = [str(blocker) for blocker in _sequence(console.get("chat_blockers")) if str(blocker)]
    mutation_allowed = console.get("mutation_allowed") is True
    chat_allowed = console.get("chat_allowed") is True
    change_allowed = console.get("change_request_allowed") is True
    chat_disabled = not server_mode or not chat_allowed
    change_disabled = not server_mode or not change_allowed
    disabled_chat_attr = " disabled" if chat_disabled else ""
    disabled_change_attr = " disabled" if change_disabled else ""
    chat_rows = _render_inspector_chat_rows(_sequence(console.get("recent_chat")))
    latest_chat_html = _render_latest_inspector_chat(_mapping(console.get("latest_chat")))
    change_records = _sequence(console.get("recent_change_requests")) or _sequence(fallback_change_requests.get("recent"))
    change_rows = _render_inspector_change_rows(change_records)
    commands = _sequence(console.get("commands")) or _sequence(fallback_change_requests.get("commands"))
    command_html = "".join(f"<code>{_escape(_text(command))}</code>" for command in commands[:6])
    context_paths = _sequence(console.get("context_paths")) or _sequence(console.get("allowed_paths"))
    context_path_rows = "".join(f"<li>{_escape(_text(path))}</li>" for path in context_paths)
    if not context_path_rows:
        context_path_rows = "<li>none</li>"
    return f"""<div class="inspector-console" data-mutation-allowed="{_escape(str(mutation_allowed).lower())}">
  <p class="selector-status" id="inspector-console-status" role="status">{_escape(_inspector_console_status_message(server_mode=server_mode, mutation_allowed=mutation_allowed, chat_allowed=chat_allowed, blockers=blockers, chat_blockers=chat_blockers))}</p>
  {latest_chat_html}
  <div class="inspector-action-grid">
    <form id="inspector-chat-form" class="inspector-chat-form" data-endpoint="{_escape(_text(endpoints.get("chat")))}">
      <h3>Full Agent Inspector</h3>
      <input type="hidden" name="runner_id" value="{_escape(_text(console.get("runner_id") or "inspector"))}">
      <label>Question<textarea name="message" rows="4"{disabled_chat_attr}></textarea></label>
      <button type="submit"{disabled_chat_attr}>Ask Inspector</button>
      <p id="inspector-chat-status" class="selector-status" role="status"></p>
    </form>
    <form id="change-request-form" class="change-request-form" data-endpoint="{_escape(_text(endpoints.get("change_request")))}">
      <h3>Change Request</h3>
      <label>Request<textarea name="user_request" rows="4"{disabled_change_attr}></textarea></label>
      <button type="submit"{disabled_change_attr}>Create Change Request</button>
      <p id="change-request-status" class="selector-status" role="status"></p>
    </form>
  </div>
  <div class="inspector-meta-grid">
    <dl class="detail-list">
      {_detail_row("Mode", console.get("mode") or INSPECTION_MODE)}
      {_detail_row("Chat Requests", console.get("chat_count", 0))}
      {_detail_row("Chat Pending", console.get("chat_pending_count", 0))}
      {_detail_row("Change Requests", console.get("change_request_count", fallback_change_requests.get("total_count", 0)))}
      {_detail_row("Pending Review", console.get("pending_review_count", fallback_change_requests.get("pending_review_count", 0)))}
      {_detail_row("Latest Chat", _latest_inspector_chat_label(console))}
      {_detail_row("Latest Change", _latest_inspector_change_label(console))}
    </dl>
    <div class="inspector-allowed-paths">
      <h3>Context Paths</h3>
      <ul>{context_path_rows}</ul>
    </div>
    <div class="attention-commands">{command_html}</div>
  </div>
  <div class="inspector-history-grid">
    <div>
      <h3>Recent Chat</h3>
      <ol class="feed-list compact-feed">{chat_rows}</ol>
    </div>
    <div>
      <h3>Recent Change Requests</h3>
      <ol class="feed-list compact-feed">{change_rows}</ol>
    </div>
  </div>
  {f'<section class="inspector-embedded-controls"><h3>Execution Controls</h3>{execution_controls_html}</section>' if execution_controls_html else ''}
</div>"""


def _render_latest_inspector_chat(record: Mapping[str, Any]) -> str:
    if not record or not record.get("request_id"):
        return ""
    answer = _text(record.get("answer") or record.get("summary") or "No response recorded.")
    question = _text(record.get("user_message") or "")
    refs = "".join(f"<li>{_escape(_text(ref))}</li>" for ref in _sequence(record.get("refs"))[:5])
    technical = f"""<details class="technical-detail">
  <summary>Sources and request details</summary>
  <dl class="detail-list compact-detail-list">
    {_detail_row("Status", record.get("status"))}
    {_detail_row("Request", record.get("request_id"))}
    {_detail_row("Response", record.get("response_id"))}
  </dl>
  <ul>{refs or '<li>none</li>'}</ul>
</details>"""
    return f"""<section class="inspector-latest-answer">
  <h3>Latest Inspector Answer</h3>
  {f'<p class="inspector-question-text">{_escape(question)}</p>' if question else ''}
  <p class="inspector-answer-text">{_escape(answer)}</p>
  {technical}
</section>"""


def _render_inspector_chat_rows(records: Sequence[Any]) -> str:
    rows: list[str] = []
    for raw_record in records[-6:]:
        record = _mapping(raw_record)
        rows.append(
            _status_feed_row(
                record.get("request_id") or "chat_request",
                record.get("status") or "pending",
                record.get("answer") or record.get("summary") or record.get("user_message") or "",
            )
        )
    if not rows:
        rows.append("<li><span>No inspector chat records.</span></li>")
    return "".join(rows)


def _render_inspector_change_rows(records: Sequence[Any]) -> str:
    rows: list[str] = []
    for raw_record in records[-6:]:
        record = _mapping(raw_record)
        rows.append(
            _status_feed_row(
                record.get("change_request_id") or "change_request",
                record.get("status") or "pending_review",
                record.get("user_request") or "",
            )
        )
    if not rows:
        rows.append("<li><span>No change requests recorded.</span></li>")
    return "".join(rows)


def _inspector_console_status_message(
    *,
    server_mode: bool,
    mutation_allowed: bool,
    chat_allowed: bool,
    blockers: Sequence[str],
    chat_blockers: Sequence[str],
) -> str:
    if not server_mode:
        return "Static dashboard is read-only; open server mode to create inspector chat or change request records."
    if not mutation_allowed:
        return f"Inspector console is disabled for this workflow: {', '.join(blockers) or 'not mutable'}."
    if not chat_allowed:
        return f"Inspector chat is disabled: {', '.join(chat_blockers) or 'inspector unavailable'}. Change requests still append request records."
    return "Runs the configured inspector agent with full local access and shows the agent response here."


def _latest_inspector_chat_label(console: Mapping[str, Any]) -> str:
    request_id = console.get("latest_chat_request_id")
    status = console.get("latest_chat_status")
    if not request_id:
        return "none"
    return " ".join(_text(part) for part in (status, request_id) if part)


def _latest_inspector_change_label(console: Mapping[str, Any]) -> str:
    change_request_id = console.get("latest_change_request_id")
    status = console.get("latest_change_request_status")
    if not change_request_id:
        return "none"
    return " ".join(_text(part) for part in (status, change_request_id) if part)


def _parse_dashboard_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _first_dashboard_timestamp(record: Mapping[str, Any], keys: Sequence[str]) -> datetime | None:
    for key in keys:
        parsed = _parse_dashboard_timestamp(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _dashboard_timestamp_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_dashboard_time(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else _parse_dashboard_timestamp(value)
    if parsed is None:
        return "pending"
    local = parsed.astimezone()
    return local.strftime("%m-%d %H:%M")


def _duration_label(seconds: Any) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        return "pending"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _node_timing(node: Mapping[str, Any]) -> dict[str, str]:
    started_raw = _text(node.get("started_at") or "")
    ended_raw = _text(node.get("ended_at") or "")
    started = _first_dashboard_timestamp(
        node,
        ("started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"),
    )
    ended = _first_dashboard_timestamp(
        node,
        (
            "ended_at",
            "completed_at",
            "finished_at",
            "validated_at",
            "updated_at",
            "heartbeat_at",
            "ts",
            "timestamp",
        ),
    )
    if started is None and ended is not None:
        started = ended
    terminal = _is_terminal_dashboard_status(node.get("status"))
    active = _status_tier(node.get("status")) == "active" or node.get("active") is True
    if active:
        ended = None
    if ended is None and started is not None and terminal:
        ended = started
    elapsed_seconds = node.get("elapsed_seconds")
    if not isinstance(elapsed_seconds, int):
        elapsed_end = ended or (datetime.now(UTC) if active and started is not None else None)
        elapsed_seconds = max(0, int((elapsed_end - started).total_seconds())) if started and elapsed_end else None
    return {
        "started": started_raw or (_dashboard_timestamp_iso(started) if started else "pending"),
        "ended": "Present" if active and started else ended_raw or (_dashboard_timestamp_iso(ended) if ended else "pending"),
        "elapsed": _duration_label(elapsed_seconds),
    }


def _is_terminal_dashboard_status(value: Any) -> bool:
    return _status_value(value) in {
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


def _workflow_elapsed_label(
    payload: Mapping[str, Any],
    workflow_status: Mapping[str, Any],
    workflow_graph: Mapping[str, Any],
    feed: Sequence[Any],
) -> str:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for value in (
        workflow_status.get("started_at"),
        workflow_status.get("created_at"),
        payload.get("started_at"),
    ):
        parsed = _parse_dashboard_timestamp(value)
        if parsed is not None:
            starts.append(parsed)
    for value in (
        workflow_status.get("ended_at"),
        workflow_status.get("completed_at"),
        workflow_status.get("updated_at"),
    ):
        parsed = _parse_dashboard_timestamp(value)
        if parsed is not None:
            ends.append(parsed)
    for raw_node in _sequence(workflow_graph.get("nodes")):
        node = _mapping(raw_node)
        start = _first_dashboard_timestamp(
            node,
            ("started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"),
        )
        end = _first_dashboard_timestamp(
            node,
            (
                "ended_at",
                "completed_at",
                "finished_at",
                "validated_at",
                "updated_at",
                "heartbeat_at",
                "ts",
                "timestamp",
                "started_at",
            ),
        )
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    for raw_record in feed:
        record = _mapping(raw_record)
        timestamp = _first_dashboard_timestamp(record, ("ts", "created_at", "generated_at"))
        if timestamp:
            starts.append(timestamp)
            ends.append(timestamp)
    if not starts:
        return "unknown"
    started = min(starts)
    rendered = _parse_dashboard_timestamp(payload.get("rendered_at") or payload.get("generated_at")) or datetime.now(UTC)
    ended = max(ends) if _is_terminal_dashboard_status(workflow_status.get("status")) and ends else rendered
    return _duration_label(max(0, int((ended - started).total_seconds())))


def _render_plan_panel(
    plan_index: Mapping[str, Any],
    plan_markdown: Mapping[str, Any] | None = None,
    *,
    workflow_graph: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    phases = _sequence(plan_index.get("phases"))
    workflow_objectives = [
        _mapping(objective)
        for objective in _sequence(plan_index.get("objectives"))
        if _mapping(objective).get("scope") == "workflow"
    ]
    rendered_at = _parse_dashboard_timestamp(_mapping(payload).get("rendered_at")) or datetime.now(UTC)
    phase_timings = [
        _phase_timing(_mapping(phase), _sequence(_mapping(phase).get("tasks")), _mapping(workflow_graph), rendered_at)
        for phase in phases
    ]
    max_duration = max((timing.get("duration_seconds") or 0 for timing in phase_timings), default=0)
    checklist_blocks: list[str] = []
    if phases:
        for index, phase in enumerate(phases):
            tasks = _sequence(_mapping(phase).get("tasks"))
            task_rows = []
            for task in tasks:
                item = _mapping(task)
                display = _mapping(item.get("display"))
                status = _text(item.get("status") or "unknown")
                task_title = _render_human_summary_trigger(
                    _text(item.get("title") or "Untitled task"),
                    _mapping(item.get("human_summary")),
                    css_class="task-summary-link",
                )
                task_rows.append(
                    f"""<li class="task-row" {_status_attrs(status)}{_expanded_attr(item)}>
  {_status_pill(status)}
  <div>
    <strong>{_escape(_text(item.get("task_id") or "task"))}</strong>
    {_expansion_note_html(item, entity_label="Task")}
    <span>{task_title}</span>
    <small>{_escape(_text(item.get("validation_status") or display.get("subtitle") or "validation unknown"))}</small>
  </div>
</li>"""
                )
            phase_status = _text(_mapping(phase).get("status") or "unknown")
            phase_progress = _phase_progress(_mapping(phase), tasks)
            objective_rows = _sequence(_mapping(phase).get("objectives"))
            phase_title = _render_human_summary_trigger(
                _text(_mapping(phase).get("title") or "Unphased"),
                _mapping(_mapping(phase).get("human_summary")),
                css_class="phase-summary-link",
            )
            checklist_blocks.append(
                f"""<article class="phase-block" {_status_attrs(phase_status)}{_expanded_attr(_mapping(phase))}>
  <div class="phase-heading">
    <div class="phase-title-block">
      <h3>{phase_title}</h3>
      {_expansion_note_html(_mapping(phase), entity_label="Phase")}
    </div>
    {_status_pill(phase_status)}
  </div>
  {_render_phase_progress(phase_progress)}
  {_render_phase_timing(phase_timings[index], max_duration)}
  <ol class="task-list">
    {''.join(task_rows)}
  </ol>
  {_render_objective_list(objective_rows, title="Phase objectives")}
</article>"""
            )
    else:
        checklist_blocks.append('<p class="empty-state">No checklist tasks are present.</p>')
    return f"""<div class="plan-view-toggle" role="tablist" aria-label="Plan view">
  <button type="button" class="is-active" data-plan-view="checklist" aria-pressed="true">Checklist</button>
  <button type="button" data-plan-view="markdown" aria-pressed="false">Full Markdown</button>
</div>
<div id="plan-checklist-view" class="plan-view plan-checklist-view is-active" data-plan-view-panel="checklist">
  {"".join(checklist_blocks)}
  {_render_objective_list(workflow_objectives, title="Workflow objectives")}
</div>
<div id="plan-markdown-view" class="plan-view plan-markdown-view" data-plan-view-panel="markdown" hidden>
  {_render_plan_markdown_view(plan_markdown or {}, project_root=_text(_mapping(payload).get("project_root") or ""))}
</div>"""


def _render_objective_list(objectives: Sequence[Any], *, title: str) -> str:
    rows = [_mapping(objective) for objective in objectives]
    if not rows:
        return ""
    rendered_rows: list[str] = []
    for objective in rows:
        status = _text(objective.get("status") or objective.get("plan_status") or "needs_verification")
        result = _mapping(objective.get("result"))
        followup_task_value = objective.get("followup_tasks") or result.get("suggested_followup") or ""
        followup_phase_value = objective.get("followup_phases") or ""
        followup_tasks = _join_display_values(followup_task_value)
        followup_phases = _join_display_values(followup_phase_value)
        followup_parts = []
        if followup_tasks:
            followup_parts.append(f"tasks: {followup_tasks}")
        if followup_phases:
            followup_parts.append(f"phases: {followup_phases}")
        detail = f"follow-up: {'; '.join(followup_parts)}" if followup_parts else _text(objective.get("report_status") or result.get("verdict") or "verification pending")
        rendered_rows.append(
            f"""<li class="objective-row" {_status_attrs(status)}>
  {_status_pill(status)}
  <div>
    <strong>{_escape(_text(objective.get("objective_id") or "objective"))}</strong>
    <span>{_escape(_text(objective.get("text") or "Objective"))}</span>
    <small>{_escape(detail)}</small>
  </div>
</li>"""
        )
    return f"""<section class="objective-section">
  <div class="objective-section-heading"><strong>{_escape(title)}</strong><span>{len(rows)}</span></div>
  <ul class="objective-list">
    {''.join(rendered_rows)}
  </ul>
</section>"""


def _expansion_note_html(item: Mapping[str, Any], *, entity_label: str) -> str:
    if item.get("expanded") is not True:
        return ""
    label = f"{entity_label} added by self-expansion"
    return f'<small class="expansion-note" title="{_escape(label)}" aria-label="{_escape(label)}">Self-expansion</small>'


def _expanded_attr(item: Mapping[str, Any]) -> str:
    return ' data-expanded="true"' if item.get("expanded") is True else ""


def _join_display_values(value: Any) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(str(item) for item in value if str(item))
    return _text(value)


def _phase_progress(phase: Mapping[str, Any], tasks: Sequence[Any]) -> dict[str, Any]:
    status = _status_value(phase.get("status"))
    progress = _mapping(phase.get("progress"))
    raw_percent = _first_present(progress.get("progress_percent"), phase.get("progress_percent"), phase.get("percent_complete"))
    if isinstance(raw_percent, (int, float)):
        percent = max(0, min(100, int(round(float(raw_percent)))))
        return {"percent": percent, "label": f"{percent}% complete"}
    completed = _first_present(progress.get("completed_tasks"), progress.get("completed_count"), phase.get("completed_count"))
    total = _first_present(progress.get("total_tasks"), progress.get("task_count"), phase.get("task_count"))
    if isinstance(completed, int) and isinstance(total, int) and total > 0:
        percent = max(0, min(100, int(round((completed / total) * 100))))
        return {"percent": percent, "label": f"{completed}/{total} tasks"}
    if tasks:
        done = sum(1 for task in tasks if _status_tier(_mapping(task).get("status")) == "success")
        percent = max(0, min(100, int(round((done / len(tasks)) * 100))))
        return {"percent": percent, "label": f"{done}/{len(tasks)} tasks"}
    if _status_tier(status) == "success":
        return {"percent": 100, "label": "phase complete"}
    if _status_tier(status) == "active":
        return {"percent": 50, "label": "running"}
    return {"percent": 0, "label": "not started"}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _render_phase_progress(progress: Mapping[str, Any]) -> str:
    percent = progress.get("percent")
    if not isinstance(percent, int):
        percent = 0
    percent = max(0, min(100, percent))
    return f"""<div class="phase-progress" aria-label="Phase progress">
  <div class="phase-progress-label"><span>Progress</span><strong>{_escape(progress.get("label") or f"{percent}%")}</strong></div>
  <div class="phase-progress-track"><span class="phase-progress-bar" style="--phase-progress-pct: {_escape(percent)}%"></span></div>
</div>"""


def _phase_timing(
    phase: Mapping[str, Any],
    tasks: Sequence[Any],
    workflow_graph: Mapping[str, Any],
    rendered_at: datetime,
) -> dict[str, Any]:
    task_ids = {str(_mapping(task).get("task_id") or "") for task in tasks}
    task_ids.discard("")
    starts: list[datetime] = []
    ends: list[datetime] = []
    active = _status_tier(phase.get("status")) == "active"
    for raw_task in tasks:
        task = _mapping(raw_task)
        active = active or _status_tier(task.get("status")) == "active"
        start = _first_dashboard_timestamp(task, ("started_at", "created_at", "assigned_at", "last_updated_at"))
        end = _first_dashboard_timestamp(task, ("ended_at", "completed_at", "finished_at", "validated_at", "last_updated_at"))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    for raw_node in _sequence(workflow_graph.get("nodes")):
        node = _mapping(raw_node)
        if str(node.get("task_id") or "") not in task_ids:
            continue
        active = active or _status_tier(node.get("status")) == "active"
        start = _first_dashboard_timestamp(
            node,
            ("started_at", "prepared_at", "created_at", "ts", "timestamp", "heartbeat_at", "ended_at"),
        )
        end = _first_dashboard_timestamp(
            node,
            (
                "ended_at",
                "completed_at",
                "finished_at",
                "validated_at",
                "updated_at",
                "heartbeat_at",
                "ts",
                "timestamp",
                "started_at",
            ),
        )
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    if not starts and not ends:
        return {"available": False, "active": active}
    started = min(starts or ends)
    ended = None if active else max(ends) if ends else None
    duration_end = ended
    if duration_end is None and active:
        duration_end = rendered_at
    duration_seconds = max(0, int((duration_end - started).total_seconds())) if duration_end else None
    return {
        "available": True,
        "active": active,
        "started": started,
        "ended": ended,
        "duration_seconds": duration_seconds,
    }


def _render_phase_timing(timing: Mapping[str, Any], max_duration_seconds: int) -> str:
    if not timing.get("available"):
        return """<div class="phase-timing" data-phase-timing="pending">
  <div class="phase-timing-facts">
    <span><small>Start</small><strong>pending</strong></span>
    <span><small>End</small><strong>pending</strong></span>
    <span><small>Duration</small><strong>pending</strong></span>
  </div>
  <div class="phase-duration-track"><span class="phase-duration-bar" style="--phase-duration-pct: 0%"></span></div>
</div>"""
    duration = timing.get("duration_seconds")
    percent = 8
    active = bool(timing.get("active"))
    if not active and timing.get("ended"):
        percent = 100
    elif isinstance(duration, int) and duration > 0 and max_duration_seconds > 0:
        percent = max(8, min(100, round((duration / max_duration_seconds) * 100)))
    end_label = "Present" if active and not timing.get("ended") else _compact_dashboard_time(timing.get("ended"))
    return f"""<div class="phase-timing" data-phase-timing="{_escape('running' if active else 'recorded')}">
  <div class="phase-timing-facts">
    <span><small>Start</small><strong>{_escape(_compact_dashboard_time(timing.get("started")))}</strong></span>
    <span><small>End</small><strong>{_escape(end_label)}</strong></span>
    <span><small>Duration</small><strong>{_escape(_duration_label(duration))}</strong></span>
  </div>
  <div class="phase-duration-track"><span class="phase-duration-bar" style="--phase-duration-pct: {_escape(percent)}%"></span></div>
</div>"""


def _render_plan_markdown_view(plan_markdown: Mapping[str, Any], *, project_root: str = "") -> str:
    content = _text(plan_markdown.get("content") or "")
    path = _text(plan_markdown.get("path") or "PLAN.md")
    if not content:
        return f"""<div class="plan-markdown-meta">
  <strong>Full plan markdown</strong>
  <small>{_escape(path)} unavailable</small>
</div>
  <p class="empty-state">The plan markdown file could not be loaded.</p>
"""
    size = plan_markdown.get("size_bytes")
    size_label = f"{size} bytes" if isinstance(size, int) else "markdown"
    return f"""<div class="plan-markdown-meta">
  <strong>Full PLAN.md</strong>
  <small>{_escape(path)} · {_escape(size_label)}</small>
</div>
<div class="markdown-document">{_render_markdown_document(content, markdown_path=path, project_root=project_root)}</div>"""


def _render_markdown_document(content: str, *, markdown_path: str = "", project_root: str = "") -> str:
    blocks: list[str] = []
    list_stack: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def close_lists() -> None:
        while list_stack:
            blocks.append(f"</{list_stack.pop()}>")

    lines = content.splitlines()
    line_index = 0
    while line_index < len(lines):
        raw_line = lines[line_index]
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append(f'<pre class="markdown-code"><code>{_escape(chr(10).join(code_lines))}</code></pre>')
                code_lines = []
                in_code = False
            else:
                close_lists()
                in_code = True
                code_lines = []
            line_index += 1
            continue
        if in_code:
            code_lines.append(raw_line)
            line_index += 1
            continue
        if _is_markdown_table_start(lines, line_index):
            close_lists()
            table_lines = [stripped, lines[line_index + 1].strip()]
            line_index += 2
            while line_index < len(lines) and lines[line_index].strip() and "|" in lines[line_index]:
                table_lines.append(lines[line_index].strip())
                line_index += 1
            blocks.append(_render_markdown_table(table_lines, markdown_path=markdown_path, project_root=project_root))
            continue
        if not stripped:
            close_lists()
            line_index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_lists()
            level = min(len(heading.group(1)) + 2, 6)
            blocks.append(f"<h{level}>{_markdown_inline(heading.group(2), markdown_path=markdown_path, project_root=project_root)}</h{level}>")
            line_index += 1
            continue
        image = _parse_markdown_image_line(stripped)
        if image:
            close_lists()
            blocks.append(_markdown_figure(image["alt"], image["path"], image["title"], markdown_path=markdown_path, project_root=project_root))
            line_index += 1
            continue
        checklist = re.match(r"^[-*]\s+\[([ xX])\]\s+(.+)$", stripped)
        if checklist:
            if list_stack[-1:] != ["ul"]:
                close_lists()
                blocks.append('<ul class="markdown-task-list">')
                list_stack.append("ul")
            checked = checklist.group(1).lower() == "x"
            checked_attr = " checked" if checked else ""
            blocks.append(
                f'<li class="markdown-task"><input type="checkbox" disabled{checked_attr}>'
                f"<span>{_markdown_inline(checklist.group(2), markdown_path=markdown_path, project_root=project_root)}</span></li>"
            )
            line_index += 1
            continue
        unordered = re.match(r"^[-*]\s+(.+)$", stripped)
        if unordered:
            if list_stack[-1:] != ["ul"]:
                close_lists()
                blocks.append("<ul>")
                list_stack.append("ul")
            blocks.append(f"<li>{_markdown_inline(unordered.group(1), markdown_path=markdown_path, project_root=project_root)}</li>")
            line_index += 1
            continue
        ordered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ordered:
            if list_stack[-1:] != ["ol"]:
                close_lists()
                blocks.append("<ol>")
                list_stack.append("ol")
            blocks.append(f"<li>{_markdown_inline(ordered.group(1), markdown_path=markdown_path, project_root=project_root)}</li>")
            line_index += 1
            continue
        close_lists()
        blocks.append(f"<p>{_markdown_inline(stripped, markdown_path=markdown_path, project_root=project_root)}</p>")
        line_index += 1
    if in_code:
        blocks.append(f'<pre class="markdown-code"><code>{_escape(chr(10).join(code_lines))}</code></pre>')
    close_lists()
    return "\n".join(blocks)


def _is_markdown_table_start(lines: Sequence[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    divider = lines[index + 1].strip()
    return "|" in header and _is_markdown_table_divider(divider)


def _is_markdown_table_divider(line: str) -> bool:
    if not line or "|" not in line:
        return False
    return all(re.match(r"^:?-{3,}:?$", cell.strip()) is not None for cell in _split_markdown_table_row(line))


def _split_markdown_table_row(line: str) -> list[str]:
    trimmed = line.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return [cell.strip() for cell in trimmed.split("|")]


def _render_markdown_table(lines: Sequence[str], *, markdown_path: str = "", project_root: str = "") -> str:
    header = _split_markdown_table_row(lines[0] if lines else "")
    rows = [
        row
        for row in (_split_markdown_table_row(line) for line in list(lines)[2:])
        if any(cell.strip() for cell in row)
    ]
    head = "".join(f"<th>{_markdown_inline(cell, markdown_path=markdown_path, project_root=project_root)}</th>" for cell in header)
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td>{_markdown_inline(row[index] if index < len(row) else '', markdown_path=markdown_path, project_root=project_root)}</td>"
            for index, _cell in enumerate(header)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return f'<div class="markdown-table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'


def _markdown_inline(value: str, *, markdown_path: str = "", project_root: str = "") -> str:
    parts = re.split(r"(`[^`]+`)", str(value or ""))
    rendered: list[str] = []
    for part in parts:
        if re.fullmatch(r"`[^`]+`", part):
            rendered.append(f"<code>{_escape(part[1:-1])}</code>")
        else:
            rendered.append(_markdown_inline_segment(part, markdown_path=markdown_path, project_root=project_root))
    return "".join(rendered)


def _markdown_inline_segment(value: str, *, markdown_path: str = "", project_root: str = "") -> str:
    source = str(value or "")
    rendered: list[str] = []
    cursor = 0
    while True:
        token = _next_markdown_inline_token(source, cursor)
        if token is None:
            break
        kind, start, end, label, path, title = token
        rendered.append(_markdown_plain_text(source[cursor:start], markdown_path=markdown_path))
        if kind == "image":
            rendered.append(_markdown_inline_image(label, path, title, markdown_path=markdown_path, project_root=project_root))
        else:
            rendered.append(
                _markdown_anchor(
                    label,
                    _markdown_link_href(path, markdown_path=markdown_path, project_root=project_root),
                    css_class="markdown-link",
                )
            )
        cursor = end + 1
    rendered.append(_markdown_plain_text(source[cursor:], markdown_path=markdown_path))
    return "".join(rendered)


def _next_markdown_inline_token(source: str, start_index: int) -> tuple[str, int, int, str, str, str] | None:
    image = _next_markdown_image(source, start_index)
    link = _next_markdown_link(source, start_index)
    candidates = [candidate for candidate in (image, link) if candidate is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[1])


def _next_markdown_image(source: str, start_index: int) -> tuple[str, int, int, str, str, str] | None:
    start = source.find("![", start_index)
    while start != -1:
        close_label = source.find("]", start + 2)
        if close_label != -1 and close_label + 1 < len(source) and source[close_label + 1] == "(":
            close_dest = source.find(")", close_label + 2)
            if close_dest != -1:
                path, title = _parse_markdown_destination_and_title(source[close_label + 2 : close_dest])
                if path:
                    return "image", start, close_dest, source[start + 2 : close_label], path, title
        start = source.find("![", start + 2)
    return None


def _next_markdown_link(source: str, start_index: int) -> tuple[str, int, int, str, str, str] | None:
    start = source.find("[", start_index)
    while start != -1:
        if start > 0 and source[start - 1] == "!":
            start = source.find("[", start + 1)
            continue
        close_label = source.find("]", start + 1)
        if close_label != -1 and close_label + 1 < len(source) and source[close_label + 1] == "(":
            close_dest = source.find(")", close_label + 2)
            if close_dest != -1:
                path, _title = _parse_markdown_destination_and_title(source[close_label + 2 : close_dest])
                if path:
                    return "link", start, close_dest, source[start + 1 : close_label], path, ""
        start = source.find("[", start + 1)
    return None


def _markdown_plain_text(value: str, *, markdown_path: str = "") -> str:
    return _markdown_emphasis(str(value or ""))


def _markdown_emphasis(value: str) -> str:
    escaped = _escape(value)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _markdown_anchor(label: str, href: str, *, css_class: str) -> str:
    if not href:
        return _markdown_emphasis(label)
    return (
        f'<a class="{_escape(css_class)}" href="{_escape(href)}" '
        f'target="_blank" rel="noopener noreferrer">{_markdown_emphasis(label)}</a>'
    )


def _markdown_figure(alt: str, raw_path: str, title: str, *, markdown_path: str = "", project_root: str = "") -> str:
    src = _markdown_asset_src(raw_path, markdown_path=markdown_path, project_root=project_root)
    caption = title or alt
    caption_html = (
        f"<figcaption>{_markdown_inline(caption, markdown_path=markdown_path, project_root=project_root)}</figcaption>"
        if caption
        else ""
    )
    return (
        '<figure class="markdown-figure">'
        f'<a class="markdown-figure-link" href="{_escape(src)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{_escape(src)}" alt="{_escape(alt or caption or "figure")}" loading="lazy">'
        f"</a>{caption_html}</figure>"
    )


def _markdown_inline_image(alt: str, raw_path: str, title: str, *, markdown_path: str = "", project_root: str = "") -> str:
    src = _markdown_asset_src(raw_path, markdown_path=markdown_path, project_root=project_root)
    label = title or alt or "figure"
    if not src:
        return _markdown_emphasis(label)
    return (
        '<span class="markdown-inline-figure">'
        f'<a class="markdown-figure-link" href="{_escape(src)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{_escape(src)}" alt="{_escape(label)}" loading="lazy">'
        f"</a></span>"
    )


def _parse_markdown_image_line(line: str) -> dict[str, str] | None:
    image = re.match(r"^!\[([^\]]*)\]\((.*)\)$", line)
    if not image:
        return None
    path, title = _parse_markdown_destination_and_title(image.group(2))
    if not path:
        return None
    return {"alt": image.group(1), "path": path, "title": title}


def _parse_markdown_destination_and_title(raw_value: str) -> tuple[str, str]:
    body = html.unescape(str(raw_value or "").strip())
    title = ""
    title_match = re.match(r'^(.*?)\s+(["\'])(.*?)\2\s*$', body)
    if title_match:
        body = title_match.group(1)
        title = title_match.group(3)
    return body.strip().strip("<>"), title


def _markdown_link_href(raw_path: str, *, markdown_path: str = "", project_root: str = "") -> str:
    path = str(raw_path or "").strip().strip("<>")
    if not path:
        return ""
    if re.match(r"^(?:mailto:|tel:|#)", path, flags=re.I):
        return path
    return _markdown_asset_src(path, markdown_path=markdown_path, project_root=project_root)


def _markdown_asset_src(raw_path: str, *, markdown_path: str = "", project_root: str = "") -> str:
    path = html.unescape(raw_path.strip().strip("<>"))
    project_relative = _markdown_project_relative_from_absolute_path(path, project_root)
    if project_relative:
        return project_relative
    if re.match(r"^(?:https?:|data:|blob:|/)", path, flags=re.I):
        return path
    path = _normalize_markdown_relative_path(path)
    if _looks_project_relative_markdown_path(path) or not markdown_path:
        return path
    base = PurePosixPath(markdown_path).parent
    return _normalize_markdown_relative_path((base / path).as_posix())


def _markdown_project_relative_from_absolute_path(path: str, project_root: str) -> str:
    source = _normalize_markdown_absolute_path(path)
    root = _normalize_markdown_absolute_path(project_root)
    if not source or not root or not source.startswith("/") or not root.startswith("/"):
        return ""
    if source == root or not source.startswith(root + "/"):
        return ""
    return _normalize_markdown_relative_path(source[len(root) + 1 :])


def _normalize_markdown_absolute_path(path: str) -> str:
    clean = str(path or "").strip().replace("\\", "/")
    while "//" in clean:
        clean = clean.replace("//", "/")
    return clean.rstrip("/")


def _looks_project_relative_markdown_path(path: str) -> bool:
    if path.startswith(".loopplane/"):
        return True
    if path.startswith("../") or path.startswith("./"):
        return False
    return "/" in path


def _normalize_markdown_relative_path(path: str) -> str:
    if re.match(r"^(?:https?:|data:|blob:|/)", path, flags=re.I):
        return path
    parts: list[str] = []
    for part in path.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _human_summary_ready(summary: Mapping[str, Any]) -> bool:
    return _text(summary.get("status")) == "ready" and bool(
        _text(summary.get("content") or summary.get("markdown_path") or summary.get("excerpt"))
    )


def _render_human_summary_trigger(label: str, summary: Mapping[str, Any], *, css_class: str = "") -> str:
    display = _text(label)
    if not _human_summary_ready(summary):
        return _escape(display)
    title = _text(summary.get("title") or display or "Human-readable summary")
    path = _text(summary.get("markdown_path") or "")
    content = _text(summary.get("content") or summary.get("excerpt") or "")
    path_attr = f' data-detail-path="{_escape(path)}"' if path else ""
    content_attr = f' data-detail-content="{_escape(content)}"' if content else ""
    classes = "human-summary-trigger"
    if css_class:
        classes += f" {_escape(css_class)}"
    return (
        f'<button type="button" class="{classes}" '
        f'data-detail-title="{_escape(title)}" data-detail-render="markdown"{path_attr}{content_attr} '
        f'aria-label="{_escape("Open summary for " + display)}">{_escape(display)}</button>'
    )


def _render_graph_panel(workflow_graph: Mapping[str, Any], plan_index: Mapping[str, Any]) -> str:
    nodes = _sequence(workflow_graph.get("nodes"))
    edges = _sequence(workflow_graph.get("edges"))
    if not nodes:
        return '<p class="empty-state">No graph nodes are present.</p>'
    phase_groups = _graph_phase_groups(plan_index, nodes)
    lane_html = []
    for group in phase_groups:
        task_cards = _render_graph_task_cards(group)
        group_status = _text(group.get("status") or "runtime")
        group_title = _render_human_summary_trigger(
            _text(group.get("title") or "Workflow Events"),
            _mapping(group.get("human_summary")),
            css_class="graph-summary-link",
        )
        lane_html.append(
            f"""<section class="graph-phase-lane graph-phase-group" data-phase-key="{_escape(_text(group.get("phase_key")))}" {_status_attrs(group_status)}{_expanded_attr(group)}>
  <div class="graph-phase-heading">
    <div>
      <strong>{group_title}</strong>
      {_expansion_note_html(group, entity_label="Phase")}
      <small>{_escape(_text(group.get("subtitle") or ""))}</small>
    </div>
    {_status_pill(group_status)}
  </div>
  <div class="graph-task-rail">
    {task_cards}
  </div>
</section>"""
        )
    edge_rows = []
    for edge in edges[:12]:
        item = _mapping(edge)
        edge_rows.append(
            f"<li>{_escape(_text(item.get('source')))} to {_escape(_text(item.get('target')))} <span>{_escape(_text(item.get('type') or 'edge'))}</span></li>"
        )
    overflow = ""
    if len(edges) > 12:
        overflow = f'<li>{len(edges) - 12} more edges</li>'
    return f"""<div class="graph-mode-toolbar">
  <div>
    <span class="eyebrow">Graph Mode</span>
    <strong>Agent Pipeline</strong>
  </div>
  <div class="graph-mode-actions">
    <small>Agent runs, lifecycle events, and validation checks.</small>
    <button type="button" class="graph-expand-toggle" data-graph-expand-toggle aria-pressed="false">Expand All</button>
  </div>
</div>
{_render_graph_overview(phase_groups, nodes, edges, workflow_graph)}
<div class="graph-pipeline-scroll" tabindex="0" aria-label="Scrollable phase pipeline">
  <div class="graph-pipeline" data-graph-mode="phase_pipeline">
    {''.join(lane_html)}
  </div>
</div>
<details class="graph-edge-summary">
  <summary><strong>Runtime Relations</strong><span>{len(edges)}</span></summary>
  <ul class="edge-list">
    {''.join(edge_rows)}{overflow}
  </ul>
</details>"""


def _render_graph_overview(
    phase_groups: Sequence[Mapping[str, Any]],
    nodes: Sequence[Any],
    edges: Sequence[Any],
    workflow_graph: Mapping[str, Any],
) -> str:
    task_count = sum(len(_sequence(group.get("tasks"))) for group in phase_groups)
    node_records = [_mapping(node) for node in nodes]
    agent_count = sum(1 for node in node_records if _is_graph_agent_node(node))
    event_count = sum(1 for node in node_records if _is_graph_event_node(node))
    event_window = _mapping(workflow_graph.get("event_window"))
    total_events = _coerce_int(event_window.get("total_events")) or _coerce_int(_mapping(workflow_graph.get("source_hashes")).get("events_count"))
    event_label = "recent events" if total_events and total_events > event_count else "events"
    event_suffix = f"<small>of {_escape(total_events)}</small>" if total_events and total_events > event_count else ""
    aggregation = _mapping(workflow_graph.get("self_expansion_aggregation"))
    aggregated_count = _coerce_int(aggregation.get("aggregated_node_count")) or 0
    aggregation_html = (
        f'<span><strong>{aggregated_count}</strong> self-expansion aggregated</span>'
        if aggregated_count
        else ""
    )
    check_count = sum(1 for node in node_records if _is_graph_check_node(node))
    hot_nodes = [
        node
        for node in node_records
        if _status_tier(node.get("status")) in {"danger", "warning", "active"}
    ]
    return f"""<div class="graph-overview" aria-label="Graph summary">
  <span><strong>{len(phase_groups)}</strong> phases</span>
  <span><strong>{task_count}</strong> tasks</span>
  <span><strong>{agent_count}</strong> agents</span>
  <span><strong>{event_count}</strong> {_escape(event_label)}{event_suffix}</span>
  {aggregation_html}
  <span><strong>{check_count}</strong> checks</span>
  <span data-status-tier="{_escape('warning' if hot_nodes else 'muted')}"><strong>{len(hot_nodes)}</strong> attention</span>
</div>"""


def _render_graph_task_cards(group: Mapping[str, Any]) -> str:
    nodes = [_mapping(node) for node in _sequence(group.get("nodes"))]
    tasks = sorted(
        [_mapping(task) for task in _sequence(group.get("tasks"))],
        key=lambda task: _graph_task_priority(task, nodes),
    )
    used_node_ids: set[str] = set()
    cards: list[str] = []
    for task in tasks:
        task_id = _text(task.get("task_id") or "")
        task_nodes = [node for node in nodes if _text(node.get("task_id") or "") == task_id]
        used_node_ids.update(_text(node.get("node_id") or "") for node in task_nodes)
        cards.append(_render_graph_task_card(task, task_nodes))
    unassigned = [node for node in nodes if _text(node.get("node_id") or "") not in used_node_ids]
    if unassigned:
        if tasks:
            cards.append(
                _render_graph_task_card(
                    {
                        "task_id": "workflow_events",
                        "title": "Unassigned Runtime Events",
                        "status": group.get("status") or "events",
                    },
                    unassigned,
                )
            )
        else:
            cards.append(
                _render_graph_task_card(
                    {
                        "task_id": "workflow_events",
                        "title": _text(group.get("title") or "Workflow Events"),
                        "status": group.get("status") or "events",
                    },
                    unassigned,
                )
            )
    if not cards:
        return '<article class="graph-task-card empty"><p class="empty-state">No agent runs for this phase yet.</p></article>'
    return "".join(cards)


def _render_graph_task_card(task: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> str:
    task_id = _text(task.get("task_id") or "task")
    task_status = _text(task.get("status") or "planned")
    timeline = _render_graph_agent_timeline(nodes, task_id=task_id)
    time_label = _graph_task_time_label(nodes)
    task_title = _render_human_summary_trigger(
        _text(task.get("title") or "Untitled task"),
        _mapping(task.get("human_summary")),
        css_class="graph-summary-link",
    )
    return f"""<article class="graph-task-card" {_status_attrs(task_status)} data-task-id="{_escape(task_id)}"{_expanded_attr(task)}>
  <div class="graph-task-heading">
    <div>
      <strong>{_escape(task_id)}</strong>
      {_expansion_note_html(task, entity_label="Task")}
      <small>{task_title}</small>
      {f'<small class="graph-time-label">{_escape(time_label)}</small>' if time_label else ''}
    </div>
    {_status_pill(task_status)}
  </div>
  <div class="graph-node-stack agent-run-stack">{timeline}</div>
</article>"""


def _render_graph_agent_timeline(nodes: Sequence[Mapping[str, Any]], *, task_id: str) -> str:
    node_records = [_mapping(node) for node in nodes]
    agents = sorted([node for node in node_records if _is_graph_agent_node(node)], key=_graph_node_priority)
    secondary = [node for node in node_records if not _is_graph_agent_node(node)]
    used_ids: set[str] = set()
    cards: list[str] = []
    for agent in agents:
        related = [
            node
            for node in secondary
            if _text(node.get("node_id") or "") not in used_ids
            and _graph_node_related_to_agent(node, agent)
        ]
        used_ids.add(_text(agent.get("node_id") or ""))
        used_ids.update(_text(node.get("node_id") or "") for node in related)
        cards.append(_render_graph_agent_card(agent, related, task_id=task_id))
    remaining = [node for node in sorted(node_records, key=_graph_node_priority) if _text(node.get("node_id") or "") not in used_ids]
    if remaining:
        cards.append(_render_graph_record_group(remaining, title="Workflow Records", task_id=task_id))
    if not cards:
        return '<p class="empty-state">No agent runs yet.</p>'
    return "".join(cards)


def _render_graph_agent_card(agent: Mapping[str, Any], related_nodes: Sequence[Mapping[str, Any]], *, task_id: str) -> str:
    checks = sorted([node for node in related_nodes if _is_graph_check_node(node)], key=_graph_node_priority)
    events = sorted([node for node in related_nodes if _is_graph_event_node(node)], key=_graph_node_priority)
    other = sorted(
        [node for node in related_nodes if not _is_graph_event_node(node) and not _is_graph_check_node(node)],
        key=_graph_node_priority,
    )
    status = _text(agent.get("status") or "unknown")
    display_agent = _graph_node_with_related_time(agent, related_nodes)
    event_row = _render_graph_record_row("Lifecycle", events, task_id=task_id)
    check_row = _render_graph_record_row("Checks", checks, task_id=task_id)
    other_row = _render_graph_record_row("Records", other, task_id=task_id)
    return f"""<section class="agent-run-card" {_status_attrs(status)}>
  {_render_graph_node_button(display_agent, task_id=task_id, variant="agent")}
  {event_row}{check_row}{other_row}
</section>"""


def _render_graph_record_group(nodes: Sequence[Mapping[str, Any]], *, title: str, task_id: str) -> str:
    status = _text(_mapping(nodes[0]).get("status") if nodes else "events")
    return f"""<section class="agent-run-card graph-record-group" {_status_attrs(status)}>
  <div class="agent-run-group-heading"><strong>{_escape(title)}</strong><small>{len(nodes)} record{'s' if len(nodes) != 1 else ''}</small></div>
  {_render_graph_record_row("Related", nodes, task_id=task_id, limit=8)}
</section>"""


def _render_graph_record_row(label: str, nodes: Sequence[Mapping[str, Any]], *, task_id: str, limit: int = 6) -> str:
    if not nodes:
        return ""
    ordered = sorted(nodes, key=_graph_node_priority)
    visible = ordered[:limit]
    hidden = ordered[limit:]
    chips = [_render_graph_node_button(node, task_id=task_id, variant=_graph_node_variant(node)) for node in visible]
    if hidden:
        label_text = f"Show {len(hidden)} more related record{'s' if len(hidden) != 1 else ''}"
        less_label = f"Hide {len(hidden)} related record{'s' if len(hidden) != 1 else ''}"
        chips.append(
            f'<button class="graph-node-more" type="button" data-graph-more="collapsed" aria-expanded="false" '
            f'data-more-label="{_escape(label_text)}" data-less-label="{_escape(less_label)}">{_escape(label_text)}</button>'
        )
        for node in hidden:
            chips.append(_render_graph_node_button(node, task_id=task_id, variant=_graph_node_variant(node), hidden=True))
    return f"""<div class="agent-flow-row" data-flow-kind="{_escape(label.lower())}">
  <span class="agent-flow-label">{_escape(label)}</span>
  <div class="agent-flow-items">{''.join(chips)}</div>
</div>"""


def _render_graph_node_button(
    node: Mapping[str, Any],
    *,
    task_id: str,
    variant: str = "record",
    hidden: bool = False,
) -> str:
    status = _text(node.get("status") or "unknown")
    hidden_attr = ' data-overflow-node="true" hidden' if hidden else ""
    type_label = _graph_node_type_label(node)
    title = _graph_node_title_label(node, status=status, variant=variant)
    class_name = "graph-node"
    if variant == "agent":
        class_name += " agent-run-node"
    elif variant == "event":
        class_name += " graph-event-chip"
    elif variant == "check":
        class_name += " graph-check-chip"
    else:
        class_name += " graph-record-chip"
    small_label = _graph_node_time_label(node)
    strong_label = f"{type_label} · {_human_status_label(status)}" if variant == "agent" else title
    return f"""<button class="{class_name}" type="button" data-node-id="{_escape(_text(node.get("node_id")))}" {_status_attrs(status)} data-task-id="{_escape(_text(node.get("task_id") or ""))}"{hidden_attr}>
  <span>{_escape(type_label)}</span>
  <strong>{_escape(strong_label)}</strong>
  <small>{_escape(small_label)}</small>
</button>"""


def _graph_node_type_label(node: Mapping[str, Any]) -> str:
    node_type = _status_value(node.get("type") or "node")
    if node_type == "validation":
        return "Validation"
    if node_type == "event":
        return _truncate_text(_text(node.get("context_label") or node.get("actor_label") or "Event"), 42)
    if node_type in {"worker", "recovery_worker", "planner", "auditor", "inspector", "final_verifier"}:
        return node_type.replace("_", " ").title()
    return node_type.replace("_", " ").title() or "Node"


def _graph_node_title_label(node: Mapping[str, Any], *, status: str, variant: str) -> str:
    if variant == "event":
        title = _text(node.get("title") or "")
        if title:
            return _truncate_text(title, 72)
        sequence = node.get("event_sequence")
        event_label = _humanize_identifier(_text(node.get("event_type") or status or "event"))
        return _truncate_text(f"Event {sequence}: {event_label}" if sequence not in (None, "") else event_label, 72)
    if variant == "check":
        return f"Validation · {_human_status_label(status)}"
    title = _text(node.get("title") or node.get("task_title") or node.get("task_id") or status or "Node")
    if variant == "agent":
        return title
    return _truncate_text(_humanize_identifier(title), 64)


def _humanize_identifier(value: str) -> str:
    cleaned = value.strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() if part.isupper() or part.islower() else part for part in cleaned.split())


def _human_status_label(value: str) -> str:
    labels = {
        "needs_verification": "Verify",
        "needs-expansion": "Expand",
        "needs_expansion": "Expand",
        "objective_unresolved": "Unresolved",
        "pass_with_warnings": "Warnings",
    }
    normalized = _status_value(value)
    if normalized in labels:
        return labels[normalized]
    return _humanize_identifier(value or "unknown")


def _graph_node_time_label(node: Mapping[str, Any]) -> str:
    started = _text(node.get("started_at") or "")
    ended = _text(node.get("ended_at") or "")
    heartbeat = _text(node.get("heartbeat_at") or "")
    active = node.get("active") is True or _status_tier(node.get("status")) == "active"
    if active and started:
        return f"{_compact_dashboard_time(started)} -> Present"
    if started and ended and started != ended:
        return f"{_compact_dashboard_time(started)} -> {_compact_dashboard_time(ended)}"
    if started:
        return f"Started {_compact_dashboard_time(started)}"
    if ended:
        return f"Ended {_compact_dashboard_time(ended)}"
    if heartbeat:
        return f"Heartbeat {_compact_dashboard_time(heartbeat)}"
    return "Time pending"


def _graph_node_with_related_time(agent: Mapping[str, Any], related_nodes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if agent.get("started_at") or agent.get("ended_at"):
        return agent
    timestamps: list[datetime] = []
    for node in (agent, *related_nodes):
        for key in ("started_at", "ended_at", "heartbeat_at", "ts", "timestamp", "created_at"):
            parsed = _parse_dashboard_timestamp(_mapping(node).get(key))
            if parsed is not None:
                timestamps.append(parsed)
    if not timestamps:
        return agent
    display = dict(agent)
    if not display.get("started_at"):
        display["started_at"] = min(timestamps).isoformat().replace("+00:00", "Z")
    active = display.get("active") is True or _status_tier(display.get("status")) == "active"
    if not display.get("ended_at") and not active:
        display["ended_at"] = max(timestamps).isoformat().replace("+00:00", "Z")
    return display


def _graph_task_time_label(nodes: Sequence[Mapping[str, Any]]) -> str:
    parsed: list[datetime] = []
    active = False
    for node in nodes:
        active = active or _mapping(node).get("active") is True or _status_tier(_mapping(node).get("status")) == "active"
        for key in ("started_at", "ended_at", "heartbeat_at"):
            value = _parse_dashboard_timestamp(node.get(key))
            if value is not None:
                parsed.append(value)
    if not parsed:
        return ""
    started = min(parsed)
    ended = max(parsed)
    if active:
        return f"{_compact_dashboard_time(started)} -> Present"
    if started == ended:
        return f"At {_compact_dashboard_time(started)}"
    return f"{_compact_dashboard_time(started)} -> {_compact_dashboard_time(ended)}"


def _graph_node_variant(node: Mapping[str, Any]) -> str:
    if _is_graph_event_node(node):
        return "event"
    if _is_graph_check_node(node):
        return "check"
    return "record"


def _is_graph_event_node(node: Mapping[str, Any]) -> bool:
    return _status_value(node.get("type")) == "event"


def _is_graph_check_node(node: Mapping[str, Any]) -> bool:
    return _status_value(node.get("type")) == "validation"


def _is_graph_agent_node(node: Mapping[str, Any]) -> bool:
    node_type = _status_value(node.get("type"))
    return bool(node.get("run_id")) and node_type not in {"event", "validation"}


def _graph_node_related_to_agent(node: Mapping[str, Any], agent: Mapping[str, Any]) -> bool:
    node_run_id = _text(node.get("run_id") or "")
    agent_run_id = _text(agent.get("run_id") or "")
    if agent_run_id:
        return bool(node_run_id and node_run_id == agent_run_id)
    node_task_id = _text(node.get("task_id") or "")
    agent_task_id = _text(agent.get("task_id") or "")
    return bool(node_task_id and agent_task_id and node_task_id == agent_task_id)


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _graph_node_priority(node: Mapping[str, Any]) -> tuple[int, str, int, str]:
    tier_rank = {
        "active": 0,
        "danger": 1,
        "warning": 2,
        "info": 3,
        "success": 4,
        "muted": 5,
    }
    timestamp = _latest_graph_node_timestamp(node)
    return (
        0 if timestamp else 1,
        _reverse_sort_text(timestamp),
        tier_rank.get(_status_tier(node.get("status")), 6),
        _text(node.get("node_id") or ""),
    )


def _graph_task_priority(task: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> tuple[int, str, int, str]:
    task_id = _text(task.get("task_id") or "")
    timestamps = [
        _latest_graph_node_timestamp(node)
        for node in nodes
        if _text(_mapping(node).get("task_id") or "") == task_id
    ]
    own_timestamp = _latest_graph_mapping_timestamp(task)
    if own_timestamp:
        timestamps.append(own_timestamp)
    latest = max((timestamp for timestamp in timestamps if timestamp), default="")
    order_index = task.get("order_index")
    try:
        order = int(order_index)
    except (TypeError, ValueError):
        order = 0
    return (0 if latest else 1, _reverse_sort_text(latest), order, task_id)


def _graph_group_priority(group: Mapping[str, Any]) -> tuple[int, str, int, str]:
    timestamps = [_latest_graph_node_timestamp(_mapping(node)) for node in _sequence(group.get("nodes"))]
    timestamps.extend(_latest_graph_mapping_timestamp(_mapping(task)) for task in _sequence(group.get("tasks")))
    latest = max((timestamp for timestamp in timestamps if timestamp), default="")
    try:
        order = int(group.get("order_index"))
    except (TypeError, ValueError):
        order = 0
    return (0 if latest else 1, _reverse_sort_text(latest), order, _text(group.get("phase_key") or ""))


def _latest_graph_node_timestamp(node: Mapping[str, Any]) -> str:
    return _latest_graph_mapping_timestamp(node)


def _latest_graph_mapping_timestamp(value: Mapping[str, Any]) -> str:
    timestamps: list[str] = []
    for key in (
        "ended_at",
        "completed_at",
        "finished_at",
        "validated_at",
        "updated_at",
        "heartbeat_at",
        "started_at",
        "ts",
        "timestamp",
        "created_at",
    ):
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            timestamps.append(raw)
    return max(timestamps) if timestamps else ""


def _reverse_sort_text(value: str) -> str:
    return "".join(chr(0x10FFFF - ord(char)) for char in value)


def _graph_phase_groups(plan_index: Mapping[str, Any], nodes: Sequence[Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    task_to_group: dict[str, dict[str, Any]] = {}
    phase_to_group: dict[str, dict[str, Any]] = {}
    for index, raw_phase in enumerate(_sequence(plan_index.get("phases"))):
        phase = _mapping(raw_phase)
        phase_key = _text(phase.get("phase_id") or phase.get("id") or phase.get("title") or f"phase_{index + 1}")
        task_ids: list[str] = []
        for raw_task in _sequence(phase.get("tasks")):
            task = _mapping(raw_task)
            task_id = _text(task.get("task_id") or "")
            if not task_id:
                continue
            task_ids.append(task_id)
        group = {
            "phase_key": phase_key,
            "title": _text(phase.get("title") or f"Phase {index + 1}"),
            "subtitle": f"{len(task_ids)} task{'s' if len(task_ids) != 1 else ''}",
            "status": _text(phase.get("status") or "planned"),
            "human_summary": dict(_mapping(phase.get("human_summary"))),
            "expanded": phase.get("expanded") is True,
            "expansion_marker": phase.get("expansion_marker"),
            "expansion": dict(_mapping(phase.get("expansion"))),
            "order_index": index,
            "task_ids": task_ids,
            "tasks": [
                {
                    "task_id": _text(_mapping(task).get("task_id") or ""),
                    "title": _text(_mapping(task).get("title") or "Untitled task"),
                    "status": _text(_mapping(task).get("status") or "planned"),
                    "deliverables": _text(_mapping(task).get("deliverables") or ""),
                    "human_summary": dict(_mapping(_mapping(task).get("human_summary"))),
                    "expanded": _mapping(task).get("expanded") is True,
                    "expansion_marker": _mapping(task).get("expansion_marker"),
                    "expansion": dict(_mapping(_mapping(task).get("expansion"))),
                    "order_index": task_index,
                }
                for task_index, task in enumerate(_sequence(phase.get("tasks")))
                if _text(_mapping(task).get("task_id") or "")
            ],
            "nodes": [],
        }
        groups.append(group)
        for key in {phase_key, _text(phase.get("id") or ""), _text(phase.get("title") or "")}:
            if key:
                phase_to_group[key] = group
        for task_id in task_ids:
            task_to_group[task_id] = group
    event_group = {
        "phase_key": "workflow_events",
        "title": "Workflow Events",
        "subtitle": "Unassigned runtime and lifecycle nodes",
        "status": "events",
        "order_index": len(groups),
        "task_ids": [],
        "nodes": [],
    }
    for raw_node in nodes:
        node = dict(_mapping(raw_node))
        task_id = _text(node.get("task_id") or "")
        group = task_to_group.get(task_id) if task_id else None
        if group is None:
            for key in (
                _text(node.get("phase_id") or ""),
                _text(node.get("objective_phase_id") or ""),
                _text(node.get("phase") or ""),
            ):
                if key and key in phase_to_group:
                    group = phase_to_group[key]
                    break
        if group is None:
            group = event_group
        group["nodes"].append(node)
    visible_groups = [group for group in groups if group.get("nodes") or group.get("task_ids")]
    if event_group["nodes"]:
        visible_groups.append(event_group)
    visible_groups = sorted(visible_groups, key=_graph_group_priority)
    return visible_groups or [event_group]


def _render_node_detail(node: Mapping[str, Any], detail: Mapping[str, Any] | None = None) -> str:
    if not node:
        return '<div id="node-detail-body"><p class="empty-state">No node selected.</p></div>'
    return f'<div id="node-detail-body">{_node_detail_html(node, detail)}</div>'


def _node_detail_html(node: Mapping[str, Any], detail: Mapping[str, Any] | None = None) -> str:
    summary = _mapping(node.get("summary"))
    refs = _sequence(node.get("output_refs"))
    risks = _sequence(summary.get("risks"))
    highlights = _sequence(summary.get("highlights"))
    detail_payload = _mapping(detail)
    deliverables = _text(node.get("deliverables") or "")
    one_line = _text(summary.get("one_line") or "No summary available.")
    primary_html = f"""<div class="node-detail-primary">
  <p>{_escape(one_line)}</p>
  {f'<dl class="detail-list compact-detail-list">{_detail_row("Deliverables", deliverables)}</dl>' if deliverables else ''}
</div>"""
    sections = _sequence(detail_payload.get("sections"))
    if not sections:
        sections = [
            {
                "key": "summary",
                "title": "Summary",
                "available": True,
                "content": _text(summary.get("one_line") or "No summary available."),
            }
        ]
    section_html = "".join(_render_node_detail_section(_mapping(section)) for section in sections)
    timing = _node_timing(node)
    return f"""<article class="node-detail">
  <h3>{_escape(_text(node.get("title") or node.get("node_id") or "Node"))}</h3>
  {primary_html}
  <dl class="detail-list">
    {_detail_row("Node", node.get("node_id"))}
    {_detail_row("Type", node.get("type"))}
    {_detail_row("Status", node.get("status"))}
    {_detail_row("Task", node.get("task_id"))}
    {_detail_row("Run", node.get("run_id"))}
    {_detail_row("Started", timing["started"])}
    {_detail_row("Ended", timing["ended"])}
    {_detail_row("Elapsed", timing["elapsed"])}
  </dl>
  {_tag_list("Highlights", highlights)}
  {_tag_list("Risks", risks)}
  {_tag_list("Output Refs", refs)}
  <div class="node-detail-sections">{section_html}</div>
</article>"""


def _detail_for_node(node: Mapping[str, Any], node_details: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = _mapping(node_details.get("nodes"))
    node_id = _text(node.get("node_id"))
    detail = _mapping(nodes.get(node_id))
    if detail:
        return detail
    run_id = _text(node.get("run_id"))
    if run_id:
        return _mapping(_mapping(node_details.get("runs")).get(run_id))
    return {}


def _render_node_detail_section(section: Mapping[str, Any]) -> str:
    key = _text(section.get("key") or "section")
    title = _node_detail_section_title(key, section.get("title") or key.replace("_", " ").title())
    available = section.get("available") is True
    if not available:
        return f"""<section class="node-detail-section" data-section="{_escape(key)}" data-available="false">
  <h4>{_escape(title)}</h4>
  <p class="empty-state">{_escape(_node_detail_section_empty_message(key, section.get("empty_message") or "No evidence recorded."))}</p>
</section>"""
    body_parts: list[str] = []
    content_value = str(section.get("content") or "")
    if section.get("path"):
        body_parts.append(
            _render_detail_file_action(
                section.get("path"),
                title,
                content=content_value,
                truncated=section.get("truncated") is True,
                size_bytes=section.get("size_bytes"),
                sha256=section.get("sha256"),
                render_mode=section.get("render_mode"),
            )
        )
    section_summary_value = section.get("summary")
    if section.get("status") or (section_summary_value and not isinstance(section_summary_value, Mapping)):
        body_parts.append(
            f"""<dl class="detail-list compact-detail-list">
  {_detail_row("Status", section.get("status"))}
  {_detail_row("Summary", section_summary_value)}
</dl>"""
        )
    if content_value and not section.get("path"):
        body_parts.append(
            _render_detail_file_action(
                None,
                title,
                content=content_value,
                truncated=section.get("truncated") is True,
                render_mode=section.get("render_mode"),
            )
        )
    items = _sequence(section.get("items"))
    if items:
        body_parts.append(_render_detail_items(items))
    changed_files = _sequence(section.get("changed_files"))
    if changed_files:
        body_parts.append(_render_changed_file_items(changed_files))
    summary = _mapping(section.get("summary"))
    if summary and not isinstance(section.get("summary"), str):
        body_parts.append(_render_detail_mapping("Summary", summary))
    patch = _mapping(section.get("patch"))
    if patch:
        body_parts.append(_render_artifact_detail("Patch Artifact", patch))
    checkpoint_before = _mapping(section.get("before"))
    checkpoint_after = _mapping(section.get("after"))
    if checkpoint_before or checkpoint_after:
        body_parts.append(
            f"""<div class="node-checkpoint-grid">
  {_render_detail_mapping("Before", checkpoint_before) if checkpoint_before else '<p class="empty-state">No before checkpoint.</p>'}
  {_render_detail_mapping("After", checkpoint_after) if checkpoint_after else '<p class="empty-state">No after checkpoint.</p>'}
</div>"""
        )
    if section.get("truncated") is True:
        body_parts.append('<p class="selector-status">Additional records exist; this view is capped for safety.</p>')
    body = "".join(body_parts) if body_parts else '<p class="empty-state">Evidence metadata is available but has no displayable details.</p>'
    return f"""<section class="node-detail-section" data-section="{_escape(key)}" data-available="true">
  <h4>{_escape(title)}</h4>
  {body}
</section>"""


def _node_detail_section_title(key: str, title: Any) -> str:
    value = _text(title)
    if key == "final_output" and value == "Final Output":
        return "Final Response"
    return value


def _node_detail_section_empty_message(key: str, message: Any) -> str:
    value = _text(message)
    if key == "final_output" and value == "No final output file was recorded for this run.":
        return "No final response file was recorded for this run."
    return value


def _render_detail_file_action(
    path: Any,
    title: str,
    *,
    content: str = "",
    truncated: bool = False,
    size_bytes: Any = None,
    sha256: Any = None,
    render_mode: Any = None,
) -> str:
    path_value = str(path or "").strip()
    if not path_value and not content:
        return ""
    content_attr = f' data-detail-content="{_escape(content)}"' if content else ""
    path_attr = f' data-detail-path="{_escape(path_value)}"' if path_value else ""
    mode = _detail_render_mode(path_value, render_mode)
    render_attr = f' data-detail-render="{_escape(mode)}"' if mode else ""
    truncated_attr = ' data-detail-truncated="true"' if truncated else ""
    meta_rows = [
        _detail_row("Path", path_value) if path_value else "",
        _detail_row("Size", f"{size_bytes} bytes") if size_bytes not in (None, "") else "",
    ]
    link_html = (
        f'<a class="detail-file-link" href="#" data-detail-file-link{path_attr}>Open file</a>'
        if path_value
        else ""
    )
    stream_html = (
        f'<button type="button" class="detail-file-button" data-log-stream-title="{_escape(title)}"{path_attr}>Follow log tail</button>'
        if _is_dashboard_log_path(path_value)
        else ""
    )
    return f"""<div class="detail-file-card"{truncated_attr}>
  <dl class="detail-list compact-detail-list">{''.join(meta_rows)}</dl>
  <div class="detail-file-actions">
    {link_html}
    <button type="button" class="detail-file-button" data-detail-title="{_escape(title)}"{path_attr}{content_attr}{render_attr}{truncated_attr}>Preview</button>
    {stream_html}
  </div>
</div>"""


def _detail_render_mode(path: str, render_mode: Any = None) -> str:
    explicit = str(render_mode or "").strip().lower()
    if explicit in {"markdown", "text"}:
        return explicit
    lowered = path.strip().lower()
    return "markdown" if lowered.endswith((".md", ".markdown", ".mdown", ".mkd")) else "text"


def _is_dashboard_log_path(path: str) -> bool:
    lowered = path.strip().lower()
    return bool(lowered) and (
        lowered.endswith(".log")
        or lowered.endswith(".out")
        or lowered.endswith(".err")
        or "/logs/" in lowered
        or lowered.endswith("_stdout")
        or lowered.endswith("_stderr")
    )


def _render_artifact_detail(title: str, item: Mapping[str, Any]) -> str:
    metadata = _human_detail_metadata(item, exclude={"content", "path"})
    action = _render_detail_file_action(
        item.get("path"),
        title,
        content=str(item.get("content") or ""),
        truncated=item.get("truncated") is True,
        size_bytes=item.get("size_bytes"),
        sha256=item.get("sha256"),
    )
    return f'<div class="detail-mapping"><h5>{_escape(title)}</h5>{action}{_render_detail_mapping("", metadata)}</div>'


def _render_detail_items(items: Sequence[Any]) -> str:
    rows = []
    for item in items:
        if isinstance(item, Mapping):
            path = _detail_item_label(item)
            metadata = _human_detail_metadata(item, exclude={"content", "path"})
            content = str(item.get("content") or "")
            content_html = (
                _render_detail_file_action(
                    item.get("path"),
                    _text(path or "record"),
                    content=content,
                    truncated=item.get("truncated") is True,
                    size_bytes=item.get("size_bytes"),
                    sha256=item.get("sha256"),
                )
                if content or item.get("path")
                else ""
            )
            rows.append(
                f"""<li>
  <strong>{_escape(path or "record")}</strong>
  {_render_detail_mapping("", metadata) if metadata else ""}
  {content_html}
</li>"""
            )
        else:
            rows.append(f"<li><span>{_escape(item)}</span></li>")
    return f'<ul class="node-detail-item-list">{"".join(rows)}</ul>'


def _detail_item_label(item: Mapping[str, Any]) -> str:
    for key in ("path", "title", "name", "status", "type", "event_type", "reason"):
        value = _text(item.get(key) or "")
        if value:
            return value
    for key in ("change_request_id", "request_id", "run_id", "task_id"):
        value = _text(item.get(key) or "")
        if value:
            return value
    return "record"


def _human_detail_metadata(item: Mapping[str, Any], *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = set(exclude or set())
    hidden_suffixes = ("_sha", "_sha256", "_hash", "_token")
    hidden_keys = {
        "sha",
        "sha256",
        "event_hash",
        "events_sha256",
        "events_segment_manifest",
        "content_sha256",
        "source_hashes",
        "token",
        "access_token",
        "api_key",
        "secret",
    }
    metadata: dict[str, Any] = {}
    for key, value in item.items():
        text_key = str(key)
        if text_key in excluded or text_key in hidden_keys or text_key.endswith(hidden_suffixes):
            continue
        if value in (None, "", [], {}):
            continue
        metadata[text_key] = value
    return metadata


def _render_changed_file_items(items: Sequence[Any]) -> str:
    rows = []
    for raw_item in items:
        item = _mapping(raw_item)
        stats = []
        if item.get("lines_added") is not None:
            stats.append(f"+{item.get('lines_added')}")
        if item.get("lines_deleted") is not None:
            stats.append(f"-{item.get('lines_deleted')}")
        stat_label = f" ({', '.join(stats)})" if stats else ""
        rows.append(
            f"""<li>
  <strong>{_escape(item.get("path") or "changed file")}</strong>
  <span>{_escape(_text(item.get("change_type") or "changed") + stat_label)}</span>
</li>"""
        )
    return f'<ul class="node-detail-item-list changed-file-list">{"".join(rows)}</ul>'


def _render_detail_mapping(title: str, values: Mapping[str, Any]) -> str:
    if not values:
        return ""
    rows = []
    for key, value in _human_detail_metadata(values).items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, Mapping):
            display = json.dumps(value, sort_keys=True)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            display = ", ".join(_text(item) for item in value)
        else:
            display = _text(value)
        rows.append(_detail_row(str(key).replace("_", " ").title(), display))
    if not rows:
        return ""
    heading = f"<h5>{_escape(title)}</h5>" if title else ""
    return f'<div class="detail-mapping">{heading}<dl class="detail-list compact-detail-list">{"".join(rows)}</dl></div>'


def _render_activity_feed(feed: Sequence[Any], payload: Mapping[str, Any]) -> str:
    scope_note = _activity_feed_scope_note(feed, payload)
    if not feed:
        return f'{scope_note}<p class="empty-state">No activity records are present.</p>'
    rows = []
    for raw in list(feed)[-25:]:
        item = _mapping(raw)
        severity = _text(item.get("severity") or "info")
        rows.append(
            f"""<li class="feed-row" data-severity="{_escape(severity)}">
  <time>{_escape(_text(item.get("ts") or item.get("generated_at") or ""))}</time>
  <strong>{_escape(_text(item.get("event") or "event"))}</strong>
  <span>{_escape(_text(item.get("message") or ""))}</span>
</li>"""
        )
    return f'{scope_note}<ol class="feed-list">{"".join(rows)}</ol>'


def _activity_feed_scope_note(feed: Sequence[Any], payload: Mapping[str, Any]) -> str:
    max_events = _optional_positive_int(payload.get("max_dashboard_events")) or len(feed)
    freshness = _mapping(payload.get("read_model_freshness"))
    event_log = _mapping(freshness.get("event_log"))
    total_events = _coerce_int(event_log.get("events_count"))
    visible_rows = min(25, len(feed))
    if total_events and total_events > max_events:
        message = (
            f"Read model contains the most recent {len(feed)} of {total_events} events "
            f"(configured max {max_events}); this panel shows the newest {visible_rows} rows."
        )
    else:
        message = f"Read model contains {len(feed)} event records; this panel shows the newest {visible_rows} rows."
    return f'<p class="selector-status">{_escape(message)}</p>'


def _render_git_status(version_control: Mapping[str, Any]) -> str:
    repository = _mapping(version_control.get("repository"))
    latest_checkpoint = _mapping(version_control.get("latest_checkpoint"))
    checkpoint_html = (
        f"""<dl class="detail-list">
  {_detail_row("Checkpoint", latest_checkpoint.get("checkpoint_id") or latest_checkpoint.get("id"))}
  {_detail_row("Reason", latest_checkpoint.get("reason"))}
  {_detail_row("Created", latest_checkpoint.get("created_at"))}
</dl>"""
        if latest_checkpoint
        else '<p class="empty-state">No checkpoint has been recorded in the read model.</p>'
    )
    return f"""<div class="git-grid">
  <dl class="detail-list">
    {_detail_row("Status", version_control.get("status"))}
    {_detail_row("Provider", version_control.get("provider"))}
    {_detail_row("Git Available", version_control.get("git_available"))}
    {_detail_row("Repo Dirty", repository.get("dirty"))}
    {_detail_row("Dirty Files", repository.get("dirty_files_count"))}
    {_detail_row("Head Commit", repository.get("head_commit"))}
    {_detail_row("Problem", version_control.get("problem"))}
  </dl>
  {checkpoint_html}
</div>"""


def _render_read_model_snapshot(payload: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    counts = _mapping(metrics.get("counts"))
    freshness = _mapping(payload.get("read_model_freshness"))
    files = _sequence(payload.get("read_model_files"))
    file_links = "".join(
        f'<li><a href="read_models/{quote(_text(filename))}" data-read-model-file="{_escape(_text(filename))}">{_escape(_text(filename))}</a></li>' for filename in files
    )
    return f"""<dl class="detail-list">
  {_detail_row("Rendered", payload.get("rendered_at"))}
  {_detail_row("Read Models", payload.get("read_models_dir"))}
  {_detail_row("Freshness", freshness.get("status"))}
  {_detail_row("Read Model Event", _event_ref_label(_mapping(freshness.get("read_model"))))}
  {_detail_row("Event Log Head", _event_ref_label(_mapping(freshness.get("event_log"))))}
  {_detail_row("Tasks", counts.get("tasks_total"))}
  {_detail_row("Runs", counts.get("runs_total"))}
  {_detail_row("Failed Validations", counts.get("validations_failed"))}
</dl>
<ul class="model-link-list">{file_links}</ul>"""


def _status_value(value: Any) -> str:
    return _text(value).strip().lower().replace(" ", "_").replace("-", "_")


def _status_tier(value: Any) -> str:
    status = _status_value(value)
    if not status or status == "none":
        return "muted"
    if status in {"fail", "failed", "failure", "error", "blocked", "rejected", "invalid", "conflict", "unsafe", "objective_unresolved"}:
        return "danger"
    if status in {
        "stale",
        "pending",
        "pending_review",
        "requested",
        "queued",
        "waiting",
        "wait",
        "warning",
        "needs_attention",
        "requires_attention",
        "needs_user_approval",
        "needs_expansion",
        "needs_verification",
        "partial",
        "review",
        "expired",
    }:
        return "warning"
    if status in {"starting", "running", "active", "serving", "started", "resumed", "in_progress", "processing"}:
        return "active"
    if status in {
        "pass",
        "ok",
        "current",
        "completed",
        "complete",
        "done",
        "available",
        "approved",
        "applied",
        "created",
        "recorded",
        "submitted",
        "answered",
        "change_request_created",
        "enabled",
        "ready",
        "rendered",
        "closed",
    }:
        return "success"
    if status in {"disabled", "read_only", "read_only_imported", "archived", "archived_view", "superseded", "skipped", "static", "events"}:
        return "muted"
    if "fail" in status or "error" in status or "blocked" in status:
        return "danger"
    if "stale" in status or "pending" in status or "wait" in status or "attention" in status:
        return "warning"
    if "running" in status or "active" in status:
        return "active"
    if "pass" in status or "complete" in status or status.startswith("ok"):
        return "success"
    return "info"


def _status_attrs(value: Any) -> str:
    return f'data-status="{_escape(_status_value(value))}" data-status-tier="{_escape(_status_tier(value))}"'


def _status_pill(value: Any) -> str:
    return f'<span class="status-pill" {_status_attrs(value)}>{_escape(_human_status_label(_text(value)))}</span>'


def _status_feed_row(title: Any, status: Any, detail: Any = "") -> str:
    row_status = _text(status or "unknown")
    return f"""<li {_status_attrs(row_status)}>
  <strong>{_escape(_text(title))}</strong>
  {_status_pill(row_status)}
  <small>{_escape(_text(detail))}</small>
</li>"""


def _metric(label: str, value: Any, key: str) -> str:
    return f"""<div class="metric" data-metric="{_escape(key)}" {_status_attrs(value)}>
  <span>{_escape(label)}</span>
  <strong>{_escape(_text(value if value not in (None, "") else "unknown"))}</strong>
</div>"""


def _detail_row(label: str, value: Any) -> str:
    display = _text(value if value not in (None, "") else "none")
    return f"<dt>{_escape(label)}</dt><dd>{_escape(display)}</dd>"


def _tag_list(label: str, values: Sequence[Any]) -> str:
    if not values:
        return ""
    items = "".join(f"<li>{_escape(_text(value))}</li>" for value in values)
    return f'<div class="tag-section"><h4>{_escape(label)}</h4><ul>{items}</ul></div>'


def _latest_control_label(control: Mapping[str, Any]) -> str:
    request_id = control.get("latest_request_id")
    control_type = control.get("latest_type")
    status = control.get("latest_status")
    if not request_id:
        return "none"
    return " ".join(_text(part) for part in (control_type, status, request_id) if part)


def _progress_label(workflow_status: Mapping[str, Any]) -> str:
    progress = _mapping(workflow_status.get("progress"))
    percent = progress.get("progress_percent")
    completed = progress.get("completed_tasks")
    total = progress.get("total_tasks")
    if percent is None:
        return f"{completed or 0}/{total or 0}"
    return f"{percent}% ({completed or 0}/{total or 0})"


def _checkpoint_label(version_control: Mapping[str, Any]) -> str:
    checkpoint = _mapping(version_control.get("latest_checkpoint"))
    if checkpoint:
        return checkpoint.get("checkpoint_id") or checkpoint.get("id") or "recorded"
    return "none"


def _event_ref_label(value: Mapping[str, Any]) -> str:
    event_id = value.get("source_event_id") or value.get("events_head") or value.get("event_id")
    sequence = value.get("last_event_seq") if value.get("last_event_seq") is not None else value.get("seq")
    if event_id and sequence is not None:
        return f"{event_id} (seq {sequence})"
    if event_id:
        return _text(event_id)
    if sequence is not None:
        return f"seq {sequence}"
    return "none"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _event_sequence(event: Mapping[str, Any]) -> int | None:
    return _coerce_int(event.get("seq", event.get("sequence")))


def _event_id(event: Mapping[str, Any]) -> str | None:
    return _optional_string(event.get("event_id") or event.get("id"))


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _json_script(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    if len(compressed) + 256 < len(encoded):
        payload_for_script: Mapping[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": payload.get("workflow_id"),
            "status": payload.get("status"),
            "workspace": payload.get("workspace"),
            "payload_encoding": "gzip+base64",
            "payload_compressed": base64.b64encode(compressed).decode("ascii"),
            "payload_compressed_bytes": len(compressed),
            "payload_uncompressed_bytes": len(encoded),
            "payload_schema_version": payload.get("schema_version"),
        }
        script = json.dumps(payload_for_script, sort_keys=True, separators=(",", ":"))
    else:
        script = encoded.decode("utf-8")
    return script.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _project_relative(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _failure(*, project: Path, started_at: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "failed",
        "project_root": project.as_posix(),
        "workflow_id": None,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "dashboard_dir": None,
        "index_file": None,
        "read_models_dir": None,
        "errors": [message],
        "warnings": [],
    }
