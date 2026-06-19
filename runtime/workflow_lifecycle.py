from __future__ import annotations

import json
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import (
    DEFAULT_WORKFLOW_PATHS,
    WorkflowPathError,
    WorkflowPaths,
    default_workflow_path_values,
    load_workflow_config,
)
from runtime.workspace_identity import repository_root_value


WORKSPACE_SCHEMA_VERSION = "1.6"
CREATED_WITH = "loopplane 1.5.0"
WORKFLOW_ID_RE = re.compile(r"^wf_\d{8}_[0-9a-f]{8}$")
WORKSPACE_ID_RE = re.compile(r"^ws_[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
WORKFLOW_HISTORY_STATUSES = frozenset(
    {
        "draft",
        "ready",
        "active",
        "running",
        "paused",
        "stopped",
        "objective_unresolved",
        "completed",
        "failed",
        "archived",
        "read_only_imported",
        "forked",
        "superseded",
    }
)
ACTIVE_RUNNING_WORKFLOW_STATUSES = frozenset({"active", "running"})


class WorkflowLifecycleError(RuntimeError):
    pass


def create_workflow_record(
    project_root: Path | str,
    *,
    workflow_id: str,
    name: str,
    workflow_root: str,
    status: str = "draft",
    make_current: bool = False,
    selection_reason: str = "workflow_created",
    updated_by: str = CREATED_WITH,
    created_at: str | None = None,
    summary: Mapping[str, Any] | None = None,
    path_values: Mapping[str, Any] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = _project(project_root)
    _ensure_workflow_id(workflow_id)
    _ensure_status(status)
    now = utc_timestamp()
    registry = _load_or_create_registry(project, updated_by=updated_by)
    if _record_for_workflow(registry, workflow_id) is not None:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} already exists in .loopplane/workflow_registry.json")

    record = _workflow_record(
        workflow_id=workflow_id,
        name=name,
        status=status,
        workflow_root=workflow_root,
        created_at=created_at or now,
        last_seen_at=now,
        summary=summary,
        path_values=path_values,
        extra_fields=extra_fields,
    )
    workflows = _workflow_records(registry)
    candidate = {**registry, "workflows": [*workflows, record]}
    _ensure_active_running_policy(candidate)
    _write_registry(project, candidate, generated_at=now)
    current_update = None
    if make_current:
        current_update = set_current_workflow(
            project,
            workflow_id,
            selection_reason=selection_reason,
            updated_by=updated_by,
            observed_at=now,
        )
    return _result(
        "workflow_created",
        project=project,
        workflow_id=workflow_id,
        record=record,
        current_update=current_update,
        updated_at=now,
    )


def archive_workflow(
    project_root: Path | str,
    workflow_id: str,
    *,
    reason: str | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    return _update_record(
        project_root,
        workflow_id,
        status="archived",
        updated_by=updated_by,
        updates={
            "archived": True,
            "archived_at": utc_timestamp(),
            "archived_by": updated_by,
            **({"archive_reason": reason} if reason else {}),
        },
        action="workflow_archived",
    )


def restore_workflow(
    project_root: Path | str,
    workflow_id: str,
    *,
    make_current: bool = True,
    selection_reason: str = "workflow_restored",
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    project = _project(project_root)
    registry = _load_or_create_registry(project, updated_by=updated_by)
    record = _record_for_workflow(registry, workflow_id)
    if record is None:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} is not in .loopplane/workflow_registry.json")
    status = str(record.get("status") or "").strip()
    archived = bool(record.get("archived")) or status == "archived"
    read_only = bool(record.get("read_only")) or status == "read_only_imported"
    if read_only:
        raise WorkflowLifecycleError(
            f"workflow {workflow_id!r} is read-only imported; fork it before mutating workflow history"
        )
    if not archived:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} is not archived and cannot be restored")
    result = _update_record(
        project,
        workflow_id,
        status="active",
        updated_by=updated_by,
        updates={
            "archived": False,
            "restored_at": utc_timestamp(),
            "restored_by": updated_by,
        },
        action="workflow_restored",
        skip_current_update=True,
    )
    if make_current:
        current_update = set_current_workflow(
            project,
            workflow_id,
            selection_reason=selection_reason,
            updated_by=updated_by,
            observed_at=result["updated_at"],
        )
        result["current_update"] = current_update
    return result


def fork_workflow(
    project_root: Path | str,
    source_workflow_id: str,
    *,
    new_workflow_id: str,
    name: str,
    workflow_root: str | None = None,
    make_current: bool = False,
    selection_reason: str = "workflow_forked",
    updated_by: str = CREATED_WITH,
    created_at: str | None = None,
    path_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = _project(project_root)
    registry = _load_or_create_registry(project, updated_by=updated_by)
    source = _record_for_workflow(registry, source_workflow_id)
    if source is None:
        raise WorkflowLifecycleError(f"source workflow {source_workflow_id!r} is not in .loopplane/workflow_registry.json")
    root = workflow_root or f".loopplane/workflows/{new_workflow_id}"
    summary = {
        "one_line": f"Forked from {source_workflow_id}.",
        "tasks_total": 0,
        "tasks_completed": 0,
        "tasks_blocked": 0,
    }
    result = create_workflow_record(
        project,
        workflow_id=new_workflow_id,
        name=name,
        workflow_root=root,
        status="forked",
        make_current=make_current,
        selection_reason=selection_reason,
        updated_by=updated_by,
        created_at=created_at,
        summary=summary,
        path_values=path_values,
        extra_fields={
            "forked_from": source_workflow_id,
            "forked_at": utc_timestamp(),
            "source_workflow_root": source.get("workflow_root"),
        },
    )
    result["status"] = "workflow_forked"
    return result


def import_workflow_record(
    project_root: Path | str,
    *,
    workflow_id: str,
    name: str,
    workflow_root: str,
    make_current: bool = False,
    selection_reason: str = "workflow_imported",
    updated_by: str = CREATED_WITH,
    summary: Mapping[str, Any] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = create_workflow_record(
        project_root,
        workflow_id=workflow_id,
        name=name,
        workflow_root=workflow_root,
        status="read_only_imported",
        make_current=make_current,
        selection_reason=selection_reason,
        updated_by=updated_by,
        summary=summary
        or {
            "one_line": "Read-only imported workflow history.",
            "tasks_total": 0,
            "tasks_completed": 0,
            "tasks_blocked": 0,
        },
        extra_fields={
            "read_only": True,
            "imported_at": utc_timestamp(),
            "imported_by": updated_by,
            **dict(extra_fields or {}),
        },
    )
    result["status"] = "workflow_imported"
    return result


def supersede_workflow(
    project_root: Path | str,
    workflow_id: str,
    *,
    superseded_by: str | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "superseded_at": utc_timestamp(),
        "superseded_by_loopplane": updated_by,
    }
    if superseded_by:
        _ensure_workflow_id(superseded_by)
        updates["superseded_by"] = superseded_by
    return _update_record(
        project_root,
        workflow_id,
        status="superseded",
        updated_by=updated_by,
        updates=updates,
        action="workflow_superseded",
    )


def mark_workflow_active(
    project_root: Path | str,
    workflow_id: str,
    *,
    workflow_title: str | None = None,
    summary: Mapping[str, Any] | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "archived": False,
        "activated_at": utc_timestamp(),
        "activated_by": updated_by,
    }
    title = str(workflow_title or "").strip()
    if title:
        updates["name"] = title
        updates["workflow_title"] = title
    if summary is not None:
        updates["summary"] = _summary(summary)
    return _update_record(
        project_root,
        workflow_id,
        status="active",
        updated_by=updated_by,
        updates=updates,
        action="workflow_active",
    )


def mark_workflow_runtime_status(
    project_root: Path | str,
    workflow_id: str,
    *,
    status: str,
    summary: Mapping[str, Any] | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    if status not in {"running", "paused", "stopped"}:
        raise WorkflowLifecycleError(f"unsupported runtime workflow status: {status!r}")
    now = utc_timestamp()
    updates: dict[str, Any] = {
        "archived": False,
        f"{status}_at": now,
        f"{status}_by": updated_by,
    }
    if status == "running":
        updates["last_started_at"] = now
        updates["last_started_by"] = updated_by
    if summary is not None:
        updates["summary"] = _summary(summary)
    return _update_record(
        project_root,
        workflow_id,
        status=status,
        updated_by=updated_by,
        updates=updates,
        action=f"workflow_{status}",
    )


def mark_workflow_completed(
    project_root: Path | str,
    workflow_id: str,
    *,
    completion_marker: str,
    summary: Mapping[str, Any] | None = None,
    final_verification_report: str | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "archived": False,
        "completion_marker": completion_marker,
        "completed_at": utc_timestamp(),
        "completed_by": updated_by,
    }
    if final_verification_report:
        updates["final_verification_report"] = final_verification_report
    if summary is not None:
        updates["summary"] = _summary(summary)
    return _update_record(
        project_root,
        workflow_id,
        status="completed",
        updated_by=updated_by,
        updates=updates,
        action="workflow_completed",
    )


def mark_workflow_objective_unresolved(
    project_root: Path | str,
    workflow_id: str,
    *,
    target_objective_ids: Sequence[str] | None = None,
    summary: Mapping[str, Any] | None = None,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "archived": False,
        "objective_unresolved_at": utc_timestamp(),
        "objective_unresolved_by": updated_by,
        "target_objective_ids": [str(item) for item in target_objective_ids or [] if str(item)],
    }
    if summary is not None:
        updates["summary"] = _summary(summary)
    return _update_record(
        project_root,
        workflow_id,
        status="objective_unresolved",
        updated_by=updated_by,
        updates=updates,
        action="workflow_objective_unresolved",
    )


def ensure_compatibility_workflow_metadata(
    project_root: Path | str,
    *,
    updated_by: str = CREATED_WITH,
) -> dict[str, Any]:
    project = _project(project_root)
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    if not workflow_path.is_file():
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "ok": True,
            "status": "no_op",
            "project_root": project.as_posix(),
            "created": [],
            "preserved": [],
            "warnings": [],
        }

    tracked = {
        "workspace": project / ".loopplane" / "workspace.json",
        "workflow_registry": project / ".loopplane" / "workflow_registry.json",
        "current_workflow": project / ".loopplane" / "current_workflow.json",
    }
    before = {key: path.is_file() for key, path in tracked.items()}
    if all(before.values()):
        boundary_defaults = _ensure_workspace_boundary_defaults(
            project,
            tracked["workspace"],
            updated_by=updated_by,
        )
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "ok": True,
            "status": "updated" if boundary_defaults else "current",
            "project_root": project.as_posix(),
            "created": [],
            "preserved": sorted(_path_for_record(project, path) for path in tracked.values()),
            "modified": boundary_defaults,
            "warnings": (
                ["defaulted missing workspace boundary config fields: " + ", ".join(boundary_defaults)]
                if boundary_defaults
                else []
            ),
        }
    if any(before.values()):
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "ok": True,
            "status": "partial_v16_identity",
            "project_root": project.as_posix(),
            "created": [],
            "preserved": sorted(
                _path_for_record(project, path) for key, path in tracked.items() if before[key]
            ),
            "warnings": [],
        }

    registry = _load_or_create_registry(project, updated_by=updated_by)
    workflow_config = load_workflow_config(project)
    workflow_id = str(workflow_config.get("workflow_id") or "")
    _ensure_workflow_id(workflow_id)
    if _record_for_workflow(registry, workflow_id) is None:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} is not in .loopplane/workflow_registry.json")

    current_path = tracked["current_workflow"]
    if not current_path.is_file():
        set_current_workflow(
            project,
            workflow_id,
            selection_reason="compatibility_flat_workflow",
            updated_by=updated_by,
        )

    after = {key: path.is_file() for key, path in tracked.items()}
    created = [
        _path_for_record(project, tracked[key])
        for key, exists_before in before.items()
        if not exists_before and after.get(key)
    ]
    preserved = [
        _path_for_record(project, tracked[key])
        for key, exists_before in before.items()
        if exists_before and after.get(key)
    ]
    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "ok": True,
        "status": "created" if created else "current",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "created": sorted(created),
        "preserved": sorted(preserved),
        "registry": registry,
        "warnings": [],
    }


