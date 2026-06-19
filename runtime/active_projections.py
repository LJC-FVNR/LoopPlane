from __future__ import annotations

import json
import os
import uuid
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import WorkflowPaths


SCHEMA_VERSION = "1.6"
ACTIVE_PROJECTIONS_CONFIG_KEY = "active_workflow_projections"
ACTIVE_PROJECTIONS_METADATA = PurePosixPath(".loopplane/projections/active_workflow.json")
SUPPORTED_FIELDS = ("brief_file", "plan_file", "shared_context_file")
DEFAULT_TARGETS = {
    "brief_file": "PROJECT_BRIEF.md",
    "plan_file": "PLAN.md",
    "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
}


def canonical_active_projection_config(*, enabled: bool, include_plan_file: bool = False) -> dict[str, Any]:
    files = {
        "brief_file": DEFAULT_TARGETS["brief_file"],
        "shared_context_file": DEFAULT_TARGETS["shared_context_file"],
    }
    if include_plan_file:
        files["plan_file"] = DEFAULT_TARGETS["plan_file"]
    return {
        "enabled": bool(enabled),
        "metadata_file": ACTIVE_PROJECTIONS_METADATA.as_posix(),
        "overwrite_policy": "managed_or_identical",
        "files": files,
    }


def sync_active_workflow_projections(
    project_root: Path | str,
    workflow_config: Mapping[str, Any],
    paths: WorkflowPaths,
    *,
    reason: str,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    config = workflow_config.get(ACTIVE_PROJECTIONS_CONFIG_KEY)
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return _result(
            project=project,
            workflow_id=paths.workflow_id,
            started_at=started_at,
            enabled=False,
            status="disabled",
            reason=reason,
            projections=[],
            warnings=[],
            errors=[],
            metadata_file=None,
        )

    try:
        metadata_path_value = _metadata_path_value(config.get("metadata_file"))
    except ValueError as error:
        return _result(
            project=project,
            workflow_id=paths.workflow_id,
            started_at=started_at,
            enabled=True,
            status="failed",
            reason=reason,
            projections=[],
            warnings=[],
            errors=[str(error)],
            metadata_file=None,
        )
    metadata_path = project / metadata_path_value
    metadata = _load_metadata(metadata_path)
    raw_files_config = config.get("files")
    if isinstance(raw_files_config, Mapping):
        files_config = raw_files_config
        explicit_files_config = True
    else:
        files_config = DEFAULT_TARGETS
        explicit_files_config = False

    projections: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    next_files = dict(metadata.get("files") or {}) if isinstance(metadata.get("files"), Mapping) else {}
    for field in SUPPORTED_FIELDS:
        if explicit_files_config and field not in files_config:
            next_files.pop(DEFAULT_TARGETS[field], None)
            projections.append(
                {
                    "field": field,
                    "source": paths.value(field),
                    "target": None,
                    "status": "disabled",
                    "changed": False,
                }
            )
            continue
        target_value = files_config.get(field, DEFAULT_TARGETS[field])
        source_path = paths.path(field)
        source_value = paths.value(field)
        try:
            target_relative = _project_relative_path(str(field), target_value)
        except ValueError as error:
            warnings.append(str(error))
            projections.append(
                {
                    "field": field,
                    "source": source_value,
                    "target": str(target_value),
                    "status": "invalid_target",
                    "changed": False,
                }
            )
            continue
        target_path = project / target_relative
        projection = _sync_one_projection(
            project=project,
            field=field,
            source_path=source_path,
            source_value=source_value,
            target_path=target_path,
            target_value=target_relative,
            metadata_entry=next_files.get(target_relative),
        )
        projections.append(projection)
        if projection.get("status") in {"created", "updated", "unchanged", "same_path"}:
            source_sha = projection.get("source_sha256")
            if isinstance(source_sha, str) and source_sha:
                next_files[target_relative] = {
                    "field": field,
                    "source": source_value,
                    "target": target_relative,
                    "last_projected_sha256": source_sha,
                    "updated_at": utc_timestamp(),
                }
        if projection.get("warning"):
            warnings.append(str(projection["warning"]))
        if projection.get("error"):
            errors.append(str(projection["error"]))

    changed = any(bool(item.get("changed")) for item in projections)
    if projections and not errors:
        try:
            _write_metadata(
                metadata_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "workflow_id": paths.workflow_id,
                    "workflow_root": paths.workflow_root_value,
                    "metadata_file": metadata_path_value,
                    "reason": reason,
                    "updated_at": utc_timestamp(),
                    "files": next_files,
                },
            )
        except OSError as error:
            errors.append(f"{metadata_path_value}: projection metadata could not be written: {error}")
    status = "updated" if changed else "current"
    if warnings and not changed:
        status = "skipped"
    if errors:
        status = "failed"
    return _result(
        project=project,
        workflow_id=paths.workflow_id,
        started_at=started_at,
        enabled=True,
        status=status,
        reason=reason,
        projections=projections,
        warnings=_dedupe(warnings),
        errors=_dedupe(errors),
        metadata_file=metadata_path_value,
    )


