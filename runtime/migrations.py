from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_MIGRATION_REQUIRED, EXIT_SUCCESS
from runtime.path_resolution import WorkflowPaths
from runtime.schema_validation import (
    SCHEMA_VERSION,
    RUNTIME_VERSION,
    check_project_schema_version,
    schema_targets,
    validate_project_schemas,
)


MIGRATION_RECORDS_FILENAME = "migration_records.jsonl"
SUPPORTED_MIGRATIONS = {
    ("1.4", SCHEMA_VERSION): "v1_4_to_v1_5.py",
}


def migrate_project(project_root: Path | str, *, dry_run: bool = False) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    schema_path = project / ".loopplane" / "config" / "schema_version.json"
    schema_data, schema_error = _read_json_object(schema_path)
    if schema_error:
        return _blocked(
            project=project,
            started_at=started_at,
            status="blocked",
            reason=f"Unable to read schema_version.json: {schema_error}",
            errors=[f".loopplane/config/schema_version.json: {schema_error}"],
        )

    from_version = str(schema_data.get("schema_version") or "")
    workflow_id = _workflow_id(project)
    compatibility = check_project_schema_version(project)
    if from_version == SCHEMA_VERSION and compatibility.get("status") == "current":
        validation = validate_project_schemas(project)
        if validation.get("ok"):
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "status": "no_op",
                "project_root": project.as_posix(),
                "workflow_id": workflow_id,
                "from_version": from_version,
                "to_version": SCHEMA_VERSION,
                "started_at": started_at,
                "ended_at": utc_timestamp(),
                "message": "Project schema is already current; no migration was applied.",
                "modified_files": [],
                "backup_dir": None,
                "migration_record_path": None,
                "validation": validation,
                "dry_run": dry_run,
                "errors": [],
                "warnings": [],
            }
        return _blocked(
            project=project,
            started_at=started_at,
            status="blocked",
            reason="Project is already on the current schema version, but schema validation failed.",
            errors=list(validation.get("errors", [])),
            workflow_id=workflow_id,
            validation=validation,
        )

    migration_key = (from_version, SCHEMA_VERSION)
    migration_script = SUPPORTED_MIGRATIONS.get(migration_key)
    if migration_script is None:
        return _blocked(
            project=project,
            started_at=started_at,
            status="migration_required",
            reason=f"No explicit migration path from schema {from_version!r} to {SCHEMA_VERSION!r}.",
            errors=list(compatibility.get("errors", [])) or [f"unsupported schema_version {from_version!r}"],
            workflow_id=workflow_id,
            compatibility=compatibility,
        )

    workflow_config = _read_workflow(project)
    paths = _paths_for_migration(project, workflow_config)
    migrations_dir = paths.runtime_dir / "migrations"
    migration_id = _migration_id(from_version, SCHEMA_VERSION)
    backup_dir = migrations_dir / "backups" / migration_id
    record_path = migrations_dir / f"{migration_id}.json"
    records_path = migrations_dir / MIGRATION_RECORDS_FILENAME
    script_path = migrations_dir / migration_script

    targets = _migration_targets(project, workflow_config)
    payloads, collect_errors = _collect_payloads(project, targets)
    if collect_errors:
        return _blocked(
            project=project,
            started_at=started_at,
            status="blocked",
            reason="Migration refused because one or more target JSON files could not be parsed.",
            errors=collect_errors,
            workflow_id=workflow_id,
        )

    planned_files = [
        relative_path
        for relative_path, payload in payloads.items()
        if _payload_needs_version_update(relative_path, payload, from_version)
    ]
    if dry_run:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "dry_run",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "from_version": from_version,
            "to_version": SCHEMA_VERSION,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "message": f"Migration {migration_script} can update {len(planned_files)} file(s).",
            "migration_script": _path_for_record(project, script_path),
            "planned_files": sorted(planned_files),
            "backup_dir": _path_for_record(project, backup_dir),
            "dry_run": True,
            "errors": [],
            "warnings": [],
        }

    migrations_dir.mkdir(parents=True, exist_ok=True)
    _ensure_project_migration_script(script_path, from_version=from_version, to_version=SCHEMA_VERSION)
    modified_files: list[str] = []
    backup_files: list[str] = []
    for relative_path in sorted(planned_files):
        source = project / relative_path
        backup = backup_dir / relative_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        backup_files.append(_path_for_record(project, backup))
        payload = _migrated_payload(
            relative_path,
            payloads[relative_path],
            workflow_id=workflow_id,
            migrated_at=started_at,
            read_models_dir=paths.value("read_models_dir"),
        )
        _atomic_write_json(source, payload)
        modified_files.append(relative_path)

    validation = validate_project_schemas(project)
    ended_at = utc_timestamp()
    status = "migrated" if validation.get("ok") else "blocked"
    record = {
        "schema_version": SCHEMA_VERSION,
        "migration_id": migration_id,
        "status": status,
        "from_version": from_version,
        "to_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "migration_script": _path_for_record(project, script_path),
        "backup_dir": _path_for_record(project, backup_dir),
        "backup_files": backup_files,
        "modified_files": modified_files,
        "validation_status": validation.get("status"),
        "validation_errors": list(validation.get("errors", [])),
    }
    _atomic_write_json(record_path, record)
    _append_jsonl(records_path, record)

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "migrated",
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "from_version": from_version,
        "to_version": SCHEMA_VERSION,
        "started_at": started_at,
        "ended_at": ended_at,
        "message": (
            f"Applied explicit migration {migration_script}."
            if status == "migrated"
            else "Migration wrote backups but post-migration schema validation failed."
        ),
        "migration_id": migration_id,
        "migration_script": _path_for_record(project, script_path),
        "backup_dir": _path_for_record(project, backup_dir),
        "backup_files": backup_files,
        "modified_files": modified_files,
        "migration_record_path": _path_for_record(project, record_path),
        "migration_records_path": _path_for_record(project, records_path),
        "validation": validation,
        "dry_run": False,
        "errors": list(validation.get("errors", [])),
        "warnings": [],
    }