def set_current_workflow(
    project_root: Path | str,
    workflow_id: str,
    *,
    selection_reason: str,
    updated_by: str = CREATED_WITH,
    observed_at: str | None = None,
) -> dict[str, Any]:
    project = _project(project_root)
    _ensure_workflow_id(workflow_id)
    now = observed_at or utc_timestamp()
    registry = _load_or_create_registry(project, updated_by=updated_by)
    record = _record_for_workflow(registry, workflow_id)
    if record is None:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} is not in .loopplane/workflow_registry.json")
    registry["workflows"] = [
        {**record, "last_seen_at": now} if record.get("workflow_id") == workflow_id else record
        for record in _workflow_records(registry)
    ]
    _write_registry(project, registry, generated_at=now)

    workspace = _load_workspace(project)
    workspace["current_workflow_id"] = workflow_id
    _atomic_write_json(project / ".loopplane" / "workspace.json", workspace)

    current = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": str(workspace["workspace_id"]),
        "current_workflow_id": workflow_id,
        "selection_reason": _non_empty(selection_reason, "selection_reason"),
        "updated_at": now,
        "updated_by": _non_empty(updated_by, "updated_by"),
    }
    _atomic_write_json(project / ".loopplane" / "current_workflow.json", current)
    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "ok": True,
        "status": "current_workflow_updated",
        "workflow_id": workflow_id,
        "selection_reason": selection_reason,
        "updated_at": now,
        "updated_by": updated_by,
        "current_workflow_path": ".loopplane/current_workflow.json",
    }


