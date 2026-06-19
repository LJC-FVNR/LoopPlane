from __future__ import annotations

import json
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.active_projections import sync_active_workflow_projections
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SUCCESS, EXIT_VERSION_CONTROL_UNAVAILABLE, has_text
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.read_models import rebuild_read_models
from runtime.scheduler import SCHEMA_VERSION, append_event
from runtime.version_control import create_git_checkpoint


def write_project_brief(
    project_root: Path | str,
    brief_content: str | None,
    *,
    source: str,
    force: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    if brief_content is None:
        return _failure(
            project=project,
            workflow_id=None,
            status="missing_input",
            message="Brief content is required. Use --text, --file, or --stdin.",
            started_at=started_at,
        )
    if not brief_content.strip():
        return _failure(
            project=project,
            workflow_id=None,
            status="empty_input",
            message="Brief content must not be empty.",
            started_at=started_at,
        )

    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(
            project=project,
            workflow_id=None,
            status="waiting_config",
            message=f"Unable to load workflow configuration: {error}",
            started_at=started_at,
        )

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    brief_path = paths.brief_file
    rendered = _render_brief(
        brief_content,
        workflow_id=workflow_id,
        brief_file=paths.value("brief_file"),
        source=source,
    )
    new_sha = _sha256_text(rendered)
    old_text = _read_text_or_none(brief_path)
    old_sha = _sha256_text(old_text) if old_text is not None else None
    exists = old_text is not None

    if exists and old_text != rendered and not force:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="overwrite_refused",
            message=(
                f"{paths.value('brief_file')} already exists with different content. "
                "Use --force to replace it."
            ),
            started_at=started_at,
            extra={
                "brief_file": _path_for_record(project, brief_path),
                "changed": False,
                "old_sha256": old_sha,
                "new_sha256": new_sha,
            },
        )

    if old_text == rendered:
        projection_sync = sync_active_workflow_projections(
            project,
            workflow_config,
            paths,
            reason="write_brief_unchanged",
        )
        rebuild = rebuild_read_models(project, write=True)
        warnings = list(rebuild.get("warnings") or [])
        warnings.extend(str(warning) for warning in projection_sync.get("warnings") or [])
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "unchanged",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "brief_file": _path_for_record(project, brief_path),
            "source": source,
            "changed": False,
            "created": False,
            "updated": False,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "event": None,
            "checkpoint": None,
            "checkpoint_status": "not_needed",
            "projection_sync": projection_sync,
            "read_model_rebuild": rebuild,
            "planning_invalidated": False,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "warnings": _dedupe_text(warnings),
            "errors": [],
        }

    _atomic_write_text(brief_path, rendered)
    projection_sync = sync_active_workflow_projections(
        project,
        workflow_config,
        paths,
        reason=f"write_brief_{'created' if not exists else 'updated'}",
    )
    action = "created" if not exists else "updated"
    event = append_event(
        paths,
        workflow_id=workflow_id,
        event_type=f"project_brief_{action}",
        data={
            "brief_file": paths.value("brief_file"),
            "source": source,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "forced": bool(force),
        },
    )
    planning_invalidated = _invalidate_planning_state(
        paths,
        workflow_id=workflow_id,
        reason=f"PROJECT_BRIEF.md {action} by loopplane write-brief.",
        event_id=str(event.get("event_id") or ""),
        brief_sha256=new_sha,
    )
    checkpoint = None
    checkpoint_status = "not_applicable"
    warnings: list[str] = []
    errors: list[str] = []
    warnings.extend(str(warning) for warning in projection_sync.get("warnings") or [])
    if projection_sync.get("ok") is not True:
        errors.extend(str(error) for error in projection_sync.get("errors") or ["Active workflow projection sync failed."])
    if _version_control_enabled(paths):
        checkpoint = create_git_checkpoint(
            project,
            reason="brief_created" if action == "created" else "brief_updated",
        )
        checkpoint_status = str(checkpoint.get("status") or "unknown")
        warnings.extend(str(warning) for warning in checkpoint.get("warnings") or [])
        if checkpoint.get("ok") is not True:
            errors.extend(str(error) for error in checkpoint.get("errors") or ["Brief checkpoint failed."])

    rebuild = rebuild_read_models(project, write=True)
    warnings.extend(str(warning) for warning in rebuild.get("warnings") or [])
    if rebuild.get("ok") is not True:
        errors.extend(str(error) for error in rebuild.get("errors") or ["Read-model rebuild failed."])

    ok = not errors
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": action if ok else ("checkpoint_failed" if checkpoint and checkpoint.get("ok") is not True else "read_model_rebuild_failed"),
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "brief_file": _path_for_record(project, brief_path),
        "source": source,
        "changed": True,
        "created": action == "created",
        "updated": action == "updated",
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "event": _compact_event(event),
        "checkpoint": checkpoint,
        "checkpoint_status": checkpoint_status,
        "projection_sync": projection_sync,
        "read_model_rebuild": rebuild,
        "planning_invalidated": planning_invalidated,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def write_brief_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if has_text(
        result,
        (
            "git is unavailable",
            "version control is unavailable",
            "git repository is unavailable",
            "local git init",
        ),
        "errors",
        "warnings",
        "message",
    ):
        return EXIT_VERSION_CONTROL_UNAVAILABLE
    return EXIT_INVALID_CONFIG


