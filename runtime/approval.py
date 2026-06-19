from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config


SCHEMA_VERSION = "1.5"
APPROVAL_REQUESTS_FILENAME = "human_approval_requests.jsonl"
APPROVAL_RESPONSES_FILENAME = "human_approval_responses.jsonl"
ALLOWED_APPROVAL_DECISIONS = frozenset({"approved", "rejected", "expired", "superseded"})
CLOSED_APPROVAL_DECISIONS = ALLOWED_APPROVAL_DECISIONS
DEFAULT_APPROVAL_EXPIRY_HOURS = 24


def load_approval_policy(paths: WorkflowPaths) -> dict[str, Any]:
    security_path = paths.config_file("security.json")
    security = _read_json_object(security_path, default={})
    approval = security.get("approval") if isinstance(security, Mapping) else {}
    if not isinstance(approval, Mapping):
        approval = {}
    enabled = approval.get("enabled") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": enabled,
        "mode": "interactive" if enabled else "disabled",
        "default_action_when_disabled": str(approval.get("default_action_when_disabled") or "auto_authorize"),
        "source": _path_for_record(paths.project_root, security_path),
        "require_for_scope_change": approval.get("require_for_scope_change") is not False,
        "require_for_destructive_file_ops": approval.get("require_for_destructive_file_ops") is not False,
        "require_for_external_publish": approval.get("require_for_external_publish") is not False,
        "require_for_long_running_jobs": approval.get("require_for_long_running_jobs") is not False,
        "require_for_partial_acceptance": approval.get("require_for_partial_acceptance") is not False,
        "require_for_skipping_active_tasks": approval.get("require_for_skipping_active_tasks") is not False,
    }


def load_approval_status(project_root: Path | str, *, include_all: bool = False) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    policy = load_approval_policy(paths)
    requests = read_approval_requests(paths)
    responses = read_approval_responses(paths)
    records = [
        approval_record_status(request, responses=responses, now=utc_timestamp())
        for request in requests
    ]
    if not include_all:
        records = [record for record in records if record["status"] == "pending"]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "approval_disabled" if not policy["enabled"] else "ok",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "approval_policy": policy,
        "requests_path": _path_for_record(project, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME),
        "responses_path": _path_for_record(project, paths.runtime_dir / APPROVAL_RESPONSES_FILENAME),
        "pending_count": sum(1 for record in records if record["status"] == "pending"),
        "approvals": records,
        "errors": [],
        "warnings": [] if policy["enabled"] else ["Interactive approval is disabled by security.json."],
    }


def record_approval_response(
    project_root: Path | str,
    approval_id: str,
    *,
    decision: str,
    approved_by: str = "user",
    notes: str = "",
    source: str = "cli",
    allow_disabled_policy: bool = False,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    policy = load_approval_policy(paths)
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in ALLOWED_APPROVAL_DECISIONS:
        return _response_failure(
            project=project,
            workflow_id=workflow_id,
            status="invalid_decision",
            message=f"Decision must be one of: {', '.join(sorted(ALLOWED_APPROVAL_DECISIONS))}.",
        )
    if not approval_id.strip():
        return _response_failure(
            project=project,
            workflow_id=workflow_id,
            status="missing_approval_id",
            message="approval_id is required.",
        )
    if not policy["enabled"] and not allow_disabled_policy:
        return _response_failure(
            project=project,
            workflow_id=workflow_id,
            status="approval_disabled",
            message="Interactive approval is disabled in security.json; refusing to record an approval decision.",
            approval_policy=policy,
        )

    requests = read_approval_requests(paths)
    responses = read_approval_responses(paths)
    request = _request_by_id(requests, approval_id)
    if request is None:
        return _response_failure(
            project=project,
            workflow_id=workflow_id,
            status="approval_not_found",
            message=f"No approval request exists for {approval_id}.",
            approval_policy=policy,
        )
    status = approval_record_status(request, responses=responses, now=utc_timestamp())
    if status["status"] != "pending":
        return _response_failure(
            project=project,
            workflow_id=workflow_id,
            status="approval_already_closed",
            message=f"Approval {approval_id} is already {status['status']}.",
            approval_policy=policy,
            approval=status,
        )

    response = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "responded_at": utc_timestamp(),
        "decision": normalized_decision,
        "approved_by": approved_by or "user",
        "scope": str(request.get("scope") or ""),
        "notes": notes,
        "source": f"{source}:override_disabled_policy" if allow_disabled_policy and not policy["enabled"] else source,
        "workflow_id": str(request.get("workflow_id") or workflow_id),
    }
    for field in ("task_id", "run_id", "type"):
        if request.get(field) is not None:
            response[field] = request[field]
    _append_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME, response)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": normalized_decision,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "approval_policy": policy,
        "approval": approval_record_status(request, responses=[*responses, response], now=response["responded_at"]),
        "response": response,
        "errors": [],
        "warnings": (
            ["Interactive approval is disabled in security.json; response was recorded because override_disabled_policy was explicitly requested."]
            if allow_disabled_policy and not policy["enabled"]
            else []
        ),
    }