def _update_record(
    project_root: Path | str,
    workflow_id: str,
    *,
    status: str,
    updated_by: str,
    updates: Mapping[str, Any],
    action: str,
    skip_current_update: bool = False,
) -> dict[str, Any]:
    project = _project(project_root)
    _ensure_workflow_id(workflow_id)
    _ensure_status(status)
    now = utc_timestamp()
    registry = _load_or_create_registry(project, updated_by=updated_by)
    workflows = _workflow_records(registry)
    changed_record: dict[str, Any] | None = None
    next_workflows: list[dict[str, Any]] = []
    for record in workflows:
        if record.get("workflow_id") != workflow_id:
            next_workflows.append(record)
            continue
        changed_record = dict(record)
        changed_record.update(dict(updates))
        changed_record["status"] = status
        changed_record["last_seen_at"] = now
        changed_record.setdefault("read_only", False)
        changed_record.setdefault("archived", status == "archived")
        if status == "read_only_imported":
            changed_record["read_only"] = True
        if status == "archived":
            changed_record["archived"] = True
        next_workflows.append(changed_record)
    if changed_record is None:
        raise WorkflowLifecycleError(f"workflow {workflow_id!r} is not in .loopplane/workflow_registry.json")
    candidate = {**registry, "workflows": next_workflows}
    _ensure_active_running_policy(candidate)
    _write_registry(project, candidate, generated_at=now)
    return _result(
        action,
        project=project,
        workflow_id=workflow_id,
        record=changed_record,
        current_update=None if skip_current_update else None,
        updated_at=now,
    )


