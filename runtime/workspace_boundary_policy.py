from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime.path_resolution import WorkflowPaths
from runtime.workspace_identity import normalize_identity_path_value, workspace_boundary_root


FIELD_LINE_RE = re.compile(r"^  - (?P<field>[A-Za-z0-9_ -]+):(?P<value>.*)$")
TASK_LINE_RE = re.compile(r"^- \[(?P<status>[ x~!\-])\]\s+(?P<task_id>[A-Za-z0-9_.-]+):\s+(?P<title>.+?)\s*$")

PATH_FIELDS = ("path", "new_path", "old_path", "file", "filename")
RUN_LOCAL_PREFIXES = frozenset({"artifacts", "logs", "raw", "git"})
RUN_LOCAL_FILES = frozenset(
    {
        "agent_status.json",
        "commands.sh",
        "report.md",
        "metadata.json",
        "run_execution.json",
        "node_summary.json",
        "validation.json",
    }
)
PLAN_ALLOW_FIELDS = frozenset({"allow_out_of_boundary_writes", "out_of_boundary_writes"})
PLAN_PATH_FIELDS = frozenset(
    {
        "out_of_boundary_write_paths",
        "out_of_boundary_paths",
        "allow_out_of_boundary_paths",
        "allowed_out_of_boundary_write_paths",
    }
)
ALLOW_VALUES = frozenset({"true", "yes", "allow", "allowed", "explicitly_allowed", "enabled"})