def migration_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True or str(result.get("status") or "") in {"no_op", "dry_run"}:
        return EXIT_SUCCESS
    if str(result.get("status") or "") == "migration_required":
        return EXIT_MIGRATION_REQUIRED
    return EXIT_INVALID_CONFIG


def format_migration_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane migrate: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"from_version: {result.get('from_version') or 'unknown'}",
        f"to_version: {result.get('to_version') or SCHEMA_VERSION}",
        str(result.get("message") or ""),
    ]
    for key in ("migration_script", "backup_dir", "migration_record_path"):
        value = result.get(key)
        if value:
            lines.append(f"{key}: {value}")
    modified = result.get("modified_files")
    if isinstance(modified, Sequence) and not isinstance(modified, (str, bytes)) and modified:
        lines.append("modified_files:")
        lines.extend(f"  - {path}" for path in modified)
    planned = result.get("planned_files")
    if isinstance(planned, Sequence) and not isinstance(planned, (str, bytes)) and planned:
        lines.append("planned_files:")
        lines.extend(f"  - {path}" for path in planned)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(line for line in lines if line) + "\n"


def _blocked(
    *,
    project: Path,
    started_at: str,
    status: str,
    reason: str,
    errors: Sequence[str],
    workflow_id: str | None = None,
    validation: Mapping[str, Any] | None = None,
    compatibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "from_version": None,
        "to_version": SCHEMA_VERSION,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "message": reason,
        "modified_files": [],
        "backup_dir": None,
        "migration_record_path": None,
        "validation": dict(validation) if isinstance(validation, Mapping) else None,
        "compatibility": dict(compatibility) if isinstance(compatibility, Mapping) else None,
        "dry_run": False,
        "errors": list(errors),
        "warnings": [],
    }


def _migration_targets(project: Path, workflow_config: Mapping[str, Any]) -> tuple[str, ...]:
    targets = [target.relative_path for target in schema_targets(project, workflow_config)]
    return tuple(sorted(set(targets)))


def _collect_payloads(project: Path, targets: Sequence[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    payloads: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for relative_path in targets:
        path = project / relative_path
        if not path.exists():
            continue
        payload, error = _read_json_object(path)
        if error:
            errors.append(f"{relative_path}: {error}")
            continue
        payloads[relative_path] = payload
    return payloads, errors


def _payload_needs_version_update(relative_path: str, payload: Mapping[str, Any], from_version: str) -> bool:
    if relative_path == ".loopplane/config/schema_version.json":
        return True
    version = payload.get("schema_version")
    return version in {None, from_version}


def _migrated_payload(
    relative_path: str,
    payload: Mapping[str, Any],
    *,
    workflow_id: str | None,
    migrated_at: str,
    read_models_dir: str,
) -> dict[str, Any]:
    migrated = dict(payload)
    migrated["schema_version"] = SCHEMA_VERSION
    read_models_prefix = read_models_dir.rstrip("/") + "/"
    if workflow_id and relative_path.startswith(read_models_prefix):
        migrated.setdefault("workflow_id", workflow_id)
        migrated.setdefault("generated_at", migrated_at)
        migrated.setdefault("source_hashes", {})
        migrated.setdefault("last_event_seq", 0)
        migrated.setdefault("source_event_id", None)
    if relative_path == ".loopplane/config/schema_version.json":
        files = migrated.get("files")
        if not isinstance(files, dict):
            files = {}
        for key in list(files):
            files[key] = SCHEMA_VERSION
        migrated["files"] = files
        migrated["last_migrated_at"] = migrated_at
        migrated["required_runtime_version"] = f">={RUNTIME_VERSION}"
        migrated["created_with"] = f"loopplane {RUNTIME_VERSION}"
    return migrated


def _ensure_project_migration_script(path: Path, *, from_version: str, to_version: str) -> None:
    if path.exists():
        return
    path.write_text(
        "\n".join(
            [
                '"""Project-local LoopPlane schema migration descriptor."""',
                "",
                f'FROM_VERSION = "{from_version}"',
                f'TO_VERSION = "{to_version}"',
                'RUNTIME_IMPLEMENTATION = "runtime.migrations.migrate_project"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _read_workflow(project: Path) -> dict[str, Any]:
    workflow, error = _read_json_object(project / ".loopplane" / "config" / "workflow.json")
    return workflow if error is None else {}


def _workflow_id(project: Path) -> str | None:
    workflow = _read_workflow(project)
    value = workflow.get("workflow_id")
    return str(value) if isinstance(value, str) and value else None


def _paths_for_migration(project: Path, workflow_config: Mapping[str, Any]) -> WorkflowPaths:
    try:
        return WorkflowPaths.from_config(project, workflow_config)
    except Exception:
        return WorkflowPaths.from_config(project, {})


def _read_json_object(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, "missing"
    except json.JSONDecodeError as error:
        return {}, f"invalid JSON: {error.msg}"
    except OSError as error:
        return {}, f"read error: {type(error).__name__}: {error}"
    if not isinstance(data, dict):
        return {}, "must be a JSON object"
    return data, None


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _migration_id(from_version: str, to_version: str) -> str:
    return f"migration_{utc_timestamp().replace('-', '').replace(':', '').replace('T', '_').rstrip('Z')}_{from_version.replace('.', '_')}_to_{to_version.replace('.', '_')}"


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()