def _load_or_create_registry(project: Path, *, updated_by: str) -> dict[str, Any]:
    registry_path = project / ".loopplane" / "workflow_registry.json"
    if registry_path.is_file():
        registry = _load_json_object(registry_path)
        _ensure_active_running_policy(registry)
        return registry
    return _create_compatibility_registry(project, updated_by=updated_by)


def _create_compatibility_registry(project: Path, *, updated_by: str) -> dict[str, Any]:
    workflow_config = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow_config)
    workflow_id = str(workflow_config.get("workflow_id") or "")
    _ensure_workflow_id(workflow_id)
    now = utc_timestamp()
    workspace = _load_workspace(project, required=False)
    if not workspace:
        workspace = {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "workspace_id": _new_workspace_id(),
            "project_root": ".",
            "loopplane_dir": ".loopplane",
            "repo_root": repository_root_value(project),
            "created_at": str(workflow_config.get("created_at") or now),
            "created_by_loopplane_version": CREATED_WITH,
            "workspace_boundary": "project_root",
            "allow_out_of_boundary_writes": False,
            "single_active_running_workflow": True,
            "layout": "compatibility_flat",
            "current_workflow_id": workflow_id,
        }
        _atomic_write_json(project / ".loopplane" / "workspace.json", workspace)
    workspace_id = str(workspace.get("workspace_id") or "")
    _ensure_workspace_id(workspace_id)
    registry = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "generated_at": now,
        "workflows": [
            _workflow_record(
                workflow_id=workflow_id,
                name="v1.5 compatibility-flat workflow",
                status=_status_from_runtime_state(paths),
                workflow_root=".loopplane/",
                created_at=str(workflow_config.get("created_at") or now),
                last_seen_at=now,
                summary=_summary_from_read_models(paths),
                path_values=paths.values,
            )
        ],
    }
    _atomic_write_json(project / ".loopplane" / "workflow_registry.json", registry)
    current_path = project / ".loopplane" / "current_workflow.json"
    if not current_path.is_file():
        set_current_workflow(
            project,
            workflow_id,
            selection_reason="compatibility_flat_workflow",
            updated_by=updated_by,
            observed_at=now,
        )
    return _load_json_object(project / ".loopplane" / "workflow_registry.json")