def _sync_one_projection(
    *,
    project: Path,
    field: str,
    source_path: Path,
    source_value: str,
    target_path: Path,
    target_value: str,
    metadata_entry: Any,
) -> dict[str, Any]:
    if source_path == target_path:
        source_bytes = _read_bytes(source_path)
        source_sha = _sha256_bytes(source_bytes) if source_bytes is not None else None
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "status": "same_path",
            "changed": False,
        }
    source_bytes = _read_bytes(source_path)
    if source_bytes is None:
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "status": "source_missing",
            "changed": False,
            "warning": f"{source_value}: source file is missing; projection {target_value} was not updated",
        }

    source_sha = _sha256_bytes(source_bytes)
    if target_path.exists() and target_path.is_symlink():
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "status": "unsafe_existing_content",
            "changed": False,
            "warning": f"{target_value}: projection target is a symlink and was not overwritten",
        }
    if target_path.exists() and not target_path.is_file():
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "status": "unsafe_existing_content",
            "changed": False,
            "warning": f"{target_value}: projection target exists and is not a regular file",
        }
    target_bytes = _read_bytes(target_path)
    if target_bytes == source_bytes:
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "target_sha256": source_sha,
            "status": "unchanged",
            "changed": False,
        }
    if target_bytes is not None and not _safe_to_replace(target_bytes, metadata_entry):
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "target_sha256": _sha256_bytes(target_bytes),
            "status": "unsafe_existing_content",
            "changed": False,
            "warning": f"{target_value}: existing content is not a managed projection; leaving it unchanged",
        }
    if target_path.parent.exists() and not target_path.parent.is_dir():
        return {
            "field": field,
            "source": source_value,
            "target": target_value,
            "source_sha256": source_sha,
            "status": "unsafe_existing_content",
            "changed": False,
            "warning": f"{target_value}: parent path is not a directory",
        }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(target_path, source_bytes)
    return {
        "field": field,
        "source": source_value,
        "target": target_value,
        "source_sha256": source_sha,
        "target_sha256": source_sha,
        "status": "created" if target_bytes is None else "updated",
        "changed": True,
    }


def _safe_to_replace(target_bytes: bytes, metadata_entry: Any) -> bool:
    if not isinstance(metadata_entry, Mapping):
        return False
    expected_sha = metadata_entry.get("last_projected_sha256")
    return isinstance(expected_sha, str) and _sha256_bytes(target_bytes) == expected_sha


def _metadata_path_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ACTIVE_PROJECTIONS_METADATA.as_posix()
    return _project_relative_path("metadata_file", value)


def _project_relative_path(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: projection path must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"{field}: projection path must use POSIX-style '/' separators: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError(f"{field}: projection path must stay inside the project root: {value}")
    return path.as_posix()


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _sha256_bytes(content: bytes | None) -> str | None:
    if content is None:
        return None
    return "sha256:" + sha256(content).hexdigest()


def _result(
    *,
    project: Path,
    workflow_id: str | None,
    started_at: str,
    enabled: bool,
    status: str,
    reason: str,
    projections: list[dict[str, Any]],
    warnings: list[str],
    errors: list[str],
    metadata_file: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "enabled": enabled,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "reason": reason,
        "metadata_file": metadata_file,
        "changed": any(bool(item.get("changed")) for item in projections),
        "projections": projections,
        "warnings": warnings,
        "errors": errors,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
