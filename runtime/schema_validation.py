from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_MIGRATION_REQUIRED, EXIT_SUCCESS
from runtime.path_resolution import (
    DEFAULT_WORKFLOW_PATHS,
    WORKFLOW_PATH_FIELDS,
    WorkflowPathError,
    WorkflowPaths,
    load_workflow_config,
    workflow_path_values,
)
from runtime.workflow_lifecycle import WorkflowLifecycleError, ensure_compatibility_workflow_metadata


SCHEMA_VERSION = "1.5"
WORKSPACE_SCHEMA_VERSION = "1.6"
RUNTIME_VERSION = "1.5.0"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
WORKFLOW_ID_RE = re.compile(r"^wf_\d{8}_[0-9a-f]{8}$")
WORKSPACE_ID_RE = re.compile(r"^ws_[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
MIGRATABLE_SCHEMA_VERSIONS = frozenset({"1.4"})
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
V16_PROJECT_JSON_TARGETS = frozenset(
    {
        ".loopplane/workspace.json",
        ".loopplane/workflow_registry.json",
        ".loopplane/current_workflow.json",
    }
)
V16_SCHEMA_VERSION_PROJECT_TARGETS = V16_PROJECT_JSON_TARGETS | frozenset(
    {
        ".loopplane/config/workflow_defaults.json",
        ".loopplane/config/local/agent_runners.local.json",
    }
)
SCHEMA_VERSION_VALIDATED_BY_SCHEMA_ONLY = frozenset(
    {
        ".loopplane/config/instance.json",
        ".loopplane/config/package_manifest.json",
    }
)


@dataclass(frozen=True)
class SchemaTarget:
    relative_path: str
    schema_file: str
    required: bool = True
    kind: str = "json"

    @property
    def path_key(self) -> str:
        path = PurePosixPath(self.relative_path)
        if path.parent.name == "config" and path.name in CONFIG_SCHEMA_FILES:
            return path.name
        return path.as_posix()


CONFIG_SCHEMA_FILES = {
    "workflow.json": "workflow.schema.json",
    "agent_runners.json": "agent_runners.schema.json",
    "dashboard.json": "dashboard.schema.json",
    "security.json": "security.schema.json",
    "version_control.json": "version_control.schema.json",
    "schema_version.json": "schema_version.schema.json",
}

WORKSPACE_CONFIG_SCHEMA_FILES = {
    "instance.json": "workflow_instance.schema.json",
    "workflow_defaults.json": "workflow_defaults.schema.json",
    "package_manifest.json": "project_package_manifest.schema.json",
}

RUNTIME_JSON_TARGETS = (
    ("state.json", "runtime_state.schema.json"),
    ("background_jobs.json", "background_jobs.schema.json"),
    ("failure_registry.json", "failure_registry.schema.json"),
    ("expansion_registry.json", "expansion_registry.schema.json"),
    ("evidence_manifest.json", "evidence_manifest.schema.json"),
    ("snapshots/snapshot_000001.json", "event_snapshot.schema.json"),
)

READ_MODEL_JSON_TARGETS = (
    ("workflow_status.json", "read_model_workflow_status.schema.json"),
    ("plan_index.json", "read_model_plan_index.schema.json"),
    ("workflow_graph.json", "read_model_workflow_graph.schema.json"),
    ("metrics.json", "read_model_metrics.schema.json"),
    ("version_control_status.json", "read_model_version_control_status.schema.json"),
    ("run_details_manifest.json", "read_model_run_details_manifest.schema.json"),
    ("build_manifest.json", "read_model_build_manifest.schema.json"),
)

READ_MODEL_JSONL_TARGETS = (
    ("run_index.jsonl", "read_model_run_index.schema.json"),
)

OPTIONAL_JSON_TARGETS = (
    ("health_report.json", "health_report.schema.json"),
    ("preview_result.json", "preview_result.schema.json"),
)

LOOPPLANE_HOME_JSON_TARGETS = (
    SchemaTarget("config.json", "loopplane_home_config.schema.json"),
    SchemaTarget("registry/workspaces.json", "loopplane_home_workspaces.schema.json"),
    SchemaTarget("runners/agent_runners.local.json", "agent_runners_local.schema.json"),
    SchemaTarget("dashboard/servers.json", "loopplane_home_dashboard_servers.schema.json"),
)


def available_schema_files() -> tuple[Path, ...]:
    return tuple(sorted(SCHEMA_DIR.glob("*.schema.json")))


def schema_targets(project_root: Path | str, workflow_config: Mapping[str, Any] | None = None) -> tuple[SchemaTarget, ...]:
    project = Path(project_root).expanduser().resolve()
    if workflow_config is None:
        _workflow, paths, _workflow_path, _workflow_error = _workflow_config_for_validation(project)
    else:
        paths = _paths_for_targets(project, workflow_config)
    config_dir = paths.workflow_config_dir_value

    targets: list[SchemaTarget] = [
        SchemaTarget(".loopplane/workspace.json", "workspace.schema.json", required=False),
        SchemaTarget(".loopplane/workflow_registry.json", "workflow_registry.schema.json", required=False),
        SchemaTarget(".loopplane/current_workflow.json", "current_workflow.schema.json", required=False),
        SchemaTarget(paths.workflow_config_file_value, CONFIG_SCHEMA_FILES["workflow.json"]),
        SchemaTarget(f"{config_dir}/agent_runners.json", CONFIG_SCHEMA_FILES["agent_runners.json"]),
        SchemaTarget(f"{config_dir}/dashboard.json", CONFIG_SCHEMA_FILES["dashboard.json"]),
        SchemaTarget(f"{config_dir}/security.json", CONFIG_SCHEMA_FILES["security.json"]),
        SchemaTarget(f"{config_dir}/schema_version.json", CONFIG_SCHEMA_FILES["schema_version.json"]),
    ]
    for filename, schema_file in WORKSPACE_CONFIG_SCHEMA_FILES.items():
        targets.append(SchemaTarget(f".loopplane/config/{filename}", schema_file, required=False))
    targets.append(SchemaTarget(".loopplane/config/local/agent_runners.local.json", "agent_runners_local.schema.json", required=False))
    version_control_path = paths.value("version_control_config_file")
    targets.append(SchemaTarget(version_control_path, CONFIG_SCHEMA_FILES["version_control.json"]))

    runtime_dir = paths.value("runtime_dir")
    read_models_dir = paths.value("read_models_dir")
    for relative, schema_file in RUNTIME_JSON_TARGETS:
        targets.append(SchemaTarget(f"{runtime_dir}/{relative}", schema_file))
    objective_reports_dir = paths.runtime_dir / "objectives"
    if objective_reports_dir.exists():
        for report_path in sorted(objective_reports_dir.rglob("*.json")):
            if not _is_objective_verification_report_path(paths, report_path):
                continue
            targets.append(SchemaTarget(_path_for_record(project, report_path), "objective_verification_report.schema.json", required=False))
    dashboard_server_target = _dashboard_server_state_target(project, paths)
    if dashboard_server_target is not None:
        targets.append(SchemaTarget(dashboard_server_target, "dashboard_server.schema.json", required=False))
    for relative, schema_file in READ_MODEL_JSON_TARGETS:
        targets.append(SchemaTarget(f"{read_models_dir}/{relative}", schema_file, required=False))
    for relative, schema_file in READ_MODEL_JSONL_TARGETS:
        targets.append(SchemaTarget(f"{read_models_dir}/{relative}", schema_file, required=False, kind="jsonl"))
    run_details_dir = paths.read_models_dir / "run_details"
    if run_details_dir.exists():
        for detail_path in sorted(run_details_dir.glob("*.json")):
            targets.append(SchemaTarget(_path_for_record(project, detail_path), "read_model_run_detail.schema.json", required=False))
    for relative, schema_file in OPTIONAL_JSON_TARGETS:
        targets.append(SchemaTarget(f"{runtime_dir}/{relative}", schema_file, required=False))
    return tuple(_dedupe_targets(targets))


def _is_objective_verification_report_path(paths: WorkflowPaths, path: Path) -> bool:
    try:
        relative = path.relative_to(paths.runtime_dir / "objectives")
    except ValueError:
        return False
    parts = relative.parts
    if parts == ("final_objective_verification.json",):
        return True
    return len(parts) == 3 and parts[0] == "phases" and parts[2] == "objective_verification.json"


def loopplane_home_schema_targets(home: Path | str) -> tuple[SchemaTarget, ...]:
    root = Path(home).expanduser().resolve()
    targets = list(LOOPPLANE_HOME_JSON_TARGETS)
    runner_locks = sorted((root / "locks" / "runner_locks").glob("*.lock"))
    for lock_path in runner_locks:
        try:
            relative = lock_path.relative_to(root).as_posix()
        except ValueError:
            continue
        targets.append(SchemaTarget(relative, "runner_resource_lock.schema.json"))
    return tuple(_dedupe_targets(targets))


def validate_loopplane_home_schemas(home: Path | str) -> dict[str, Any]:
    root = Path(home).expanduser().resolve()
    errors: list[str] = []
    checked_files: list[str] = []
    schemas_used: list[str] = []

    for target in loopplane_home_schema_targets(root):
        path = root / target.relative_path
        if not path.exists():
            if target.required:
                errors.append(f"{target.relative_path}: required JSON file is missing")
            continue
        checked_files.append(target.relative_path)
        schema = _load_schema(target.schema_file)
        schemas_used.append(target.schema_file)
        if target.kind == "jsonl":
            records, read_error = _read_jsonl_values(path)
            if read_error:
                errors.append(f"{target.relative_path}: {read_error}")
                continue
            for line_number, record in records:
                location = f"{target.relative_path}:{line_number}"
                errors.extend(_validate_against_schema(record, schema, location))
                if isinstance(record, Mapping):
                    errors.extend(
                        _custom_json_checks(
                            location,
                            record,
                            workflow_id=workflow_id,
                            workspace_id=workspace_id,
                            registry_workflow_ids=registry_workflow_ids,
                            current_workflow_id=current_workflow_id,
                        )
                    )
            continue
        data, read_error = _read_json_value(path)
        if read_error:
            errors.append(f"{target.relative_path}: {read_error}")
            continue
        errors.extend(_validate_against_schema(data, schema, target.relative_path))
        if isinstance(data, Mapping):
            errors.extend(_timestamp_field_errors(target.relative_path, data))

    status = "pass" if not errors else "fail"
    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "ok": status == "pass",
        "status": status,
        "loopplane_home": root.as_posix(),
        "checked_files": sorted(checked_files),
        "schemas_used": sorted(set(schemas_used)),
        "errors": errors,
        "warnings": [],
        "schema_dir": _path_for_record(root, SCHEMA_DIR),
    }