def _workflow_record(
    *,
    workflow_id: str,
    name: str,
    status: str,
    workflow_root: str,
    created_at: str,
    last_seen_at: str,
    summary: Mapping[str, Any] | None,
    path_values: Mapping[str, Any] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_workflow_id(workflow_id)
    _ensure_status(status)
    root = _workflow_root(workflow_root)
    values = _record_path_values(root, path_values)
    record = {
        "workflow_id": workflow_id,
        "name": _non_empty(name, "name"),
        "status": status,
        "workflow_root": root,
        "created_at": _non_empty(created_at, "created_at"),
        "last_seen_at": _non_empty(last_seen_at, "last_seen_at"),
        "plan_file": values["plan_file"],
        "read_models_dir": values["read_models_dir"],
        "runtime_dir": values["runtime_dir"],
        "requests_dir": values["requests_dir"],
        "completion_marker": f"{values['runtime_dir']}/plan_loop_complete.json",
        "read_only": status == "read_only_imported",
        "archived": status == "archived",
        "summary": _summary(summary),
    }
    if root.rstrip("/") != ".loopplane":
        record["workflow_config_file"] = f"{root.rstrip('/')}/config/workflow.json"
    if extra_fields:
        record.update(dict(extra_fields))
        if record.get("read_only") is True:
            record["read_only"] = True
        if record.get("archived") is True:
            record["archived"] = True
    return record


def _record_path_values(workflow_root: str, path_values: Mapping[str, Any] | None) -> dict[str, str]:
    defaults = default_workflow_path_values(workflow_root=workflow_root.rstrip("/"))
    if path_values:
        values = {field: str(path_values.get(field) or defaults[field]) for field in DEFAULT_WORKFLOW_PATHS}
    elif workflow_root.rstrip("/") == ".loopplane":
        values = defaults
    else:
        values = defaults
    return {key: _project_relative_posix(key, value) for key, value in values.items()}


def _summary(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(summary or {})
    return {
        "one_line": str(source.get("one_line") or source.get("summary") or "Workflow history record.").strip(),
        "tasks_total": _non_negative_int(source.get("tasks_total")),
        "tasks_completed": _non_negative_int(source.get("tasks_completed")),
        "tasks_blocked": _non_negative_int(source.get("tasks_blocked")),
        **{str(key): value for key, value in source.items() if key not in {"one_line", "summary", "tasks_total", "tasks_completed", "tasks_blocked"}},
    }


def _summary_from_read_models(paths: WorkflowPaths) -> dict[str, Any]:
    status = _load_json_object(paths.read_models_dir / "workflow_status.json", required=False)
    progress = status.get("progress") if isinstance(status.get("progress"), Mapping) else {}
    return {
        "one_line": str(status.get("summary") or status.get("status") or "v1.5 compatibility-flat workflow."),
        "tasks_total": _non_negative_int(progress.get("total_tasks")),
        "tasks_completed": _non_negative_int(progress.get("completed_tasks")),
        "tasks_blocked": _non_negative_int(progress.get("blocked_tasks")),
    }


def _status_from_runtime_state(paths: WorkflowPaths) -> str:
    state = _load_json_object(paths.runtime_dir / "state.json", required=False)
    status = str(state.get("status") or "").strip().lower()
    if status in WORKFLOW_HISTORY_STATUSES:
        return status
    if status in {"initialized", "waiting_config"}:
        return "draft"
    if status in {"final_verification_failed", "failed_validation"}:
        return "failed"
    return "draft"


def _write_registry(project: Path, registry: Mapping[str, Any], *, generated_at: str) -> None:
    payload = dict(registry)
    payload["generated_at"] = generated_at
    _ensure_active_running_policy(payload)
    _atomic_write_json(project / ".loopplane" / "workflow_registry.json", payload)


def _workflow_records(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    workflows = registry.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        raise WorkflowLifecycleError(".loopplane/workflow_registry.json workflows must be a list")
    records: list[dict[str, Any]] = []
    for index, record in enumerate(workflows):
        if not isinstance(record, Mapping):
            raise WorkflowLifecycleError(f".loopplane/workflow_registry.json workflows[{index}] must be an object")
        records.append(dict(record))
    return records


def _record_for_workflow(registry: Mapping[str, Any], workflow_id: str) -> dict[str, Any] | None:
    for record in _workflow_records(registry):
        if record.get("workflow_id") == workflow_id:
            return record
    return None


def _ensure_active_running_policy(registry: Mapping[str, Any]) -> None:
    active = [
        f"{record.get('workflow_id')}:{record.get('status')}"
        for record in _workflow_records(registry)
        if str(record.get("status") or "") in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    if len(active) > 1:
        raise WorkflowLifecycleError(
            "one active-running workflow per workspace is allowed by default; found " + ", ".join(active)
        )


def _load_workspace(project: Path, *, required: bool = True) -> dict[str, Any]:
    workspace = _load_json_object(project / ".loopplane" / "workspace.json", required=required)
    if not workspace:
        return {}
    workspace_id = str(workspace.get("workspace_id") or "")
    _ensure_workspace_id(workspace_id)
    return workspace


def _ensure_workspace_boundary_defaults(project: Path, workspace_path: Path, *, updated_by: str) -> list[str]:
    workspace = _load_json_object(workspace_path)
    updates: dict[str, Any] = {}
    if "workspace_boundary" not in workspace:
        updates["workspace_boundary"] = "project_root"
    if "allow_out_of_boundary_writes" not in workspace:
        updates["allow_out_of_boundary_writes"] = False
    if not updates:
        return []
    workspace.update(updates)
    if "boundary_defaults_added_at" not in workspace:
        workspace["boundary_defaults_added_at"] = utc_timestamp()
    workspace["boundary_defaults_added_by"] = _non_empty(updated_by, "updated_by")
    _atomic_write_json(workspace_path, workspace)
    return sorted(updates)


def _load_json_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise WorkflowLifecycleError(f"{path}: required JSON file is missing") from None
        return {}
    except json.JSONDecodeError as error:
        raise WorkflowLifecycleError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(data, Mapping):
        raise WorkflowLifecycleError(f"{path}: JSON root must be an object")
    return dict(data)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _project(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _workflow_root(value: str) -> str:
    raw = _non_empty(value, "workflow_root")
    path = PurePosixPath(raw.rstrip("/") or raw)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise WorkflowPathError(f"workflow_root must stay inside the project root: {value}")
    normalized = path.as_posix()
    return ".loopplane/" if raw.rstrip("/") == ".loopplane" and raw.endswith("/") else normalized


def _project_relative_posix(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowLifecycleError(f"{field} must be a non-empty string")
    if "\\" in value:
        raise WorkflowLifecycleError(f"{field} must use POSIX-style '/' separators")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise WorkflowLifecycleError(f"{field} must stay inside the project root")
    return path.as_posix()


def _ensure_workflow_id(value: str) -> None:
    if not isinstance(value, str) or WORKFLOW_ID_RE.match(value) is None:
        raise WorkflowLifecycleError(f"workflow_id must match {WORKFLOW_ID_RE.pattern}")


def _ensure_workspace_id(value: str) -> None:
    if not isinstance(value, str) or WORKSPACE_ID_RE.match(value) is None:
        raise WorkflowLifecycleError(f"workspace_id must match {WORKSPACE_ID_RE.pattern}")


def _ensure_status(value: str) -> None:
    if value not in WORKFLOW_HISTORY_STATUSES:
        raise WorkflowLifecycleError(f"workflow status {value!r} is not supported")


def _non_empty(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowLifecycleError(f"{field} must be a non-empty string")
    return text


def _non_negative_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _new_workspace_id() -> str:
    return f"ws_{uuid.uuid4().hex}"


def _result(
    status: str,
    *,
    project: Path,
    workflow_id: str,
    record: Mapping[str, Any],
    current_update: Mapping[str, Any] | None,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "updated_at": updated_at,
        "registry_path": ".loopplane/workflow_registry.json",
        "record": dict(record),
        "current_update": dict(current_update) if isinstance(current_update, Mapping) else None,
    }
