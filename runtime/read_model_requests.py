from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import WorkflowPaths


SCHEMA_VERSION = "1.5"
READ_MODEL_REBUILD_REQUEST_FILENAME = "read_model_rebuild_request.json"
RESERVED_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "request_id",
        "requested_at",
        "requested_by",
        "status",
        "reason",
        "run_id",
    }
)


def request_read_model_rebuild(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    run_id: str | None,
    reason: str,
    requested_by: str,
    source_path: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    requested_at = utc_timestamp()
    source_path_value = _path_for_record(paths.project_root, source_path) if source_path is not None else None
    request_path = paths.runtime_dir / READ_MODEL_REBUILD_REQUEST_FILENAME
    existing = _read_json_object(request_path)
    if _same_pending_request(
        existing,
        workflow_id=workflow_id,
        run_id=run_id or "",
        reason=reason,
        requested_by=requested_by,
        source_path=source_path_value,
    ):
        coalesced = dict(existing)
        coalesced["last_requested_at"] = requested_at
        coalesced["coalesced_count"] = int(coalesced.get("coalesced_count") or 1) + 1
        coalesced["coalesced"] = True
        _atomic_write_json(request_path, coalesced)
        return coalesced
    if _same_pending_workflow(existing, workflow_id=workflow_id):
        coalesced = _coalesced_workflow_request(
            existing,
            requested_at=requested_at,
            run_id=run_id or "",
            reason=reason,
            requested_by=requested_by,
            source_path=source_path_value,
            extra=extra,
        )
        _atomic_write_json(request_path, coalesced)
        return coalesced
    request: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "request_id": f"rmr_{uuid.uuid4().hex[:8]}",
        "requested_at": requested_at,
        "requested_by": requested_by,
        "status": "pending",
        "reason": reason,
        "run_id": run_id or "",
    }
    if source_path_value is not None:
        request["source_path"] = source_path_value
    if extra:
        for key, value in extra.items():
            request[key if key not in RESERVED_REQUEST_FIELDS else f"source_{key}"] = value

    _atomic_write_json(request_path, request)
    try:
        from runtime.scheduler import append_event

        event = append_event(
            paths,
            workflow_id=workflow_id,
            run_id=run_id or request["request_id"],
            event_type="read_model_rebuild_requested",
            data=request,
        )
    except Exception as error:  # pragma: no cover - event logging is advisory to the request file.
        request["event_id"] = None
        request["warnings"] = [f"read_model_rebuild_requested event could not be written: {error}"]
    else:
        request["event_id"] = event.get("event_id") if isinstance(event, Mapping) else None
    return request


def _same_pending_request(
    existing: Mapping[str, Any] | None,
    *,
    workflow_id: str,
    run_id: str,
    reason: str,
    requested_by: str,
    source_path: str | None,
) -> bool:
    if not isinstance(existing, Mapping) or existing.get("status") != "pending":
        return False
    return (
        str(existing.get("workflow_id") or "") == str(workflow_id)
        and str(existing.get("run_id") or "") == str(run_id or "")
        and str(existing.get("reason") or "") == str(reason)
        and str(existing.get("requested_by") or "") == str(requested_by)
        and str(existing.get("source_path") or "") == str(source_path or "")
    )


def _same_pending_workflow(existing: Mapping[str, Any] | None, *, workflow_id: str) -> bool:
    return (
        isinstance(existing, Mapping)
        and existing.get("status") == "pending"
        and str(existing.get("workflow_id") or "") == str(workflow_id)
    )


def _coalesced_workflow_request(
    existing: Mapping[str, Any] | None,
    *,
    requested_at: str,
    run_id: str,
    reason: str,
    requested_by: str,
    source_path: str | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    coalesced = dict(existing or {})
    reasons = _unique_strings([*list(coalesced.get("pending_reasons") or []), coalesced.get("reason"), reason])
    run_ids = _unique_strings([*list(coalesced.get("pending_run_ids") or []), coalesced.get("run_id"), run_id])
    requested_by_values = _unique_strings(
        [*list(coalesced.get("pending_requested_by") or []), coalesced.get("requested_by"), requested_by]
    )
    source_paths = _unique_strings([*list(coalesced.get("pending_source_paths") or []), coalesced.get("source_path"), source_path])
    coalesced.setdefault("first_reason", coalesced.get("reason"))
    coalesced.setdefault("first_run_id", coalesced.get("run_id"))
    coalesced.setdefault("first_requested_by", coalesced.get("requested_by"))
    if coalesced.get("source_path"):
        coalesced.setdefault("first_source_path", coalesced.get("source_path"))
    coalesced["reason"] = reason
    coalesced["run_id"] = run_id
    coalesced["requested_by"] = requested_by
    if source_path is not None:
        coalesced["source_path"] = source_path
    coalesced["last_requested_at"] = requested_at
    coalesced["coalesced_count"] = int(coalesced.get("coalesced_count") or 1) + 1
    coalesced["coalesced"] = True
    coalesced["coalesced_scope"] = "workflow_pending"
    coalesced["latest_reason"] = reason
    coalesced["latest_run_id"] = run_id
    coalesced["latest_requested_by"] = requested_by
    if source_path is not None:
        coalesced["latest_source_path"] = source_path
    coalesced["pending_reasons"] = reasons
    coalesced["pending_run_ids"] = run_ids
    coalesced["pending_requested_by"] = requested_by_values
    coalesced["pending_source_paths"] = source_paths
    if extra:
        merged_extra = dict(coalesced.get("pending_extra") if isinstance(coalesced.get("pending_extra"), Mapping) else {})
        for key, value in extra.items():
            merged_key = key if key not in RESERVED_REQUEST_FIELDS else f"source_{key}"
            merged_extra[merged_key] = value
        coalesced["pending_extra"] = merged_extra
    return coalesced


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _path_for_record(project: Path | None, path: Path) -> str:
    if project is not None:
        try:
            return path.resolve().relative_to(project.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return path.as_posix()