def format_write_brief_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane write-brief: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"brief_file: {result.get('brief_file') or 'unknown'}",
        f"changed: {str(bool(result.get('changed'))).lower()}",
    ]
    event = result.get("event")
    if isinstance(event, Mapping) and event.get("event_id"):
        lines.append(f"event_id: {event.get('event_id')}")
    checkpoint = result.get("checkpoint")
    if isinstance(checkpoint, Mapping):
        checkpoint_record = checkpoint.get("checkpoint")
        if isinstance(checkpoint_record, Mapping):
            lines.append(f"checkpoint_id: {checkpoint_record.get('checkpoint_id')}")
            lines.append(f"checkpoint_reason: {checkpoint_record.get('reason')}")
        else:
            lines.append(f"checkpoint_status: {checkpoint.get('status')}")
    else:
        lines.append(f"checkpoint_status: {result.get('checkpoint_status') or 'not_applicable'}")
    rebuild = result.get("read_model_rebuild")
    if isinstance(rebuild, Mapping):
        lines.append(f"read_models: {rebuild.get('status', 'unknown')}")
    if result.get("planning_invalidated"):
        lines.append("planning_state: invalidated")
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def _render_brief(content: str, *, workflow_id: str, brief_file: str, source: str) -> str:
    text = content.strip()
    if text.startswith("# Project Brief"):
        return text.rstrip() + "\n"
    created_at = utc_timestamp()
    return f"""# Project Brief

## Metadata

- workflow_id: {workflow_id}
- updated_at: {created_at}
- brief_file: {brief_file}
- source: {source}

## User Request

{text}

## Goals

- To be derived from the user request during planning.

## Available Resources

- Current project workspace and any resources explicitly available to the
  workflow.

## Constraints

- Respect LoopPlane protocol rules, configured permission policy, approval gates,
  protected paths, and local environment limits.

## Expected Deliverables

- To be derived from the user request and active plan.

## Success Signals

- To be defined by the planner and validators as observable acceptance
  criteria.

## Non-goals

- No non-goals were specified in this brief update.

## Assumptions

- The planner may record assumptions when converting this brief into the active
  plan.

## Open Questions

- None recorded by `loopplane write-brief`.
"""


def _invalidate_planning_state(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    reason: str,
    event_id: str,
    brief_sha256: str,
) -> bool:
    planning_artifacts = (
        paths.planning_dir / "PLAN_DRAFT.md",
        paths.planning_dir / "plan_readiness_report.json",
        paths.planning_dir / "audit_report.json",
        paths.planning_dir / "planning_state.json",
    )
    if not any(path.exists() for path in planning_artifacts):
        _update_runtime_brief_metadata(paths, workflow_id=workflow_id, event_id=event_id, brief_sha256=brief_sha256)
        return False

    now = utc_timestamp()
    state = _read_json_object(paths.planning_dir / "planning_state.json", default={})
    invalidation = {
        "at": now,
        "event_id": event_id,
        "reason": reason,
        "brief_sha256": brief_sha256,
    }
    if not isinstance(state, Mapping):
        state = {}
    next_state = dict(state)
    next_state.setdefault("schema_version", SCHEMA_VERSION)
    next_state["workflow_id"] = workflow_id
    next_state["status"] = "brief_changed"
    next_state["ready_for_activation"] = False
    next_state["updated_at"] = now
    next_state["brief_invalidation"] = invalidation
    revision_reasons = list(next_state.get("revision_reasons") or [])
    revision_reasons.append({"source": "write_brief", "message": reason, "event_id": event_id})
    next_state["revision_reasons"] = revision_reasons
    _atomic_write_json(paths.planning_dir / "planning_state.json", next_state)
    _update_runtime_brief_metadata(
        paths,
        workflow_id=workflow_id,
        event_id=event_id,
        brief_sha256=brief_sha256,
        planning_invalidation=invalidation,
    )
    return True


def _update_runtime_brief_metadata(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    event_id: str,
    brief_sha256: str,
    planning_invalidation: Mapping[str, Any] | None = None,
) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_object(state_path, default={})
    if not isinstance(state, Mapping):
        state = {}
    next_state = dict(state)
    next_state.setdefault("schema_version", SCHEMA_VERSION)
    next_state["workflow_id"] = workflow_id
    next_state["updated_at"] = utc_timestamp()
    next_state["brief"] = {
        "last_update_event_id": event_id,
        "sha256": brief_sha256,
        "updated_at": next_state["updated_at"],
    }
    if planning_invalidation is not None:
        planning = dict(next_state.get("planning") or {})
        planning["status"] = "brief_changed"
        planning["ready_for_activation"] = False
        planning["brief_invalidation"] = dict(planning_invalidation)
        next_state["planning"] = planning
    _atomic_write_json(state_path, next_state)


def _version_control_enabled(paths: WorkflowPaths) -> bool:
    config = _read_json_object(paths.version_control_config_file, default={})
    if not isinstance(config, Mapping):
        return False
    return config.get("enabled") is not False


def _failure(
    *,
    project: Path,
    workflow_id: str | None,
    status: str,
    message: str,
    started_at: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "message": message,
        "changed": False,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "warnings": [],
        "errors": [message],
    }
    if extra:
        result.update(dict(extra))
    return result


def _compact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence") or event.get("seq"),
        "event_type": event.get("event_type"),
        "timestamp": event.get("timestamp") or event.get("ts"),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(dict(data), indent=2, sort_keys=True) + "\n")


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _sha256_text(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


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