def check_project_schema_version(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    checked_files: list[str] = []
    errors: list[str] = []
    versions: list[tuple[str, str]] = []

    workflow_data, paths, workflow_path, workflow_error = _workflow_config_for_validation(project)
    schema_version_path = paths.config_file("schema_version.json")
    schema_version_relative = _path_for_record(project, schema_version_path)
    schema_version_data, schema_error = _read_json_object(schema_version_path)
    checked_files.append(schema_version_relative)
    if schema_error:
        return _compatibility_result(
            project=project,
            status="fail",
            checked_files=checked_files,
            versions=versions,
            errors=[f"{schema_version_relative}: {schema_error}"],
        )
    versions.append((f"{schema_version_relative}:schema_version", str(schema_version_data.get("schema_version") or "")))
    files = schema_version_data.get("files")
    if isinstance(files, Mapping):
        for name, version in sorted(files.items()):
            versions.append((f"{schema_version_relative}:files.{name}", str(version)))
    else:
        errors.append(f"{schema_version_relative}: files must be an object")

    workflow_relative = _path_for_record(project, workflow_path)
    checked_files.append(workflow_relative)
    if workflow_error:
        errors.append(f"{workflow_relative}: {workflow_error}")
    else:
        versions.append((f"{workflow_relative}:schema_version", str(workflow_data.get("schema_version") or "")))

    migration_versions = _versions_requiring_migration(versions)
    if migration_versions:
        return _compatibility_result(
            project=project,
            status="migration_required",
            checked_files=checked_files,
            versions=versions,
            errors=[
                f"{location}: schema_version {version!r} is not supported by runtime schema {SCHEMA_VERSION!r}"
                for location, version in migration_versions
            ],
        )
    if errors:
        return _compatibility_result(
            project=project,
            status="fail",
            checked_files=checked_files,
            versions=versions,
            errors=errors,
        )
    return _compatibility_result(
        project=project,
        status="current",
        checked_files=checked_files,
        versions=versions,
        errors=[],
    )


def validate_project_schemas(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checked_files: list[str] = []
    schemas_used: list[str] = []
    workflow_id: str | None = None

    try:
        compatibility_metadata = ensure_compatibility_workflow_metadata(
            project,
            updated_by="loopplane schema-validation",
        )
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError) as error:
        compatibility_metadata = {
            "ok": False,
            "status": "invalid_compatibility_metadata",
            "errors": [str(error)],
        }

    compatibility = check_project_schema_version(project)
    if compatibility["status"] == "migration_required":
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "migration_required",
            "project_root": project.as_posix(),
            "workflow_id": None,
            "checked_files": compatibility["checked_files"],
            "schemas_used": [],
            "errors": compatibility["errors"],
            "warnings": [],
            "versions": compatibility.get("versions", []),
            "schema_dir": _path_for_record(project, SCHEMA_DIR),
        }

    workflow_config, paths_for_validation, workflow_path, workflow_error = _workflow_config_for_validation(project)
    workflow_relative = _path_for_record(project, workflow_path)
    if workflow_error:
        errors.append(f"{workflow_relative}: {workflow_error}")
        workflow_config = {}
    if isinstance(workflow_config.get("workflow_id"), str):
        workflow_id = str(workflow_config["workflow_id"])
    elif paths_for_validation.workflow_id:
        workflow_id = paths_for_validation.workflow_id
    workspace_data, _workspace_error = _read_json_object(project / ".loopplane" / "workspace.json")
    workspace_id = workspace_data.get("workspace_id") if isinstance(workspace_data.get("workspace_id"), str) else None
    registry_data, _registry_error = _read_json_object(project / ".loopplane" / "workflow_registry.json")
    registry_workflow_ids = _registry_workflow_ids(registry_data)
    current_data, _current_error = _read_json_object(project / ".loopplane" / "current_workflow.json")
    current_workflow_id = (
        current_data.get("current_workflow_id") if isinstance(current_data.get("current_workflow_id"), str) else None
    )
    targets = schema_targets(project, workflow_config if isinstance(workflow_config, Mapping) and workflow_config else None)
    schema_version_path = paths_for_validation.config_file("schema_version.json")
    schema_version_relative = _path_for_record(project, schema_version_path)
    schema_version_data, schema_version_error = _read_json_object(schema_version_path)
    if schema_version_error:
        errors.append(f"{schema_version_relative}: {schema_version_error}")
        schema_version_data = {}

    for target in targets:
        path = project / target.relative_path
        if not path.exists():
            if target.required:
                errors.append(f"{target.relative_path}: required JSON file is missing")
            continue
        checked_files.append(target.relative_path)
        schema = _load_schema(target.schema_file)
        schemas_used.append(target.schema_file)
        data, read_error = _read_json_value(path)
        if read_error:
            errors.append(f"{target.relative_path}: {read_error}")
            continue
        errors.extend(_validate_against_schema(data, schema, target.relative_path))
        if isinstance(data, Mapping):
            errors.extend(
                _custom_json_checks(
                    target.relative_path,
                    data,
                    workflow_id=workflow_id,
                    workspace_id=workspace_id,
                    registry_workflow_ids=registry_workflow_ids,
                    current_workflow_id=current_workflow_id,
                )
            )

    errors.extend(_v16_identity_file_presence_errors(project))
    errors.extend(_schema_version_file_checks(schema_version_data, targets, schema_version_relative=schema_version_relative))
    if workflow_config:
        try:
            workflow_path_values(workflow_config)
        except WorkflowPathError as error:
            errors.append(f"{workflow_relative}: {error}")
    if not compatibility_metadata.get("ok"):
        errors.extend(f"v1.5 compatibility metadata: {error}" for error in compatibility_metadata.get("errors", []))
    else:
        compatibility_warnings = compatibility_metadata.get("warnings")
        if isinstance(compatibility_warnings, Sequence) and not isinstance(compatibility_warnings, (str, bytes)):
            warnings.extend(str(warning) for warning in compatibility_warnings)
        if compatibility_metadata.get("status") == "created":
            created = compatibility_metadata.get("created")
            if isinstance(created, Sequence) and not isinstance(created, (str, bytes)):
                warnings.append("created v1.6 compatibility metadata files: " + ", ".join(str(item) for item in created))

    status = "pass" if not errors else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "pass",
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "checked_files": sorted(checked_files),
        "schemas_used": sorted(set(schemas_used)),
        "errors": errors,
        "warnings": warnings,
        "schema_dir": _path_for_record(project, SCHEMA_DIR),
    }