def read_approval_requests(paths: WorkflowPaths) -> list[dict[str, Any]]:
    return _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME)


def read_approval_responses(paths: WorkflowPaths) -> list[dict[str, Any]]:
    return _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME)


def approval_record_status(
    request: Mapping[str, Any],
    *,
    responses: Sequence[Mapping[str, Any]],
    now: str | None = None,
) -> dict[str, Any]:
    approval_id = str(request.get("approval_id") or request.get("request_id") or "")
    response = latest_response_for_approval(responses, approval_id)
    if response is not None:
        decision = str(response.get("decision") or response.get("status") or "").lower()
        status = decision if decision in ALLOWED_APPROVAL_DECISIONS else "closed"
    else:
        status = str(request.get("status") or "pending").lower()
        if status == "pending" and _is_expired(request.get("expires_at"), now=now):
            status = "expired"
    record = dict(request)
    record["approval_id"] = approval_id
    record["status"] = status
    if response is not None:
        record["response"] = dict(response)
        record["decision"] = response.get("decision") or response.get("status")
        record["responded_at"] = response.get("responded_at") or response.get("handled_at")
    return record


def latest_response_for_approval(
    responses: Sequence[Mapping[str, Any]],
    approval_id: str | None,
) -> dict[str, Any] | None:
    if not approval_id:
        return None
    matches = [
        dict(response)
        for response in responses
        if str(response.get("approval_id") or response.get("request_id") or "") == str(approval_id)
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("responded_at") or item.get("handled_at") or ""))[-1]


def latest_response_for_task(
    responses: Sequence[Mapping[str, Any]],
    task_id: str,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for response in responses:
        if str(response.get("task_id") or "") == task_id:
            matches.append(dict(response))
            continue
        scope = str(response.get("scope") or "")
        if task_id in {part.strip(" ,.;:") for part in scope.split()}:
            matches.append(dict(response))
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("responded_at") or item.get("handled_at") or ""))[-1]


def task_approval_decision(responses: Sequence[Mapping[str, Any]], task_id: str) -> str | None:
    response = latest_response_for_task(responses, task_id)
    if response is None:
        return None
    decision = str(response.get("decision") or response.get("status") or "").lower()
    return decision if decision in ALLOWED_APPROVAL_DECISIONS else None


def new_approval_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"approval_{stamp}_{uuid.uuid4().hex[:8]}"


def default_expires_at() -> str:
    expires = datetime.now(UTC) + timedelta(hours=DEFAULT_APPROVAL_EXPIRY_HOURS)
    return expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_project_paths(project_root: Path | str) -> tuple[Path, WorkflowPaths, str]:
    project = Path(project_root).expanduser().resolve()
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    workflow_id = str(workflow.get("workflow_id") or "unknown_workflow")
    return project, paths, workflow_id


def _request_by_id(requests: Sequence[Mapping[str, Any]], approval_id: str) -> dict[str, Any] | None:
    for request in requests:
        if str(request.get("approval_id") or request.get("request_id") or "") == approval_id:
            return dict(request)
    return None


def _response_failure(
    *,
    project: Path,
    workflow_id: str,
    status: str,
    message: str,
    approval_policy: Mapping[str, Any] | None = None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "errors": [message],
        "warnings": [],
    }
    if approval_policy is not None:
        result["approval_policy"] = dict(approval_policy)
    if approval is not None:
        result["approval"] = dict(approval)
    return result


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            records.append(dict(record))
    return records


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def _is_expired(value: object, *, now: str | None) -> bool:
    expires_at = _parse_timestamp(value)
    if expires_at is None:
        return False
    now_dt = _parse_timestamp(now) if now is not None else datetime.now(UTC)
    return now_dt is not None and expires_at <= now_dt


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
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _path_for_record(project_root: Path | None, path: Path) -> str:
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


__all__ = [
    "ALLOWED_APPROVAL_DECISIONS",
    "APPROVAL_REQUESTS_FILENAME",
    "APPROVAL_RESPONSES_FILENAME",
    "approval_record_status",
    "default_expires_at",
    "load_approval_policy",
    "load_approval_status",
    "new_approval_id",
    "read_approval_requests",
    "read_approval_responses",
    "record_approval_response",
    "task_approval_decision",
]