def evaluate_worker_write_boundary(
    project_root: Path | str,
    paths: WorkflowPaths,
    *,
    task_id: str | None,
    run_dir: Path | str,
    agent_status: Mapping[str, Any] | None = None,
    adapter_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate worker-produced path metadata against the workspace boundary."""

    project = Path(project_root).expanduser().resolve()
    resolved_run_dir = _resolve_path(project, run_dir, base=project)
    workspace = _read_json_object(project / ".loopplane" / "workspace.json")
    security = _read_json_object(paths.config_file("security.json"))
    boundary_root = _boundary_root(project, workspace)
    plan_policy = _plan_out_of_boundary_policy(paths.plan_file, task_id)
    security_policy = _security_out_of_boundary_policy(security)
    workspace_allows = bool(workspace.get("allow_out_of_boundary_writes")) if isinstance(workspace, Mapping) else False

    records = _collect_worker_path_records(
        project=project,
        run_dir=resolved_run_dir,
        agent_status=agent_status,
        adapter_result=adapter_result,
    )
    violations: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    for record in records:
        resolved = record["resolved_path"]
        display_path = _display_path(project, resolved)
        inside_boundary = _is_relative_to(resolved, boundary_root)
        checked.append(
            {
                "path": display_path,
                "reported_path": record["reported_path"],
                "source": record["source"],
                "inside_boundary": inside_boundary,
            }
        )
        if inside_boundary:
            continue
        allowed_decision = _out_of_boundary_path_allowed(
            project=project,
            resolved_path=resolved,
            workspace_allows=workspace_allows,
            security_policy=security_policy,
            plan_policy=plan_policy,
        )
        violation_record = {
            "path": display_path,
            "reported_path": record["reported_path"],
            "source": record["source"],
            "reason": allowed_decision["reason"],
        }
        if allowed_decision["allowed"]:
            allowed.append(violation_record)
        else:
            violations.append(violation_record)

    return {
        "schema_version": "1.5",
        "status": "pass" if not violations else "violation",
        "ok": not violations,
        "task_id": task_id,
        "run_dir": _display_path(project, resolved_run_dir),
        "workspace_boundary": str(workspace.get("workspace_boundary") or "project_root") if isinstance(workspace, Mapping) else "project_root",
        "resolved_workspace_boundary": _display_path(project, boundary_root),
        "allow_out_of_boundary_writes": workspace_allows,
        "security_allows_out_of_boundary_writes": bool(security_policy["allow"]),
        "active_plan_allows_out_of_boundary_writes": bool(plan_policy["allow"]),
        "active_plan": bool(plan_policy["active_plan"]),
        "checked_paths": checked,
        "allowed_out_of_boundary_paths": allowed,
        "violations": violations,
    }


def worker_write_boundary_message(policy: Mapping[str, Any]) -> str:
    violations = policy.get("violations")
    if not isinstance(violations, Sequence) or isinstance(violations, (str, bytes)) or not violations:
        return "Worker write boundary policy passed."
    paths = [
        str(item.get("path") or item.get("reported_path") or "unknown")
        for item in violations
        if isinstance(item, Mapping)
    ]
    return "Worker output references out-of-boundary write path(s): " + ", ".join(paths)


def _collect_worker_path_records(
    *,
    project: Path,
    run_dir: Path,
    agent_status: Mapping[str, Any] | None,
    adapter_result: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(agent_status, Mapping):
        _extend_project_path_records(records, project, run_dir, agent_status.get("project_changes"), "agent_status.project_changes")
        _extend_project_path_records(records, project, run_dir, agent_status.get("changed_files"), "agent_status.changed_files")
        _extend_evidence_path_records(records, project, run_dir, agent_status.get("key_outputs"), "agent_status.key_outputs")
        for index, claim in enumerate(_mapping_items(agent_status.get("evidence_satisfies"))):
            _extend_evidence_path_records(
                records,
                project,
                run_dir,
                claim.get("evidence"),
                f"agent_status.evidence_satisfies[{index}].evidence",
            )

    changed_file = run_dir / "git" / "changed_files.json"
    changed = _read_json_object(changed_file)
    if isinstance(changed, Mapping):
        _extend_project_path_records(records, project, run_dir, changed.get("changed_files"), "git.changed_files")
    elif isinstance(changed, Sequence) and not isinstance(changed, (str, bytes)):
        _extend_project_path_records(records, project, run_dir, changed, "git.changed_files")

    if adapter_result is None:
        adapter_result = _read_json_object(run_dir / "adapter_result.json")
    if isinstance(adapter_result, Mapping):
        _extend_output_path_records(records, project, run_dir, adapter_result.get("produced_files"), "adapter_result.produced_files")
        metadata = adapter_result.get("adapter_metadata")
        if isinstance(metadata, Mapping):
            adapter_boundary = metadata.get("workspace_boundary_policy")
            if isinstance(adapter_boundary, Mapping):
                _extend_project_path_records(
                    records,
                    project,
                    run_dir,
                    adapter_boundary.get("observed_changes"),
                    "adapter_result.adapter_metadata.workspace_boundary_policy.observed_changes",
                )

    return _dedupe_records(records)


def _extend_project_path_records(
    records: list[dict[str, Any]],
    project: Path,
    run_dir: Path,
    value: Any,
    source: str,
) -> None:
    for item in _path_items(value):
        for path_value in _item_paths(item):
            records.append(
                {
                    "reported_path": path_value,
                    "source": source,
                    "resolved_path": _resolve_path(project, path_value, base=project),
                }
            )


def _extend_output_path_records(
    records: list[dict[str, Any]],
    project: Path,
    run_dir: Path,
    value: Any,
    source: str,
) -> None:
    for item in _path_items(value):
        for path_value in _item_paths(item):
            records.append(
                {
                    "reported_path": path_value,
                    "source": source,
                    "resolved_path": _resolve_run_path(project, run_dir, path_value),
                }
            )


def _extend_evidence_path_records(
    records: list[dict[str, Any]],
    project: Path,
    run_dir: Path,
    value: Any,
    source: str,
) -> None:
    """Collect evidence references, including paths relative to this run.

    Worker evidence may intentionally refer to an earlier sibling run using
    ``../run_previous/...``.  Adapter ``produced_files`` metadata has different
    semantics—its relative paths are project-relative—so this resolver is kept
    separate from the write-observation path resolver.
    """

    for item in _path_items(value):
        for path_value in _item_paths(item):
            normalized = path_value.strip().replace("\\", "/")
            if normalized == ".." or normalized.startswith("../") or normalized.startswith("./../"):
                resolved = (run_dir / normalized).resolve()
            else:
                resolved = _resolve_run_path(project, run_dir, path_value)
            records.append(
                {
                    "reported_path": path_value,
                    "source": source,
                    "resolved_path": resolved,
                }
            )


def _path_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value.decode("utf-8", "ignore") if isinstance(value, bytes) else value]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return []


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _item_paths(item: Any) -> list[str]:
    if isinstance(item, str) and item.strip():
        return [item.strip()]
    if not isinstance(item, Mapping):
        return []
    paths: list[str] = []
    for field in PATH_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    return _dedupe(paths)


def _resolve_run_path(project: Path, run_dir: Path, value: str) -> Path:
    normalized = value.strip().replace("\\", "/")
    path = Path(normalized).expanduser()
    if path.is_absolute():
        return path.resolve()
    if normalized.startswith(".loopplane/") or normalized.startswith("./.loopplane/"):
        return (project / normalized.removeprefix("./")).resolve()
    first = normalized.split("/", 1)[0]
    if first in RUN_LOCAL_PREFIXES or normalized in RUN_LOCAL_FILES:
        return (run_dir / normalized).resolve()
    return (project / normalized).resolve()


def _resolve_path(project: Path, value: Path | str, *, base: Path) -> Path:
    path = value if isinstance(value, Path) else Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _boundary_root(project: Path, workspace: Mapping[str, Any] | None) -> Path:
    if isinstance(workspace, Mapping):
        try:
            return workspace_boundary_root(project, workspace)
        except (OSError, ValueError):
            pass
    return project


def _security_out_of_boundary_policy(security: Mapping[str, Any] | None) -> dict[str, Any]:
    file_access = security.get("file_access") if isinstance(security, Mapping) else None
    if not isinstance(file_access, Mapping):
        return {"allow": False, "allowlist": []}
    allowlist = _normalize_allowlist(file_access.get("out_of_boundary_write_allowlist"))
    return {
        "allow": file_access.get("allow_out_of_boundary_writes") is True,
        "allowlist": allowlist,
    }


def _plan_out_of_boundary_policy(plan_file: Path, task_id: str | None) -> dict[str, Any]:
    try:
        plan_text = plan_file.read_text(encoding="utf-8")
    except OSError:
        return {"active_plan": False, "allow": False, "allowlist": []}
    active = "- active: true" in plan_text
    fields = _task_fields(plan_text, task_id)
    allow = active and any(_truthy_allow(value) for field in PLAN_ALLOW_FIELDS for value in fields.get(field, ()))
    path_values: list[str] = []
    for field in PLAN_PATH_FIELDS:
        for value in fields.get(field, ()):
            path_values.extend(_split_path_values(value))
    return {
        "active_plan": active,
        "allow": allow,
        "allowlist": _normalize_allowlist(path_values),
    }


def _task_fields(plan_text: str, task_id: str | None) -> dict[str, tuple[str, ...]]:
    if not task_id:
        return {}
    lines = plan_text.splitlines()
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = TASK_LINE_RE.match(line)
        if not match:
            continue
        if match.group("task_id") == task_id:
            start = index
            continue
        if start is not None:
            end = index
            break
    if start is None:
        return {}
    fields: dict[str, list[str]] = {}
    current_field: str | None = None
    for line in lines[start + 1 : end]:
        match = FIELD_LINE_RE.match(line)
        if match:
            field = match.group("field").strip().lower().replace("-", "_").replace(" ", "_")
            value = match.group("value").strip()
            fields.setdefault(field, []).append(value)
            current_field = field
            continue
        if current_field and line.startswith("    "):
            fields[current_field][-1] = f"{fields[current_field][-1]} {line.strip()}".strip()
    return {key: tuple(value for value in values if value) for key, values in fields.items()}


def _out_of_boundary_path_allowed(
    *,
    project: Path,
    resolved_path: Path,
    workspace_allows: bool,
    security_policy: Mapping[str, Any],
    plan_policy: Mapping[str, Any],
) -> dict[str, Any]:
    if not workspace_allows:
        return {"allowed": False, "reason": "workspace allow_out_of_boundary_writes is false"}
    if security_policy.get("allow") is not True:
        return {"allowed": False, "reason": "security file_access.allow_out_of_boundary_writes is not true"}
    if plan_policy.get("allow") is not True:
        return {"allowed": False, "reason": "active PLAN.md does not allow out-of-boundary writes for this task"}
    relative = _relative_to_project(project, resolved_path)
    if not _matches_allowlist(relative, security_policy.get("allowlist")):
        return {"allowed": False, "reason": "security out_of_boundary_write_allowlist does not include this path"}
    if not _matches_allowlist(relative, plan_policy.get("allowlist")):
        return {"allowed": False, "reason": "active PLAN.md out_of_boundary_write_paths does not include this path"}
    return {"allowed": True, "reason": "explicitly allowed by workspace, security config, and active PLAN.md"}


def _normalize_allowlist(value: Any) -> list[dict[str, Any]]:
    entries: list[str] = []
    if isinstance(value, str):
        entries.extend(_split_path_values(value))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, str):
                entries.extend(_split_path_values(item))
    normalized: list[dict[str, Any]] = []
    for item in entries:
        text = item.strip().replace("\\", "/")
        if not text:
            continue
        is_prefix = text.endswith("/")
        try:
            normalized_path = normalize_identity_path_value("out_of_boundary_write_allowlist", text.rstrip("/"), allow_parent=True)
        except ValueError:
            continue
        normalized.append({"path": normalized_path, "prefix": is_prefix})
    seen: set[tuple[str, bool]] = set()
    deduped: list[dict[str, Any]] = []
    for item in normalized:
        key = (str(item["path"]), bool(item["prefix"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _split_path_values(value: str) -> list[str]:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return [part.strip().strip("\"'") for part in re.split(r"[,;]", stripped) if part.strip()]


def _truthy_allow(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in ALLOW_VALUES


def _matches_allowlist(relative_path: str, allowlist: Any) -> bool:
    if not isinstance(allowlist, Sequence) or isinstance(allowlist, (str, bytes)):
        return False
    for item in allowlist:
        if not isinstance(item, Mapping):
            continue
        allowed = str(item.get("path") or "")
        if not allowed:
            continue
        if item.get("prefix") is True:
            prefix = allowed.rstrip("/")
            if relative_path == prefix or relative_path.startswith(prefix + "/"):
                return True
        elif relative_path == allowed:
            return True
    return False


def _relative_to_project(project: Path, path: Path) -> str:
    return os.path.relpath(path.resolve(), start=project.resolve()).replace(os.sep, "/")


def _display_path(project: Path, path: Path) -> str:
    return _relative_to_project(project, path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        resolved = record.get("resolved_path")
        if not isinstance(resolved, Path):
            continue
        key = (str(record.get("source") or ""), str(record.get("reported_path") or ""), resolved.as_posix())
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(record))
    return result