def schema_validation_exit_code(result: Mapping[str, Any]) -> int:
    status = str(result.get("status") or "")
    if status in {"pass", "current", "no_op", "migrated"} or result.get("ok") is True:
        return EXIT_SUCCESS
    if status == "migration_required" or _result_mentions_migration(result):
        return EXIT_MIGRATION_REQUIRED
    return EXIT_INVALID_CONFIG


def format_schema_validation_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane schema validation: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"project: {result.get('project_root') or 'unknown'}",
    ]
    checked = result.get("checked_files")
    if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)):
        lines.append(f"checked_files: {len(checked)}")
    schemas = result.get("schemas_used")
    if isinstance(schemas, Sequence) and not isinstance(schemas, (str, bytes)):
        lines.append(f"schemas_used: {len(schemas)}")
    for key in ("errors", "warnings"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def validate_json_value_against_schema(instance: Any, schema_file: str, location: str) -> list[str]:
    """Validate one in-memory JSON value with the same lightweight schema engine."""
    schema = _load_schema(schema_file)
    return _validate_against_schema(instance, schema, location)


def _compatibility_result(
    *,
    project: Path,
    status: str,
    checked_files: Sequence[str],
    versions: Sequence[tuple[str, str]],
    errors: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "current",
        "status": status,
        "project_root": project.as_posix(),
        "checked_files": list(checked_files),
        "versions": [{"location": location, "version": version} for location, version in versions],
        "errors": list(errors),
        "warnings": [],
        "current_schema_version": SCHEMA_VERSION,
        "migratable_from": sorted(MIGRATABLE_SCHEMA_VERSIONS),
    }


def _versions_requiring_migration(versions: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    for location, version in versions:
        if not version:
            problems.append((location, version))
            continue
        if _schema_version_location_accepts(location, version):
            continue
        problems.append((location, version))
    return problems


def _schema_version_location_accepts(location: str, version: str) -> bool:
    if version == SCHEMA_VERSION:
        return True
    if version != WORKSPACE_SCHEMA_VERSION:
        return False
    if "schema_version.json:schema_version" in location:
        return True
    return location.endswith(":files.schema_version.json") or location.endswith("/schema_version.json")


def _read_optional_workflow(project: Path) -> Mapping[str, Any] | None:
    workflow, _paths, _path, error = _workflow_config_for_validation(project)
    if error:
        return None
    return workflow


def _workflow_config_for_validation(project: Path) -> tuple[dict[str, Any], WorkflowPaths, Path, str | None]:
    flat_workflow_path = project / ".loopplane" / "config" / "workflow.json"
    try:
        loaded = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, loaded)
        workflow, error = _read_json_object(paths.workflow_config_file)
        return workflow, paths, paths.workflow_config_file, error
    except (OSError, json.JSONDecodeError, WorkflowPathError):
        workflow, error = _read_json_object(flat_workflow_path)
        paths = _paths_for_targets(project, workflow if not error else None)
        return workflow, paths, flat_workflow_path, error


def _paths_for_targets(project: Path, workflow: Mapping[str, Any] | None) -> WorkflowPaths:
    try:
        return WorkflowPaths.from_config(project, workflow or {}, use_defaults=True)
    except WorkflowPathError:
        return WorkflowPaths.from_config(project, dict(DEFAULT_WORKFLOW_PATHS), use_defaults=True)


def _dedupe_targets(targets: Sequence[SchemaTarget]) -> list[SchemaTarget]:
    seen: set[str] = set()
    deduped: list[SchemaTarget] = []
    for target in targets:
        if target.relative_path in seen:
            continue
        seen.add(target.relative_path)
        deduped.append(target)
    return deduped


def _dashboard_server_state_target(project: Path, paths: WorkflowPaths) -> str | None:
    dashboard_config, error = _read_json_object(paths.config_file("dashboard.json"))
    raw_value = None if error else dashboard_config.get("server_state_file")
    value = str(raw_value or f"{paths.value('runtime_dir')}/dashboard_server.json").strip()
    if not value:
        return None
    for key, replacement in paths.template_variables().items():
        value = value.replace("{{" + key + "}}", replacement)
    if not _valid_project_path(value):
        return None
    return PurePosixPath(value).as_posix()


def _load_schema(schema_file: str) -> Mapping[str, Any]:
    path = SCHEMA_DIR / schema_file
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: schema must be a JSON object")
    return data


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


def _read_json_value(path: Path) -> tuple[Any, str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error.msg}"
    except OSError as error:
        return None, f"read error: {type(error).__name__}: {error}"


def _read_jsonl_values(path: Path) -> tuple[list[tuple[int, Any]], str | None]:
    records: list[tuple[int, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append((line_number, json.loads(line)))
            except json.JSONDecodeError as error:
                return [], f"invalid JSONL on line {line_number}: {error.msg}"
    except FileNotFoundError:
        return [], "missing"
    except OSError as error:
        return [], f"read error: {type(error).__name__}: {error}"
    return records, None


def _validate_against_schema(instance: Any, schema: Mapping[str, Any], location: str) -> list[str]:
    errors: list[str] = []
    _validate_schema_node(instance, schema, location, errors)
    return errors


def _validate_schema_node(instance: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branch_errors: list[list[str]] = []
        for branch in any_of:
            if not isinstance(branch, Mapping):
                continue
            candidate_errors: list[str] = []
            _validate_schema_node(instance, branch, path, candidate_errors)
            if not candidate_errors:
                return
            branch_errors.append(candidate_errors)
        errors.append(f"{path}: does not match any allowed schema")
        return

    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(instance, expected_type):
        errors.append(f"{path}: expected {expected_type}, got {type(instance).__name__}")
        return

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {instance!r}")
    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        errors.append(f"{path}: value {instance!r} is not one of {enum!r}")
    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(instance) < min_length:
            errors.append(f"{path}: string is shorter than {min_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.match(pattern, instance) is None:
            errors.append(f"{path}: value {instance!r} does not match pattern {pattern!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            errors.append(f"{path}: value {instance!r} is less than {minimum!r}")
        if isinstance(maximum, (int, float)) and instance > maximum:
            errors.append(f"{path}: value {instance!r} is greater than {maximum!r}")

    if isinstance(instance, Mapping):
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in instance:
                    errors.append(f"{path}: missing required field {field!r}")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for field, subschema in properties.items():
                if field in instance and isinstance(subschema, Mapping):
                    _validate_schema_node(instance[field], subschema, f"{path}.{field}", errors)
        additional = schema.get("additionalProperties", True)
        if additional is False and isinstance(properties, Mapping):
            unknown = sorted(str(key) for key in set(instance) - set(properties))
            if unknown:
                errors.append(f"{path}: unknown field(s): {', '.join(unknown)}")
        elif isinstance(additional, Mapping) and isinstance(properties, Mapping):
            for field, value in instance.items():
                if field not in properties:
                    _validate_schema_node(value, additional, f"{path}.{field}", errors)

    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(instance):
                _validate_schema_node(item, items, f"{path}[{index}]", errors)


def _type_matches(instance: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(instance, item) for item in expected)
    if expected == "object":
        return isinstance(instance, Mapping)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return True


def _custom_json_checks(
    relative_path: str,
    data: Mapping[str, Any],
    *,
    workflow_id: str | None,
    workspace_id: str | None,
    registry_workflow_ids: set[str] | None,
    current_workflow_id: str | None,
) -> list[str]:
    errors: list[str] = []
    expected_schema_version = _expected_schema_version_for_target(relative_path)
    if expected_schema_version is not None and "schema_version" in data and data.get("schema_version") != expected_schema_version:
        errors.append(f"{relative_path}: schema_version must be {expected_schema_version}")
    if relative_path == ".loopplane/workspace.json":
        data_workspace_id = data.get("workspace_id")
        if not isinstance(data_workspace_id, str) or WORKSPACE_ID_RE.match(data_workspace_id) is None:
            errors.append(f"{relative_path}: workspace_id does not match {WORKSPACE_ID_RE.pattern}")
        if workflow_id and data_workspace_id == workflow_id:
            errors.append(f"{relative_path}: workspace_id must be distinct from workflow_id")
        errors.extend(
            _workspace_identity_custom_errors(
                relative_path,
                data,
                workflow_id=workflow_id,
                current_workflow_id=current_workflow_id,
                registry_workflow_ids=registry_workflow_ids,
            )
        )
    if relative_path == ".loopplane/workflow_registry.json":
        errors.extend(
            _workflow_registry_custom_errors(
                relative_path,
                data,
                workflow_id,
                workspace_id,
            )
        )
    if relative_path == ".loopplane/current_workflow.json":
        errors.extend(
            _current_workflow_custom_errors(
                relative_path,
                data,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                registry_workflow_ids=registry_workflow_ids,
            )
        )
    if workflow_id and "workflow_id" in data and data.get("workflow_id") != workflow_id:
        errors.append(f"{relative_path}: workflow_id {data.get('workflow_id')!r} does not match workflow.json {workflow_id!r}")
    if "workflow_id" in data and isinstance(data.get("workflow_id"), str) and not WORKFLOW_ID_RE.match(str(data["workflow_id"])):
        errors.append(f"{relative_path}: workflow_id does not match {WORKFLOW_ID_RE.pattern}")
    errors.extend(_timestamp_field_errors(relative_path, data))
    errors.extend(_path_field_errors(relative_path, data))
    return errors


def _expected_schema_version_for_target(relative_path: str) -> str | None:
    if relative_path in V16_SCHEMA_VERSION_PROJECT_TARGETS:
        return WORKSPACE_SCHEMA_VERSION
    if relative_path in SCHEMA_VERSION_VALIDATED_BY_SCHEMA_ONLY:
        return None
    if relative_path.endswith("/schema_version.json"):
        return None
    if relative_path.endswith("/dashboard_server.json"):
        return None
    return SCHEMA_VERSION


def _workspace_identity_custom_errors(
    relative_path: str,
    data: Mapping[str, Any],
    *,
    workflow_id: str | None,
    current_workflow_id: str | None,
    registry_workflow_ids: set[str] | None,
) -> list[str]:
    errors: list[str] = []
    if "current_workflow_id" not in data:
        return errors
    workspace_current_workflow_id = data.get("current_workflow_id")
    if (
        not isinstance(workspace_current_workflow_id, str)
        or WORKFLOW_ID_RE.match(workspace_current_workflow_id) is None
    ):
        errors.append(f"{relative_path}: current_workflow_id does not match {WORKFLOW_ID_RE.pattern}")
        return errors
    data_workspace_id = data.get("workspace_id")
    if isinstance(data_workspace_id, str) and workspace_current_workflow_id == data_workspace_id:
        errors.append(f"{relative_path}: current_workflow_id must be distinct from workspace_id")
    if workflow_id and workspace_current_workflow_id != workflow_id:
        errors.append(
            f"{relative_path}: current_workflow_id {workspace_current_workflow_id!r} does not match "
            f"workflow.json {workflow_id!r}"
        )
    if current_workflow_id and workspace_current_workflow_id != current_workflow_id:
        errors.append(
            f"{relative_path}: current_workflow_id {workspace_current_workflow_id!r} does not match "
            f"current_workflow.json {current_workflow_id!r}"
        )
    if registry_workflow_ids is not None and workspace_current_workflow_id not in registry_workflow_ids:
        errors.append(
            f"{relative_path}: current_workflow_id {workspace_current_workflow_id!r} does not reference a workflow in "
            ".loopplane/workflow_registry.json"
        )
    return errors


def _v16_identity_file_presence_errors(project: Path) -> list[str]:
    files = (
        ".loopplane/workspace.json",
        ".loopplane/workflow_registry.json",
        ".loopplane/current_workflow.json",
    )
    existing = {relative for relative in files if (project / relative).exists()}
    if not existing or existing == set(files):
        return []
    return [f"{relative}: required when any v1.6 workspace identity file exists" for relative in files if relative not in existing]


def _registry_workflow_ids(data: Mapping[str, Any]) -> set[str] | None:
    workflows = data.get("workflows")
    if not isinstance(workflows, list):
        return None
    workflow_ids: set[str] = set()
    for record in workflows:
        if isinstance(record, Mapping) and isinstance(record.get("workflow_id"), str):
            workflow_ids.add(str(record["workflow_id"]))
    return workflow_ids


def _workflow_registry_custom_errors(
    relative_path: str,
    data: Mapping[str, Any],
    workflow_id: str | None,
    workspace_id: str | None,
) -> list[str]:
    errors: list[str] = []
    registry_workspace_id = data.get("workspace_id")
    if not isinstance(registry_workspace_id, str) or WORKSPACE_ID_RE.match(registry_workspace_id) is None:
        errors.append(f"{relative_path}: workspace_id does not match {WORKSPACE_ID_RE.pattern}")
    elif workspace_id and registry_workspace_id != workspace_id:
        errors.append(
            f"{relative_path}: workspace_id {registry_workspace_id!r} does not match workspace.json {workspace_id!r}"
        )

    workflows = data.get("workflows")
    if not isinstance(workflows, list):
        return errors

    seen_workflow_ids: set[str] = set()
    active_running_records: list[tuple[str, str]] = []
    has_current_workflow = workflow_id is None
    for index, record in enumerate(workflows):
        location = f"{relative_path}.workflows[{index}]"
        if not isinstance(record, Mapping):
            continue
        record_workflow_id = record.get("workflow_id")
        if isinstance(record_workflow_id, str):
            if record_workflow_id in seen_workflow_ids:
                errors.append(f"{location}.workflow_id: duplicate workflow_id {record_workflow_id!r}")
            seen_workflow_ids.add(record_workflow_id)
            if workspace_id and record_workflow_id == workspace_id:
                errors.append(f"{location}.workflow_id: workflow_id must be distinct from workspace_id")
            if workflow_id and record_workflow_id == workflow_id:
                has_current_workflow = True
        status = record.get("status")
        if isinstance(status, str) and status not in WORKFLOW_HISTORY_STATUSES:
            errors.append(f"{location}.status: value {status!r} is not a supported workflow-history status")
        elif (
            isinstance(status, str)
            and status in ACTIVE_RUNNING_WORKFLOW_STATUSES
            and isinstance(record_workflow_id, str)
        ):
            active_running_records.append((record_workflow_id, status))
    if not has_current_workflow:
        errors.append(f"{relative_path}: workflows must include current workflow_id {workflow_id!r}")
    if len(active_running_records) > 1:
        records = ", ".join(f"{workflow_id}:{status}" for workflow_id, status in active_running_records)
        errors.append(
            f"{relative_path}: one active-running workflow per workspace is allowed by default; found {records}"
        )
    return errors


def _current_workflow_custom_errors(
    relative_path: str,
    data: Mapping[str, Any],
    *,
    workflow_id: str | None,
    workspace_id: str | None,
    registry_workflow_ids: set[str] | None,
) -> list[str]:
    errors: list[str] = []
    pointer_workspace_id = data.get("workspace_id")
    if not isinstance(pointer_workspace_id, str) or WORKSPACE_ID_RE.match(pointer_workspace_id) is None:
        errors.append(f"{relative_path}: workspace_id does not match {WORKSPACE_ID_RE.pattern}")
    elif workspace_id and pointer_workspace_id != workspace_id:
        errors.append(
            f"{relative_path}: workspace_id {pointer_workspace_id!r} does not match workspace.json {workspace_id!r}"
        )

    current_workflow_id = data.get("current_workflow_id")
    if not isinstance(current_workflow_id, str) or WORKFLOW_ID_RE.match(current_workflow_id) is None:
        errors.append(f"{relative_path}: current_workflow_id does not match {WORKFLOW_ID_RE.pattern}")
        return errors
    if workspace_id and current_workflow_id == workspace_id:
        errors.append(f"{relative_path}: current_workflow_id must be distinct from workspace_id")
    if workflow_id and current_workflow_id != workflow_id:
        errors.append(
            f"{relative_path}: current_workflow_id {current_workflow_id!r} does not match workflow.json {workflow_id!r}"
        )
    if registry_workflow_ids is not None and current_workflow_id not in registry_workflow_ids:
        errors.append(
            f"{relative_path}: current_workflow_id {current_workflow_id!r} does not reference a workflow in "
            ".loopplane/workflow_registry.json"
        )
    return errors


def _timestamp_field_errors(relative_path: str, data: Any, prefix: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            if _is_timestamp_key(str(key)) and value is not None:
                if not isinstance(value, str) or TIMESTAMP_RE.match(value) is None:
                    errors.append(f"{relative_path}.{child_prefix}: timestamp must use UTC YYYY-MM-DDTHH:MM:SSZ")
            errors.extend(_timestamp_field_errors(relative_path, value, child_prefix))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            errors.extend(_timestamp_field_errors(relative_path, item, f"{prefix}[{index}]"))
    return errors


def _is_timestamp_key(key: str) -> bool:
    return key.endswith("_at") or key in {"generated_at", "checked_at", "heartbeat_at", "lease_expires_at"}


def _path_field_errors(relative_path: str, data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if relative_path.endswith("/workflow.json"):
        for field in WORKFLOW_PATH_FIELDS:
            value = data.get(field)
            if not _valid_project_path(value):
                errors.append(f"{relative_path}.{field}: must be a project-relative POSIX path")
    if relative_path.endswith("/dashboard.json"):
        for field in ("read_models_dir", "server_state_file"):
            if not _valid_project_path(data.get(field)):
                errors.append(f"{relative_path}.{field}: must be a project-relative POSIX path")
    if relative_path.endswith("/security.json"):
        dashboard = data.get("dashboard")
        if isinstance(dashboard, Mapping) and not _valid_project_path(dashboard.get("token_file")):
            errors.append(f"{relative_path}.dashboard.token_file: must be a project-relative POSIX path")
        file_access = data.get("file_access")
        if isinstance(file_access, Mapping):
            for field in ("allowlist", "denylist"):
                values = file_access.get(field)
                if isinstance(values, list):
                    for index, value in enumerate(values):
                        if not _valid_project_path(value):
                            errors.append(f"{relative_path}.file_access.{field}[{index}]: must be a project-relative POSIX path")
    if relative_path.endswith("/version_control.json"):
        path_policy = data.get("path_policy")
        if isinstance(path_policy, Mapping):
            for field in ("include", "exclude"):
                values = path_policy.get(field)
                if isinstance(values, list):
                    for index, value in enumerate(values):
                        if not _valid_project_path(value):
                            errors.append(f"{relative_path}.path_policy.{field}[{index}]: must be a project-relative POSIX path")
    if relative_path.endswith("/schema_version.json"):
        files = data.get("files")
        if isinstance(files, Mapping):
            for key in files:
                if not isinstance(key, str) or not key:
                    errors.append(f"{relative_path}.files: keys must be non-empty path strings")
                    continue
                if "/" in key or key.endswith(".md"):
                    if not _valid_project_path(key):
                        errors.append(f"{relative_path}.files.{key}: must be a project-relative POSIX path")
    if relative_path == ".loopplane/workflow_registry.json":
        workflows = data.get("workflows")
        if isinstance(workflows, list):
            for index, record in enumerate(workflows):
                if not isinstance(record, Mapping):
                    continue
                for field in (
                    "workflow_root",
                    "plan_file",
                    "read_models_dir",
                    "runtime_dir",
                    "requests_dir",
                    "completion_marker",
                ):
                    if field in record and not _valid_project_path(record.get(field)):
                        errors.append(f"{relative_path}.workflows[{index}].{field}: must be a project-relative POSIX path")
    return errors


def _valid_project_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if "\\" in value or value.startswith("/") or value.startswith("~"):
        return False
    path = PurePosixPath(value)
    if path == PurePosixPath(".") or ".." in path.parts:
        return False
    return True


def _schema_version_file_checks(
    schema_version_data: Mapping[str, Any],
    targets: Sequence[SchemaTarget],
    *,
    schema_version_relative: str,
) -> list[str]:
    errors: list[str] = []
    files = schema_version_data.get("files")
    if not isinstance(files, Mapping):
        return errors
    required_keys = {target.path_key for target in targets if target.required}
    for key in sorted(required_keys):
        version = files.get(key)
        if not _schema_version_file_entry_accepts(key, version):
            errors.append(
                f"{schema_version_relative}.files.{key}: expected {SCHEMA_VERSION!r}"
                f" or {WORKSPACE_SCHEMA_VERSION!r} for schema_version.json, got {version!r}"
            )
    for key, version in files.items():
        if not _schema_version_file_entry_accepts(str(key), version):
            errors.append(
                f"{schema_version_relative}.files.{key}: expected {SCHEMA_VERSION!r}"
                f" or {WORKSPACE_SCHEMA_VERSION!r} for schema_version.json, got {version!r}"
            )
    return errors


def _schema_version_file_entry_accepts(key: str, version: Any) -> bool:
    if key == "schema_version.json" or key.endswith("/schema_version.json"):
        return version in {SCHEMA_VERSION, WORKSPACE_SCHEMA_VERSION}
    return version == SCHEMA_VERSION


def _result_mentions_migration(result: Mapping[str, Any]) -> bool:
    haystack = json.dumps(result, sort_keys=True, default=str).lower()
    return "migration" in haystack or "unsupported" in haystack


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()
