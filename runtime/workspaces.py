from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.loopplane_home import LOOPPLANE_HOME_AUTHORITY, loopplane_home_layout, ensure_loopplane_home_layout, resolve_loopplane_home
from runtime.exit_codes import (
    EXIT_INVALID_CONFIG,
    EXIT_MIGRATION_REQUIRED,
    EXIT_SECURITY_POLICY_VIOLATION,
    EXIT_SUCCESS,
)
from runtime.path_resolution import (
    WORKFLOW_PATH_FIELDS,
    WorkflowPathError,
    WorkflowPaths,
    load_workflow_config,
    resolve_current_workflow_roots,
)
from runtime.schema_validation import validate_project_schemas
from runtime.workspace_identity import workspace_identity_summary
from runtime.workspace_nesting import detect_nested_loopplane_instances
from runtime.workflow_lifecycle import WorkflowLifecycleError, ensure_compatibility_workflow_metadata


WORKSPACE_COMMAND_SCHEMA_VERSION = "1.6"
GLOBAL_WORKSPACE_REGISTRY_SCHEMA_VERSION = "1.6"
GLOBAL_WORKSPACE_REGISTRY_AUTHORITY = LOOPPLANE_HOME_AUTHORITY
WORKSPACE_ID_RE = re.compile(r"^ws_[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
WORKFLOW_ID_RE = re.compile(r"^wf_[0-9]{8}_[0-9a-f]{8}$")
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


class WorkspaceRegistryError(RuntimeError):
    pass


def load_current_workspace(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    markers = {
        "workspace": project / ".loopplane" / "workspace.json",
        "workflow_registry": project / ".loopplane" / "workflow_registry.json",
        "current_workflow": project / ".loopplane" / "current_workflow.json",
        "workflow_config": project / ".loopplane" / "config" / "workflow.json",
    }
    if not any(path.exists() for path in markers.values()):
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No LoopPlane workspace metadata or flat workflow config was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
            ],
        )

    try:
        compatibility = ensure_compatibility_workflow_metadata(
            project,
            updated_by="loopplane workspace current",
        )
        validation = validate_project_schemas(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError, WorkflowLifecycleError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to load workspace metadata: {error}"],
            recovery_actions=[
                "Inspect .loopplane/workspace.json, .loopplane/workflow_registry.json, and .loopplane/current_workflow.json.",
                "Restore the workspace metadata from a checkpoint or backup.",
            ],
        )

    if not validation.get("ok"):
        return _failure(
            status=str(validation.get("status") or "invalid_workspace_metadata"),
            project=project,
            errors=[str(error) for error in validation.get("errors", [])],
            warnings=[str(warning) for warning in validation.get("warnings", [])],
            recovery_actions=[
                "Run loopplane migrate --project <project> when schema migration is required.",
                "Repair invalid project-local LoopPlane metadata before selecting workflows.",
            ],
            extra={
                "compatibility_metadata": compatibility,
                "schema_validation": _schema_validation_summary(validation),
            },
        )

    try:
        workspace = _load_json_object(markers["workspace"])
        registry = _load_json_object(markers["workflow_registry"])
        current = _load_json_object(markers["current_workflow"])
        resolution = resolve_current_workflow_roots(project)
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to resolve current workspace workflow: {error}"],
            recovery_actions=[
                "Ensure current_workflow.json points to a workflow in workflow_registry.json.",
                "Ensure the selected workflow_root and workflow_config_file stay inside the project.",
            ],
            extra={
                "compatibility_metadata": compatibility,
                "schema_validation": _schema_validation_summary(validation),
            },
        )

    workflows = _workflow_records(registry)
    current_workflow_id = str(current.get("current_workflow_id") or resolution.workflow_id or "")
    selected = _workflow_record(workflows, current_workflow_id) or dict(resolution.registry_record or {})
    warnings = [str(warning) for warning in validation.get("warnings", [])]
    warnings.extend(str(warning) for warning in compatibility.get("warnings", []))
    identity = workspace_identity_summary(project, workspace)
    nesting = _detect_nesting(project)
    warnings.extend(str(warning) for warning in nesting.get("warnings", []))

    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "current",
        "project_root": project.as_posix(),
        "workspace_project_root": identity["project_root"],
        "repo_root": identity["repo_root"],
        "resolved_project_root": identity["resolved_project_root"],
        "resolved_repo_root": identity["resolved_repo_root"],
        "workspace_boundary": identity["workspace_boundary"],
        "resolved_workspace_boundary": identity["resolved_workspace_boundary"],
        "allow_out_of_boundary_writes": identity["allow_out_of_boundary_writes"],
        "workspace_root": resolution.workspace_root_value,
        "workspace_id": str(workspace.get("workspace_id") or ""),
        "workspace": workspace,
        "workspace_identity": identity,
        "current_workflow_id": current_workflow_id,
        "current_workflow": current,
        "workflow": selected,
        "workflow_count": len(workflows),
        "workflow_root": resolution.workflow_root_value,
        "workflow_config_file": resolution.workflow_config_file_value,
        "workflow_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
        "nested_workspaces": nesting,
        "compatibility_metadata": compatibility,
        "schema_validation": _schema_validation_summary(validation),
        "errors": [],
        "warnings": warnings,
    }


def workspace_current_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return EXIT_SUCCESS
    if str(result.get("status") or "") == "migration_required":
        return EXIT_MIGRATION_REQUIRED
    return EXIT_INVALID_CONFIG


def register_workspace(project_root: Path | str, *, loopplane_home: Path | str | None = None) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    layout = loopplane_home_layout(loopplane_home)
    home = layout.home
    registry_file = layout.workspace_registry_file
    base_extra = {
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
    }

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Run loopplane init --project <project> --brief <brief> before registering a workspace.",
            ],
            extra=base_extra,
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workspace register."],
            extra=base_extra,
        )

    current = load_current_workspace(project)
    if current.get("ok") is not True:
        extra = {
            **base_extra,
            "current_workspace": {
                "status": current.get("status"),
                "errors": list(current.get("errors", [])),
                "warnings": list(current.get("warnings", [])),
                "recovery_actions": list(current.get("recovery_actions", [])),
            },
        }
        return _failure(
            status=str(current.get("status") or "invalid_workspace_metadata"),
            project=project,
            errors=[str(error) for error in current.get("errors", [])],
            warnings=[str(warning) for warning in current.get("warnings", [])],
            recovery_actions=[str(action) for action in current.get("recovery_actions", [])],
            extra=extra,
        )

    now = utc_timestamp()
    try:
        ensure_loopplane_home_layout(home)
        global_registry = _load_global_workspace_registry(registry_file)
        workspace = _as_mapping(current.get("workspace"))
        existing_entries = _global_workspace_entries(global_registry)
        replaced_entries = _matching_global_workspace_entries(existing_entries, project, str(current["workspace_id"]))
        entry = _global_workspace_entry(
            project=project,
            current=current,
            workspace=workspace,
            previous=replaced_entries[0] if replaced_entries else None,
            updated_at=now,
        )
        updated_entries = _upsert_global_workspace_entry(existing_entries, entry, project)
        updated_registry = _global_workspace_registry_payload(generated_at=now, entries=updated_entries)
        _atomic_write_json(registry_file, updated_registry)
    except (OSError, json.JSONDecodeError, WorkspaceRegistryError) as error:
        return _failure(
            status="invalid_global_registry",
            project=project,
            errors=[f"Unable to update LOOPPLANE_HOME workspace registry: {error}"],
            recovery_actions=[
                "Inspect or remove the machine-local registry file, then rerun workspace register.",
                "Project-local .loopplane metadata remains authoritative and was not replaced by LOOPPLANE_HOME.",
            ],
            extra=base_extra,
        )

    warnings = [str(warning) for warning in current.get("warnings", [])]
    warnings.extend(_replacement_warnings(replaced_entries, project, str(current["workspace_id"])))
    status = "updated" if replaced_entries else "registered"
    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "project_root": project.as_posix(),
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "workspace_id": str(current["workspace_id"]),
        "current_workflow_id": str(current.get("current_workflow_id") or ""),
        "workspace": dict(workspace),
        "registry_entry": entry,
        "registry_count": len(updated_entries),
        "replaced_count": len(replaced_entries),
        "compatibility_metadata": current.get("compatibility_metadata"),
        "schema_validation": current.get("schema_validation"),
        "errors": [],
        "warnings": warnings,
    }


def workspace_register_exit_code(result: Mapping[str, Any]) -> int:
    return workspace_current_exit_code(result)


def unregister_workspace(workspace_id: str | None, *, loopplane_home: Path | str | None = None) -> dict[str, Any]:
    layout = loopplane_home_layout(loopplane_home)
    home = layout.home
    registry_file = layout.workspace_registry_file
    normalized_workspace_id = str(workspace_id or "").strip()
    base_extra = {
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
    }

    if not normalized_workspace_id:
        return _registry_failure(
            status="missing_workspace_id",
            workspace_id=normalized_workspace_id,
            errors=["workspace_id is required."],
            recovery_actions=["Pass a workspace id such as ws_01JZ8Q3X6R9K7J2N4M5P6Q7R8S."],
            extra=base_extra,
        )
    if WORKSPACE_ID_RE.match(normalized_workspace_id) is None:
        return _registry_failure(
            status="invalid_workspace_id",
            workspace_id=normalized_workspace_id,
            errors=[f"workspace_id must match {WORKSPACE_ID_RE.pattern}"],
            recovery_actions=["Use the project-local workspace_id from loopplane workspace current --json."],
            extra=base_extra,
        )

    try:
        ensure_loopplane_home_layout(home)
        global_registry = _load_global_workspace_registry(registry_file)
        existing_entries = _global_workspace_entries(global_registry)
        removed_entries = [
            dict(entry)
            for entry in existing_entries
            if str(entry.get("workspace_id") or "") == normalized_workspace_id
        ]
        if not removed_entries:
            return _registry_failure(
                status="not_registered",
                workspace_id=normalized_workspace_id,
                errors=[f"Workspace is not registered in LOOPPLANE_HOME: {normalized_workspace_id}"],
                recovery_actions=[
                    "Run loopplane workspace register <project> to add this workspace to the machine-local index.",
                    "Run loopplane workspace current --project <project> --json to confirm the project-local workspace_id.",
                ],
                extra={
                    **base_extra,
                    "registry_count": len(existing_entries),
                },
            )
        updated_entries = [
            dict(entry)
            for entry in existing_entries
            if str(entry.get("workspace_id") or "") != normalized_workspace_id
        ]
        updated_registry = _global_workspace_registry_payload(generated_at=utc_timestamp(), entries=updated_entries)
        _atomic_write_json(registry_file, updated_registry)
    except (OSError, json.JSONDecodeError, WorkspaceRegistryError) as error:
        return _registry_failure(
            status="invalid_global_registry",
            workspace_id=normalized_workspace_id,
            errors=[f"Unable to update LOOPPLANE_HOME workspace registry: {error}"],
            recovery_actions=[
                "Inspect or remove the machine-local registry file, then rerun workspace unregister.",
                "Project-local .loopplane metadata remains authoritative and was not changed by LOOPPLANE_HOME.",
            ],
            extra=base_extra,
        )

    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "unregistered",
        "workspace_id": normalized_workspace_id,
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "registry_count": len(updated_entries),
        "removed_count": len(removed_entries),
        "removed_entries": removed_entries,
        "errors": [],
        "warnings": [],
    }


def workspace_unregister_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def scan_workspaces(
    directory: Path | str | Sequence[Path | str | None] | None,
    *,
    loopplane_home: Path | str | None = None,
    allow_nested_workspace: bool = False,
    workspace_namespace: str | None = None,
) -> dict[str, Any]:
    layout = loopplane_home_layout(loopplane_home)
    home = layout.home
    registry_file = layout.workspace_registry_file
    base_extra = {
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
    }
    raw_directories = _scan_directory_inputs(directory)
    if not raw_directories:
        return _scan_failure(
            status="missing_scan_directory",
            scan_root="",
            errors=["Scan directory is required."],
            recovery_actions=["Pass a directory such as loopplane workspace scan <directory>."],
            extra=base_extra,
        )

    scan_roots: list[Path] = []
    seen_scan_roots: set[str] = set()
    for raw_directory in raw_directories:
        scan_root = Path(raw_directory).expanduser()
        if not scan_root.exists():
            return _scan_failure(
                status="missing_scan_directory",
                scan_root=scan_root.as_posix(),
                errors=[f"Scan directory does not exist: {scan_root}"],
                recovery_actions=["Create the directory first or pass an existing parent directory."],
                extra={**base_extra, "scan_roots": [root.as_posix() for root in scan_roots]},
            )
        if not scan_root.is_dir():
            return _scan_failure(
                status="invalid_scan_directory",
                scan_root=scan_root.as_posix(),
                errors=[f"Scan path is not a directory: {scan_root}"],
                recovery_actions=["Pass a directory, not a file, to loopplane workspace scan."],
                extra={**base_extra, "scan_roots": [root.as_posix() for root in scan_roots]},
            )
        scan_root = scan_root.resolve()
        scan_root_value = scan_root.as_posix()
        if scan_root_value in seen_scan_roots:
            continue
        seen_scan_roots.add(scan_root_value)
        scan_roots.append(scan_root)

    discovered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_workspace_ids: dict[str, str] = {}
    for scan_root in scan_roots:
        for project in _iter_workspace_scan_candidates(scan_root):
            candidate = _load_workspace_scan_candidate(project)
            if candidate.get("ok") is not True:
                skipped.append(candidate)
                continue
            workspace_id = str(candidate.get("workspace_id") or "")
            previous_project = seen_workspace_ids.get(workspace_id)
            if previous_project:
                skipped.append(
                    _scan_skipped_candidate(
                        project=Path(str(candidate["project_root"])),
                        status="duplicate_workspace_id",
                        errors=[
                            f"workspace_id {workspace_id} was already discovered at {previous_project}; "
                            "project-local metadata must be made unique before scan can index both projects."
                        ],
                        workspace_id=workspace_id,
                        current_workflow_id=str(candidate.get("current_workflow_id") or ""),
                    )
                )
                continue
            seen_workspace_ids[workspace_id] = str(candidate["project_root"])
            discovered.append(candidate)

    warnings: list[str] = []
    nested_workspace_count = 0
    for candidate in discovered:
        nesting = _detect_nesting(Path(str(candidate["project_root"])))
        candidate["nested_workspaces"] = nesting
        candidate["nested_workspace_count"] = int(nesting.get("nested_workspace_count") or 0)
        nested_workspace_count += int(candidate["nested_workspace_count"])
        warnings.extend(str(warning) for warning in nesting.get("warnings", []))

    if nested_workspace_count and not allow_nested_workspace and not str(workspace_namespace or "").strip():
        return _scan_failure(
            status="nested_workspace_requires_explicit_namespace",
            scan_root=scan_roots[0].as_posix() if scan_roots else "",
            errors=[
                (
                    "workspace scan discovered nested LoopPlane instances and refused to update "
                    "the LOOPPLANE_HOME registry without an explicit nested workspace namespace or allow flag."
                )
            ],
            warnings=warnings,
            recovery_actions=[
                "Rerun with --allow-nested-workspace after verifying parent and child workspace ownership.",
                "Use --workspace-namespace to record an explicit nested scan namespace.",
                "Run loopplane workspace doctor --project <project> for each nested workspace before scanning.",
            ],
            extra={
                **base_extra,
                "scan_roots": [root.as_posix() for root in scan_roots],
                "discovered_count": len(discovered),
                "skipped_count": len(skipped),
                "nested_workspace_count": nested_workspace_count,
                "workspaces": discovered,
                "skipped": skipped,
                "registry_update": {
                    "status": "blocked_nested_workspace",
                    "registry_mutated": False,
                },
            },
        )

    try:
        ensure_loopplane_home_layout(home)
        now = utc_timestamp()
        global_registry = _load_global_workspace_registry(registry_file)
        existing_entries = _global_workspace_entries(global_registry)
        existing_scope_entries = [
            dict(entry)
            for entry in existing_entries
            if any(_registry_entry_under_scan_root(entry, scan_root) for scan_root in scan_roots)
        ]
        updated_entries = [
            dict(entry)
            for entry in existing_entries
            if not any(_registry_entry_under_scan_root(entry, scan_root) for scan_root in scan_roots)
        ]
        for candidate in discovered:
            project = Path(str(candidate["project_root"]))
            previous_entries = _matching_global_workspace_entries(
                existing_entries,
                project,
                str(candidate["workspace_id"]),
            )
            entry = _global_workspace_entry(
                project=project,
                current=candidate,
                workspace=_as_mapping(candidate.get("workspace")),
                previous=previous_entries[0] if previous_entries else None,
                updated_at=now,
            )
            candidate["registry_entry"] = entry
            updated_entries = _upsert_global_workspace_entry(updated_entries, entry, project)
            warnings.extend(_replacement_warnings(previous_entries, project, str(candidate["workspace_id"])))

        discovered_projects = {str(candidate["project_root"]) for candidate in discovered}
        discovered_workspace_ids = {str(candidate["workspace_id"]) for candidate in discovered}
        removed_stale_entries = [
            dict(entry)
            for entry in existing_scope_entries
            if not _entry_matches_discovered(entry, discovered_projects, discovered_workspace_ids)
        ]
        updated_registry = _global_workspace_registry_payload(generated_at=now, entries=updated_entries)
        _atomic_write_json(registry_file, updated_registry)
    except (OSError, json.JSONDecodeError, WorkspaceRegistryError) as error:
        return _scan_failure(
            status="invalid_global_registry",
            scan_root=scan_roots[0].as_posix() if scan_roots else "",
            errors=[f"Unable to rebuild LOOPPLANE_HOME workspace registry: {error}"],
            recovery_actions=[
                "Inspect or remove the machine-local registry file, then rerun workspace scan.",
                "Project-local .loopplane metadata remains authoritative and was not changed by LOOPPLANE_HOME.",
            ],
            extra={
                **base_extra,
                "workspaces": discovered,
                "skipped": skipped,
                "discovered_count": len(discovered),
                "skipped_count": len(skipped),
                "scan_roots": [root.as_posix() for root in scan_roots],
            },
        )

    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "scanned",
        "scan_root": scan_roots[0].as_posix() if scan_roots else "",
        "scan_roots": [root.as_posix() for root in scan_roots],
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "discovered_count": len(discovered),
        "skipped_count": len(skipped),
        "nested_workspace_count": nested_workspace_count,
        "workspaces": discovered,
        "skipped": skipped,
        "registry_update": {
            "status": "rebuilt_scan_scope",
            "mode": "scan_scope" if len(scan_roots) == 1 else "scan_scopes",
            "previous_registry_count": len(existing_entries),
            "registry_count": len(updated_entries),
            "scanned_scope_existing_count": len(existing_scope_entries),
            "upserted_count": len(discovered),
            "removed_stale_count": len(removed_stale_entries),
            "removed_stale_entries": removed_stale_entries,
        },
        "errors": [],
        "warnings": warnings,
    }


def workspace_scan_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return EXIT_SUCCESS
    if str(result.get("status") or "") == "nested_workspace_requires_explicit_namespace":
        return EXIT_SECURITY_POLICY_VIOLATION
    return EXIT_INVALID_CONFIG


def list_workspaces(*, loopplane_home: Path | str | None = None) -> dict[str, Any]:
    layout = loopplane_home_layout(loopplane_home)
    home = layout.home
    registry_file = layout.workspace_registry_file
    registry_exists = registry_file.exists()
    base_extra = {
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "registry_exists": registry_exists,
    }

    try:
        global_registry = _load_global_workspace_registry(registry_file)
        entries = _global_workspace_entries(global_registry)
        workspaces = [_global_workspace_list_record(entry, index) for index, entry in enumerate(entries)]
    except (OSError, json.JSONDecodeError, WorkspaceRegistryError) as error:
        return _list_failure(
            status="invalid_global_registry",
            errors=[f"Unable to read LOOPPLANE_HOME workspace registry: {error}"],
            recovery_actions=[
                "Inspect or remove the machine-local registry file, then rerun workspace list.",
                "Project-local .loopplane metadata remains authoritative and was not changed by LOOPPLANE_HOME.",
            ],
            extra=base_extra,
        )

    stale_workspaces = [
        workspace
        for workspace in workspaces
        if _as_mapping(workspace.get("health")).get("ok") is not True
    ]
    missing_statuses = {"missing_project_root", "missing_project", "invalid_project"}
    missing_workspaces = [
        workspace
        for workspace in stale_workspaces
        if str(_as_mapping(workspace.get("health")).get("status") or "") in missing_statuses
    ]
    warnings: list[str] = []
    for workspace in stale_workspaces:
        workspace_id = str(workspace.get("workspace_id") or "unknown")
        status = str(_as_mapping(workspace.get("health")).get("status") or "unknown")
        project = str(workspace.get("project_root") or "unknown")
        warnings.append(f"Workspace {workspace_id} has {status} health at {project}.")

    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "listed",
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "registry_exists": registry_exists,
        "registry_authority": str(global_registry.get("authority") or "unspecified"),
        "registry_generated_at": global_registry.get("generated_at"),
        "workspace_count": len(workspaces),
        "available_count": len(workspaces) - len(stale_workspaces),
        "stale_count": len(stale_workspaces),
        "missing_count": len(missing_workspaces),
        "workspaces": workspaces,
        "errors": [],
        "warnings": warnings,
    }


def workspace_list_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def doctor_workspace(project_root: Path | str, *, loopplane_home: Path | str | None = None) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    layout = loopplane_home_layout(loopplane_home)
    home = layout.home
    registry_file = layout.workspace_registry_file

    project_health = _workspace_doctor_project_health(project)
    registry_health = _workspace_doctor_global_registry(
        registry_file=registry_file,
        loopplane_home=home,
        project=project,
        project_health=project_health,
    )
    issues = list(project_health.get("issues", [])) + list(registry_health.get("issues", []))
    errors = [str(issue.get("message")) for issue in issues if issue.get("severity") == "error"]
    warnings = [str(issue.get("message")) for issue in issues if issue.get("severity") == "warning"]
    recovery_actions = _dedupe_strings(
        [
            str(action)
            for issue in issues
            for action in issue.get("recovery_actions", [])
            if str(action)
        ]
    )
    status = "healthy" if not errors and not warnings else ("warning" if not errors else "unhealthy")

    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": not errors,
        "status": status,
        "project_root": project.as_posix(),
        "loopplane_home": home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "workspace_id": project_health.get("workspace_id"),
        "current_workflow_id": project_health.get("current_workflow_id"),
        "project": project_health,
        "global_registry": registry_health,
        "issues": issues,
        "errors": errors,
        "warnings": warnings,
        "recovery_actions": recovery_actions,
        "mutation_boundary": (
            "workspace doctor performs read-only diagnostics; it does not create, delete, "
            "or rewrite project-local .loopplane workflow-history files."
        ),
    }


def workspace_doctor_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def format_workspace_current_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workspace current: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        workflow_status = workflow.get("status") if isinstance(workflow, Mapping) else None
        lines.extend(
            [
                f"workspace_id: {result.get('workspace_id') or 'unknown'}",
                f"workspace_project_root: {result.get('workspace_project_root') or 'unknown'}",
                f"repo_root: {result.get('repo_root') or 'unknown'}",
                f"resolved_repo_root: {result.get('resolved_repo_root') or 'unknown'}",
                f"workspace_boundary: {result.get('workspace_boundary') or 'unknown'}",
                f"resolved_workspace_boundary: {result.get('resolved_workspace_boundary') or 'unknown'}",
                f"allow_out_of_boundary_writes: {result.get('allow_out_of_boundary_writes')}",
                f"workspace_root: {result.get('workspace_root') or 'unknown'}",
                f"current_workflow_id: {result.get('current_workflow_id') or 'unknown'}",
                f"workflow_status: {workflow_status or 'unknown'}",
                f"workflow_root: {result.get('workflow_root') or 'unknown'}",
                f"workflow_config_file: {result.get('workflow_config_file') or 'unknown'}",
                f"workflow_count: {result.get('workflow_count')}",
            ]
        )
        nesting = result.get("nested_workspaces")
        if isinstance(nesting, Mapping):
            lines.append(f"nested_workspace_count: {nesting.get('nested_workspace_count', 0)}")
        compatibility = result.get("compatibility_metadata")
        if isinstance(compatibility, Mapping):
            lines.append(f"compatibility_metadata: {compatibility.get('status', 'unknown')}")
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


def format_workspace_register_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workspace register: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"loopplane_home: {result.get('loopplane_home') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
    ]
    if result.get("ok") is True:
        lines.extend(
            [
                f"workspace_id: {result.get('workspace_id') or 'unknown'}",
                f"current_workflow_id: {result.get('current_workflow_id') or 'unknown'}",
                f"registry_count: {result.get('registry_count')}",
            ]
        )
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


def format_workspace_unregister_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workspace unregister: {result.get('status', 'unknown')}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"loopplane_home: {result.get('loopplane_home') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
    ]
    if result.get("ok") is True:
        lines.extend(
            [
                f"removed_count: {result.get('removed_count')}",
                f"registry_count: {result.get('registry_count')}",
            ]
        )
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


def format_workspace_scan_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workspace scan: {result.get('status', 'unknown')}",
        f"scan_root: {result.get('scan_root') or 'unknown'}",
        f"loopplane_home: {result.get('loopplane_home') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
    ]
    scan_roots = result.get("scan_roots")
    if isinstance(scan_roots, Sequence) and len(scan_roots) > 1 and not isinstance(scan_roots, (str, bytes)):
        lines.append("scan_roots:")
        lines.extend(f"  - {scan_root}" for scan_root in scan_roots)
    if result.get("ok") is True:
        registry_update = result.get("registry_update")
        registry_status = registry_update.get("status") if isinstance(registry_update, Mapping) else "unknown"
        registry_count = registry_update.get("registry_count") if isinstance(registry_update, Mapping) else "unknown"
        removed_stale_count = (
            registry_update.get("removed_stale_count") if isinstance(registry_update, Mapping) else "unknown"
        )
        lines.extend(
            [
                f"discovered_count: {result.get('discovered_count')}",
                f"skipped_count: {result.get('skipped_count')}",
                f"nested_workspace_count: {result.get('nested_workspace_count', 0)}",
                f"registry_update: {registry_status}",
                f"registry_count: {registry_count}",
                f"removed_stale_count: {removed_stale_count}",
            ]
        )
        workspaces = result.get("workspaces")
        if isinstance(workspaces, Sequence) and workspaces and not isinstance(workspaces, (str, bytes)):
            lines.append("workspaces:")
            for workspace in workspaces:
                if not isinstance(workspace, Mapping):
                    continue
                lines.append(
                    "  - "
                    f"{workspace.get('workspace_id') or 'unknown'} "
                    f"project={workspace.get('project_root') or 'unknown'} "
                    f"current_workflow_id={workspace.get('current_workflow_id') or 'unknown'} "
                    f"nested={workspace.get('nested_workspace_count', 0)}"
                )
        skipped = result.get("skipped")
        if isinstance(skipped, Sequence) and skipped and not isinstance(skipped, (str, bytes)):
            lines.append("skipped:")
            for candidate in skipped:
                if not isinstance(candidate, Mapping):
                    continue
                lines.append(
                    "  - "
                    f"{candidate.get('status') or 'unknown'} "
                    f"project={candidate.get('project_root') or 'unknown'}"
                )
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


def format_workspace_list_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workspace list: {result.get('status', 'unknown')}",
        f"loopplane_home: {result.get('loopplane_home') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
        f"registry_exists: {bool(result.get('registry_exists'))}",
        f"registry_authority: {result.get('registry_authority') or 'unspecified'}",
    ]
    if result.get("ok") is True:
        lines.extend(
            [
                f"workspace_count: {result.get('workspace_count')}",
                f"available_count: {result.get('available_count')}",
                f"stale_count: {result.get('stale_count')}",
                f"missing_count: {result.get('missing_count')}",
            ]
        )
        workspaces = result.get("workspaces")
        if isinstance(workspaces, Sequence) and workspaces and not isinstance(workspaces, (str, bytes)):
            lines.append("workspaces:")
            for workspace in workspaces:
                if not isinstance(workspace, Mapping):
                    continue
                health = _as_mapping(workspace.get("health"))
                lines.append(
                    "  - "
                    f"{workspace.get('workspace_id') or 'unknown'} "
                    f"status={workspace.get('status') or 'unknown'} "
                    f"health={health.get('status') or 'unknown'} "
                    f"project={workspace.get('project_root') or 'unknown'} "
                    f"current_workflow_id={workspace.get('current_workflow_id') or 'unknown'}"
                )
        else:
            lines.append("workspaces: none")
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


def format_workspace_doctor_text(result: Mapping[str, Any]) -> str:
    project_health = _as_mapping(result.get("project"))
    registry_health = _as_mapping(result.get("global_registry"))
    lines = [
        f"loopplane workspace doctor: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'unknown'}",
        f"project_local: {project_health.get('status') or 'unknown'}",
        f"global_registry: {registry_health.get('status') or 'unknown'}",
        f"loopplane_home: {result.get('loopplane_home') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
        f"registry_authority: {registry_health.get('registry_authority') or 'unspecified'}",
    ]
    registry_count = registry_health.get("registry_count")
    if registry_count is not None:
        lines.append(f"registry_count: {registry_count}")
    stale_count = registry_health.get("stale_count")
    if stale_count is not None:
        lines.append(f"stale_registry_count: {stale_count}")
    layout = project_health.get("layout")
    if layout:
        lines.append(f"layout: {layout}")
    workflow_count = project_health.get("workflow_count")
    if workflow_count is not None:
        lines.append(f"workflow_count: {workflow_count}")
    nesting = project_health.get("nested_workspaces")
    if isinstance(nesting, Mapping):
        lines.append(f"nested_workspace_count: {nesting.get('nested_workspace_count', 0)}")

    issues = result.get("issues")
    if isinstance(issues, Sequence) and issues and not isinstance(issues, (str, bytes)):
        lines.append("issues:")
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            lines.append(
                "  - "
                f"[{issue.get('severity') or 'unknown'}] "
                f"{issue.get('code') or 'unknown'}: "
                f"{issue.get('message') or ''}"
            )
    recovery_actions = result.get("recovery_actions")
    if isinstance(recovery_actions, Sequence) and recovery_actions and not isinstance(recovery_actions, (str, bytes)):
        lines.append("recovery_actions:")
        lines.extend(f"  - {value}" for value in recovery_actions)
    mutation_boundary = result.get("mutation_boundary")
    if mutation_boundary:
        lines.append(f"mutation_boundary: {mutation_boundary}")
    return "\n".join(lines) + "\n"


def _workspace_doctor_project_health(project: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    tracked = {
        ".loopplane/workspace.json": project / ".loopplane" / "workspace.json",
        ".loopplane/workflow_registry.json": project / ".loopplane" / "workflow_registry.json",
        ".loopplane/current_workflow.json": project / ".loopplane" / "current_workflow.json",
    }

    if not project.exists():
        issues.append(
            _workspace_doctor_issue(
                "error",
                "missing_project",
                f"Project path does not exist: {project}",
                ["Create the project directory first or pass --project <existing-project>."],
            )
        )
        return _workspace_doctor_project_result(
            project=project,
            status="missing_project",
            checks=checks,
            issues=issues,
        )
    if not project.is_dir():
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_project",
                f"Project path is not a directory: {project}",
                ["Pass a LoopPlane project directory to loopplane workspace doctor."],
            )
        )
        return _workspace_doctor_project_result(
            project=project,
            status="invalid_project",
            checks=checks,
            issues=issues,
        )

    missing = [relative for relative, path in tracked.items() if not path.is_file()]
    for relative, path in tracked.items():
        checks.append(
            {
                "name": relative,
                "path": path.as_posix(),
                "status": "missing" if relative in missing else "present",
                "ok": relative not in missing,
            }
        )
    if missing:
        flat_config = project / ".loopplane" / "config" / "workflow.json"
        compatibility_status = "flat_config_present" if flat_config.is_file() else "unavailable"
        issues.append(
            _workspace_doctor_issue(
                "error",
                "missing_workspace_metadata",
                "Project is missing required v1.6 workspace-history metadata: " + ", ".join(missing),
                [
                    "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                    (
                        "For a valid v1.5 flat workflow, run loopplane workspace current --project <project> "
                        "to materialize compatibility metadata intentionally."
                    ),
                    "Restore .loopplane/workspace.json, .loopplane/workflow_registry.json, and .loopplane/current_workflow.json from a checkpoint or backup.",
                ],
            )
        )
        return _workspace_doctor_project_result(
            project=project,
            status="missing_workspace_metadata",
            checks=checks,
            issues=issues,
            missing_files=missing,
            compatibility={
                "status": compatibility_status,
                "flat_workflow_config": flat_config.as_posix(),
                "read_only": True,
                "created_files": [],
            },
        )

    loaded: dict[str, dict[str, Any]] = {}
    for relative, path in tracked.items():
        try:
            loaded[relative] = _load_json_object(path)
        except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
            code = "invalid_" + path.name.replace(".json", "_json")
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    code,
                    f"{relative} is not a valid JSON object: {error}",
                    [
                        f"Repair {relative} or restore it from a checkpoint.",
                        "Project-local .loopplane metadata is authoritative; do not rely on LOOPPLANE_HOME to replace it.",
                    ],
                )
            )
            _set_check_status(checks, relative, "invalid", False, str(error))

    if issues:
        return _workspace_doctor_project_result(
            project=project,
            status="invalid_workspace_metadata",
            checks=checks,
            issues=issues,
        )

    workspace = loaded[".loopplane/workspace.json"]
    registry = loaded[".loopplane/workflow_registry.json"]
    current = loaded[".loopplane/current_workflow.json"]

    workspace_id = str(workspace.get("workspace_id") or "").strip()
    current_workflow_id = str(current.get("current_workflow_id") or "").strip()
    workflows = _workflow_records(registry)

    if workspace.get("schema_version") != WORKSPACE_COMMAND_SCHEMA_VERSION:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_workspace_schema_version",
                ".loopplane/workspace.json schema_version must be 1.6.",
                ["Run loopplane migrate --project <project> when schema migration is required."],
            )
        )
    if WORKSPACE_ID_RE.match(workspace_id) is None:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_workspace_id",
                f".loopplane/workspace.json workspace_id must match {WORKSPACE_ID_RE.pattern}.",
                ["Restore a valid project-local workspace_id from a checkpoint or backup."],
            )
        )
    if registry.get("schema_version") != WORKSPACE_COMMAND_SCHEMA_VERSION:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_workflow_registry_schema_version",
                ".loopplane/workflow_registry.json schema_version must be 1.6.",
                ["Repair .loopplane/workflow_registry.json before switching workflows."],
            )
        )
    if str(registry.get("workspace_id") or "") != workspace_id:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "workflow_registry_workspace_mismatch",
                ".loopplane/workflow_registry.json workspace_id does not match .loopplane/workspace.json.",
                ["Restore a registry that belongs to this workspace_id."],
            )
        )
    raw_workflows = registry.get("workflows")
    if not isinstance(raw_workflows, Sequence) or isinstance(raw_workflows, (str, bytes)) or not raw_workflows:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_workflow_registry_workflows",
                ".loopplane/workflow_registry.json workflows must be a non-empty array.",
                ["Rebuild or restore .loopplane/workflow_registry.json from project-local workflow history."],
            )
        )
    if current.get("schema_version") != WORKSPACE_COMMAND_SCHEMA_VERSION:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_current_workflow_schema_version",
                ".loopplane/current_workflow.json schema_version must be 1.6.",
                ["Repair .loopplane/current_workflow.json before running workflow commands."],
            )
        )
    if str(current.get("workspace_id") or "") != workspace_id:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "current_workflow_workspace_mismatch",
                ".loopplane/current_workflow.json workspace_id does not match .loopplane/workspace.json.",
                ["Restore a current workflow pointer that belongs to this workspace_id."],
            )
        )
    if WORKFLOW_ID_RE.match(current_workflow_id) is None:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_current_workflow_id",
                f".loopplane/current_workflow.json current_workflow_id must match {WORKFLOW_ID_RE.pattern}.",
                ["Set current_workflow_id to a workflow_id present in .loopplane/workflow_registry.json."],
            )
        )

    invalid_workflow_records = _invalid_workflow_record_issues(workflows)
    issues.extend(invalid_workflow_records)
    selected = _workflow_record(workflows, current_workflow_id)
    if selected is None and current_workflow_id:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "current_workflow_not_registered",
                ".loopplane/current_workflow.json points to a workflow_id that is not present in .loopplane/workflow_registry.json.",
                [
                    "Repair .loopplane/current_workflow.json or .loopplane/workflow_registry.json so the pointer targets a registered workflow.",
                    "Do not infer workflow truth from LOOPPLANE_HOME; project-local files are authoritative.",
                ],
            )
        )

    active_running = [
        str(record.get("workflow_id") or "")
        for record in workflows
        if str(record.get("status") or "") in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    if workspace.get("single_active_running_workflow") is not False and len(active_running) > 1:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "multiple_active_running_workflows",
                "Workspace policy allows one active/running workflow, but multiple registry records are active: "
                + ", ".join(active_running),
                ["Archive, pause, stop, or repair conflicting workflow records before running scheduler commands."],
            )
        )

    workflow_root = str(selected.get("workflow_root") or "") if selected else ""
    layout = _workflow_layout(workflow_root)
    nesting = _detect_nesting(project)
    for parent in nesting.get("parents", []):
        if not isinstance(parent, Mapping):
            continue
        issues.append(
            _workspace_doctor_issue(
                "warning",
                "nested_parent_workspace",
                "Nested LoopPlane parent workspace detected at "
                f"{parent.get('project_root') or 'unknown'}; nested operations require an explicit namespace or approval.",
                [
                    "Run loopplane workspace doctor --project <parent-project> to inspect the parent workspace.",
                    "Use an explicit namespace or approval path before running operations across nested workspaces.",
                ],
            )
        )
    for child in nesting.get("children", []):
        if not isinstance(child, Mapping):
            continue
        issues.append(
            _workspace_doctor_issue(
                "warning",
                "nested_child_workspace",
                "Nested LoopPlane child workspace detected at "
                f"{child.get('project_root') or 'unknown'}; parent operations must not absorb child workspace truth.",
                [
                    "Run loopplane workspace doctor --project <child-project> to inspect the child workspace.",
                    "Keep parent and child project-local .loopplane metadata authoritative in their own namespaces.",
                ],
            )
        )
    if selected and workflow_root:
        try:
            resolved_workflow_root = _absolute_project_path(project, workflow_root)
        except OSError as error:
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    "invalid_workflow_root",
                    f"Unable to resolve selected workflow_root {workflow_root!r}: {error}",
                    ["Repair the selected workflow_root in .loopplane/workflow_registry.json."],
                )
            )
        else:
            if not _path_is_relative_to(resolved_workflow_root, project):
                issues.append(
                    _workspace_doctor_issue(
                        "error",
                        "workflow_root_outside_project",
                        f"Selected workflow_root resolves outside project_root: {workflow_root}",
                        ["Keep workflow_root paths inside the project-local workspace boundary."],
                    )
                )
            elif not resolved_workflow_root.exists():
                issues.append(
                    _workspace_doctor_issue(
                        "error",
                        "missing_workflow_root",
                        f"Selected workflow_root does not exist: {workflow_root}",
                        ["Restore the selected workflow_root or update the current workflow pointer."],
                    )
                )

    has_error = any(issue.get("severity") == "error" for issue in issues)
    status = "healthy" if not issues else ("warning" if not has_error else "invalid_workspace_metadata")
    return _workspace_doctor_project_result(
        project=project,
        status=status,
        checks=checks,
        issues=issues,
        workspace_id=workspace_id,
        current_workflow_id=current_workflow_id,
        workflow_count=len(workflows),
        workflow=selected or {},
        workflow_root=workflow_root,
        layout=layout,
        nested_workspaces=nesting,
        compatibility={
            "status": "supported" if layout == "compatibility_flat" else "not_needed",
            "read_only": True,
            "created_files": [],
        },
    )


def _workspace_doctor_global_registry(
    *,
    registry_file: Path,
    loopplane_home: Path,
    project: Path,
    project_health: Mapping[str, Any],
) -> dict[str, Any]:
    registry_exists = registry_file.exists()
    issues: list[dict[str, Any]] = []
    try:
        registry = _load_global_workspace_registry(registry_file)
        entries = _global_workspace_entries(registry)
        workspaces = [_global_workspace_list_record(entry, index) for index, entry in enumerate(entries)]
    except (OSError, json.JSONDecodeError, WorkspaceRegistryError) as error:
        issues.append(
            _workspace_doctor_issue(
                "error",
                "invalid_global_registry",
                f"Unable to read LOOPPLANE_HOME workspace registry: {error}",
                [
                    "Inspect or remove the machine-local registry file, then rerun workspace doctor.",
                    "Project-local .loopplane metadata remains authoritative and was not changed by LOOPPLANE_HOME.",
                ],
            )
        )
        return {
            "ok": False,
            "status": "invalid_global_registry",
            "loopplane_home": loopplane_home.as_posix(),
            "registry_file": registry_file.as_posix(),
            "registry_exists": registry_exists,
            "registry_count": 0,
            "available_count": 0,
            "stale_count": 0,
            "missing_count": 0,
            "matching_entries": [],
            "workspaces": [],
            "issues": issues,
        }

    stale_workspaces = [
        workspace
        for workspace in workspaces
        if _as_mapping(workspace.get("health")).get("ok") is not True
    ]
    missing_statuses = {"missing_project_root", "missing_project", "invalid_project"}
    missing_workspaces = [
        workspace
        for workspace in stale_workspaces
        if str(_as_mapping(workspace.get("health")).get("status") or "") in missing_statuses
    ]

    for workspace in stale_workspaces:
        health = _as_mapping(workspace.get("health"))
        workspace_id = str(workspace.get("workspace_id") or "unknown")
        status = str(health.get("status") or "unknown")
        project_root = str(workspace.get("project_root") or "unknown")
        recovery = [
            "Run loopplane workspace scan <directory> to rebuild a scan scope.",
            "Run loopplane workspace unregister <workspace_id> to remove an obsolete machine-local entry.",
        ]
        issues.append(
            _workspace_doctor_issue(
                "warning",
                "stale_global_registry_entry",
                f"LOOPPLANE_HOME registry entry {workspace_id} has {status} health at {project_root}.",
                recovery,
            )
        )

    project_workspace_id = str(project_health.get("workspace_id") or "")
    project_current_workflow_id = str(project_health.get("current_workflow_id") or "")
    project_ok = project_health.get("ok") is True and bool(project_workspace_id)
    matching_entries: list[dict[str, Any]] = []
    if project_ok:
        for workspace in workspaces:
            if (
                str(workspace.get("workspace_id") or "") == project_workspace_id
                or str(workspace.get("project_root") or "") == project.as_posix()
            ):
                matching_entries.append(dict(workspace))
        if not matching_entries:
            issues.append(
                _workspace_doctor_issue(
                    "warning",
                    "workspace_not_registered",
                    "Project-local workspace is not present in $LOOPPLANE_HOME/registry/workspaces.json.",
                    [
                        "Run loopplane workspace register <project> to add this workspace to the machine-local index.",
                        "Local execution can continue because project-local .loopplane metadata is authoritative.",
                    ],
                )
            )
        elif len(matching_entries) > 1:
            issues.append(
                _workspace_doctor_issue(
                    "warning",
                    "duplicate_global_registry_entries",
                    "Multiple LOOPPLANE_HOME registry entries match this project-local workspace.",
                    ["Run loopplane workspace scan <directory> or unregister stale duplicate workspace IDs."],
                )
            )
        for entry in matching_entries:
            entry_workspace_id = str(entry.get("workspace_id") or "")
            entry_current_workflow_id = str(entry.get("current_workflow_id") or "")
            entry_project = str(entry.get("project_root") or "")
            if entry_workspace_id != project_workspace_id:
                issues.append(
                    _workspace_doctor_issue(
                        "warning",
                        "global_registry_workspace_mismatch",
                        f"Registry workspace_id {entry_workspace_id or 'missing'} does not match project-local workspace_id {project_workspace_id}.",
                        ["Run loopplane workspace register <project> to refresh the machine-local index."],
                    )
                )
            if entry_project and entry_project != project.as_posix():
                issues.append(
                    _workspace_doctor_issue(
                        "warning",
                        "global_registry_project_mismatch",
                        f"Registry project_root {entry_project} does not match current project {project.as_posix()}.",
                        ["Run loopplane workspace scan <directory> or unregister the stale workspace entry."],
                    )
                )
            if entry_current_workflow_id and entry_current_workflow_id != project_current_workflow_id:
                issues.append(
                    _workspace_doctor_issue(
                        "warning",
                        "global_registry_current_workflow_mismatch",
                        "Registry current_workflow_id differs from project-local current_workflow.json; project-local workflow truth is authoritative.",
                        ["Run loopplane workspace register <project> to refresh the current_workflow_id in LOOPPLANE_HOME."],
                    )
                )

    status = "ok" if not issues else "warning"
    return {
        "ok": True,
        "status": status,
        "loopplane_home": loopplane_home.as_posix(),
        "registry_file": registry_file.as_posix(),
        "registry_exists": registry_exists,
        "registry_authority": str(registry.get("authority") or "unspecified"),
        "registry_generated_at": registry.get("generated_at"),
        "registry_count": len(workspaces),
        "available_count": len(workspaces) - len(stale_workspaces),
        "stale_count": len(stale_workspaces),
        "missing_count": len(missing_workspaces),
        "matching_entries": matching_entries,
        "workspaces": workspaces,
        "issues": issues,
    }


def _workspace_doctor_project_result(
    *,
    project: Path,
    status: str,
    checks: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    workspace_id: str | None = None,
    current_workflow_id: str | None = None,
    workflow_count: int | None = None,
    workflow: Mapping[str, Any] | None = None,
    workflow_root: str = "",
    layout: str = "",
    nested_workspaces: Mapping[str, Any] | None = None,
    missing_files: Sequence[str] = (),
    compatibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    errors = [str(issue.get("message")) for issue in issues if issue.get("severity") == "error"]
    warnings = [str(issue.get("message")) for issue in issues if issue.get("severity") == "warning"]
    result: dict[str, Any] = {
        "ok": not errors,
        "status": status,
        "project_root": project.as_posix(),
        "loopplane_dir": (project / ".loopplane").as_posix(),
        "workspace_id": workspace_id,
        "current_workflow_id": current_workflow_id,
        "checks": [dict(check) for check in checks],
        "issues": [dict(issue) for issue in issues],
        "errors": errors,
        "warnings": warnings,
        "missing_files": list(missing_files),
        "recovery_actions": _dedupe_strings(
            [
                str(action)
                for issue in issues
                for action in issue.get("recovery_actions", [])
                if str(action)
            ]
        ),
    }
    if workflow_count is not None:
        result["workflow_count"] = workflow_count
    if workflow is not None:
        result["workflow"] = dict(workflow)
    if workflow_root:
        result["workflow_root"] = workflow_root
    if layout:
        result["layout"] = layout
    if nested_workspaces is not None:
        result["nested_workspaces"] = dict(nested_workspaces)
    if compatibility is not None:
        result["compatibility"] = dict(compatibility)
    return result


def _invalid_workflow_record_issues(workflows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(workflows):
        workflow_id = str(record.get("workflow_id") or "")
        status = str(record.get("status") or "")
        workflow_root = str(record.get("workflow_root") or "")
        if WORKFLOW_ID_RE.match(workflow_id) is None:
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    "invalid_workflow_record_id",
                    f".loopplane/workflow_registry.json workflows[{index}].workflow_id is invalid.",
                    ["Repair malformed workflow registry records before using workflow-history commands."],
                )
            )
            continue
        if workflow_id in seen:
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    "duplicate_workflow_record_id",
                    f".loopplane/workflow_registry.json contains duplicate workflow_id {workflow_id}.",
                    ["Keep one registry record per workflow_id."],
                )
            )
        seen.add(workflow_id)
        if status not in WORKFLOW_HISTORY_STATUSES:
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    "invalid_workflow_record_status",
                    f".loopplane/workflow_registry.json workflow {workflow_id} has unsupported status {status!r}.",
                    ["Use one of the workflow-history states defined by LoopPlane.md 31.2."],
                )
            )
        if not workflow_root:
            issues.append(
                _workspace_doctor_issue(
                    "error",
                    "missing_workflow_record_root",
                    f".loopplane/workflow_registry.json workflow {workflow_id} is missing workflow_root.",
                    ["Restore workflow_root in the registry record."],
                )
            )
    return issues


def _workspace_doctor_issue(
    severity: str,
    code: str,
    message: str,
    recovery_actions: Sequence[str],
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "recovery_actions": list(recovery_actions),
    }


def _set_check_status(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    ok: bool,
    error: str | None = None,
) -> None:
    for check in checks:
        if check.get("name") == name:
            check["status"] = status
            check["ok"] = ok
            if error:
                check["error"] = error
            return


def _workflow_layout(workflow_root: str) -> str:
    normalized = workflow_root.rstrip("/")
    if normalized == ".loopplane":
        return "compatibility_flat"
    if normalized.startswith(".loopplane/workflows/"):
        return "canonical_v16"
    if normalized:
        return "custom"
    return ""


def _detect_nesting(project: Path) -> dict[str, Any]:
    try:
        return detect_nested_loopplane_instances(project)
    except OSError as error:
        return {
            "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
            "status": "unknown",
            "project_root": project.as_posix(),
            "requires_explicit_namespace_or_approval": False,
            "parent_count": 0,
            "child_count": 0,
            "overlap_count": 0,
            "nested_workspace_count": 0,
            "parents": [],
            "children": [],
            "overlapping": [],
            "relationships": [],
            "warnings": [f"Unable to scan for nested LoopPlane workspaces: {error}"],
            "recovery_actions": ["Check directory permissions, then rerun the workspace command."],
        }


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def _failure(
    *,
    status: str,
    project: Path,
    errors: Sequence[str],
    warnings: Sequence[str] = (),
    recovery_actions: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workspace_id": None,
        "current_workflow_id": None,
        "workflow_root": None,
        "workflow_config_file": None,
        "errors": list(errors),
        "warnings": list(warnings),
        "recovery_actions": list(recovery_actions),
        **dict(extra or {}),
    }


def _registry_failure(
    *,
    status: str,
    workspace_id: str | None,
    errors: Sequence[str],
    warnings: Sequence[str] = (),
    recovery_actions: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "workspace_id": workspace_id,
        "errors": list(errors),
        "warnings": list(warnings),
        "recovery_actions": list(recovery_actions),
        **dict(extra or {}),
    }


def _scan_failure(
    *,
    status: str,
    scan_root: str,
    errors: Sequence[str],
    warnings: Sequence[str] = (),
    recovery_actions: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "scan_root": scan_root,
        "discovered_count": 0,
        "skipped_count": 0,
        "workspaces": [],
        "skipped": [],
        "errors": list(errors),
        "warnings": list(warnings),
        "recovery_actions": list(recovery_actions),
        **dict(extra or {}),
    }


def _list_failure(
    *,
    status: str,
    errors: Sequence[str],
    warnings: Sequence[str] = (),
    recovery_actions: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_COMMAND_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "workspace_count": 0,
        "available_count": 0,
        "stale_count": 0,
        "missing_count": 0,
        "workspaces": [],
        "errors": list(errors),
        "warnings": list(warnings),
        "recovery_actions": list(recovery_actions),
        **dict(extra or {}),
    }


def _scan_skipped_candidate(
    *,
    project: Path,
    status: str,
    errors: Sequence[str],
    missing_files: Sequence[str] = (),
    workspace_id: str | None = None,
    current_workflow_id: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "loopplane_dir": (project / ".loopplane").as_posix(),
        "workspace_id": workspace_id,
        "current_workflow_id": current_workflow_id,
        "missing_files": list(missing_files),
        "errors": list(errors),
    }


def _schema_validation_summary(validation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": validation.get("schema_version"),
        "ok": bool(validation.get("ok")),
        "status": validation.get("status"),
        "workflow_id": validation.get("workflow_id"),
        "checked_files": list(validation.get("checked_files", [])),
        "schemas_used": list(validation.get("schemas_used", [])),
        "errors": list(validation.get("errors", [])),
        "warnings": list(validation.get("warnings", [])),
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise WorkflowPathError(f"{path}: JSON value must be an object")
    return data


def _loopplane_home(value: Path | str | None) -> Path:
    return resolve_loopplane_home(value)


def _scan_directory_inputs(directory: Path | str | Sequence[Path | str | None] | None) -> list[str]:
    if directory is None:
        return []
    if isinstance(directory, (str, Path)):
        raw_values: Sequence[Path | str | None] = [directory]
    elif isinstance(directory, Sequence) and not isinstance(directory, (str, bytes)):
        raw_values = directory
    else:
        raw_values = [directory]
    return [str(value or "").strip() for value in raw_values if str(value or "").strip()]


def _load_global_workspace_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "authority": GLOBAL_WORKSPACE_REGISTRY_AUTHORITY,
            "schema_version": GLOBAL_WORKSPACE_REGISTRY_SCHEMA_VERSION,
            "workspaces": [],
        }
    data = _load_json_object(path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, Sequence) or isinstance(workspaces, (str, bytes)):
        raise WorkspaceRegistryError(f"{path}: workspaces must be a JSON array")
    for index, entry in enumerate(workspaces):
        if not isinstance(entry, Mapping):
            raise WorkspaceRegistryError(f"{path}: workspaces[{index}] must be a JSON object")
    return data


def _global_workspace_registry_payload(
    *,
    generated_at: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "authority": GLOBAL_WORKSPACE_REGISTRY_AUTHORITY,
        "schema_version": GLOBAL_WORKSPACE_REGISTRY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "workspaces": [dict(entry) for entry in entries],
    }


def _iter_workspace_scan_candidates(scan_root: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_candidate(project: Path) -> None:
        resolved = project.resolve()
        key = resolved.as_posix()
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    if scan_root.name == ".loopplane":
        add_candidate(scan_root.parent)
        return candidates

    def onerror(error: OSError) -> None:
        raise error

    for current_root, dirnames, _filenames in os.walk(scan_root, onerror=onerror):
        current = Path(current_root)
        kept_dirnames = []
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                continue
            if dirname == ".loopplane":
                add_candidate(current)
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames
    return candidates


def _load_workspace_scan_candidate(project: Path) -> dict[str, Any]:
    tracked = {
        ".loopplane/workspace.json": project / ".loopplane" / "workspace.json",
        ".loopplane/workflow_registry.json": project / ".loopplane" / "workflow_registry.json",
        ".loopplane/current_workflow.json": project / ".loopplane" / "current_workflow.json",
    }
    missing = [relative for relative, path in tracked.items() if not path.is_file()]
    if missing:
        return _scan_skipped_candidate(
            project=project,
            status="missing_project_local_truth",
            missing_files=missing,
            errors=[
                "Candidate has a .loopplane directory but is missing required v1.6 workspace identity files. "
                "Scan is read-only and does not materialize compatibility metadata."
            ],
        )

    try:
        workspace = _load_json_object(tracked[".loopplane/workspace.json"])
        registry = _load_json_object(tracked[".loopplane/workflow_registry.json"])
        current = _load_json_object(tracked[".loopplane/current_workflow.json"])
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _scan_skipped_candidate(
            project=project,
            status="invalid_project_local_truth",
            errors=[f"Unable to load project-local workspace metadata: {error}"],
        )

    workspace_id = str(workspace.get("workspace_id") or "").strip()
    current_workflow_id = str(current.get("current_workflow_id") or "").strip()
    if WORKSPACE_ID_RE.match(workspace_id) is None:
        return _scan_skipped_candidate(
            project=project,
            status="invalid_workspace_id",
            workspace_id=workspace_id,
            current_workflow_id=current_workflow_id,
            errors=[f".loopplane/workspace.json workspace_id must match {WORKSPACE_ID_RE.pattern}"],
        )
    if not current_workflow_id:
        return _scan_skipped_candidate(
            project=project,
            status="missing_current_workflow_id",
            workspace_id=workspace_id,
            errors=[".loopplane/current_workflow.json current_workflow_id must be a non-empty string"],
        )

    workflows = _workflow_records(registry)
    selected = _workflow_record(workflows, current_workflow_id)
    if selected is None:
        return _scan_skipped_candidate(
            project=project,
            status="current_workflow_not_registered",
            workspace_id=workspace_id,
            current_workflow_id=current_workflow_id,
            errors=[
                ".loopplane/current_workflow.json points to a workflow_id that is not present in "
                ".loopplane/workflow_registry.json. Scan will not infer conflicting workflow truth."
            ],
        )

    return {
        "ok": True,
        "status": "discovered",
        "project_root": project.as_posix(),
        "loopplane_dir": (project / ".loopplane").as_posix(),
        "workspace_id": workspace_id,
        "workspace": workspace,
        "current_workflow_id": current_workflow_id,
        "workflow_count": len(workflows),
        "workflow": selected,
        "workflow_root": str(selected.get("workflow_root") or ""),
        "workflow_config_file": str(selected.get("workflow_config_file") or ""),
        "workflow_status": str(selected.get("status") or "unknown"),
    }


def _global_workspace_entries(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    workspaces = registry.get("workspaces")
    if not isinstance(workspaces, Sequence) or isinstance(workspaces, (str, bytes)):
        return []
    return [dict(entry) for entry in workspaces if isinstance(entry, Mapping)]


def _matching_global_workspace_entries(
    entries: Sequence[Mapping[str, Any]],
    project: Path,
    workspace_id: str,
) -> list[dict[str, Any]]:
    project_value = project.as_posix()
    matches: list[dict[str, Any]] = []
    for entry in entries:
        entry_workspace_id = str(entry.get("workspace_id") or "")
        entry_project_root = str(entry.get("project_root") or "")
        if entry_workspace_id == workspace_id or entry_project_root == project_value:
            matches.append(dict(entry))
    return matches


def _global_workspace_entry(
    *,
    project: Path,
    current: Mapping[str, Any],
    workspace: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    updated_at: str,
) -> dict[str, Any]:
    current_workflow_id = str(current.get("current_workflow_id") or "")
    entry: dict[str, Any] = {
        "workspace_id": str(current.get("workspace_id") or ""),
        "name": str(workspace.get("name") or project.name or "workspace"),
        "project_root": project.as_posix(),
        "loopplane_dir": (project / ".loopplane").resolve().as_posix(),
        "repo_root": _absolute_project_path(project, workspace.get("repo_root")).as_posix(),
        "status": "registered",
        "last_seen_at": updated_at,
        "current_workflow_id": current_workflow_id,
    }
    if previous and isinstance(previous.get("dashboard"), Mapping):
        entry["dashboard"] = dict(previous["dashboard"])
    return entry


def _upsert_global_workspace_entry(
    entries: Sequence[Mapping[str, Any]],
    new_entry: Mapping[str, Any],
    project: Path,
) -> list[dict[str, Any]]:
    workspace_id = str(new_entry.get("workspace_id") or "")
    project_value = project.as_posix()
    updated: list[dict[str, Any]] = []
    inserted = False
    for entry in entries:
        entry_workspace_id = str(entry.get("workspace_id") or "")
        entry_project_root = str(entry.get("project_root") or "")
        if entry_workspace_id == workspace_id or entry_project_root == project_value:
            if not inserted:
                updated.append(dict(new_entry))
                inserted = True
            continue
        updated.append(dict(entry))
    if not inserted:
        updated.append(dict(new_entry))
    return updated


def _replacement_warnings(entries: Sequence[Mapping[str, Any]], project: Path, workspace_id: str) -> list[str]:
    warnings: list[str] = []
    project_value = project.as_posix()
    for entry in entries:
        prior_project = str(entry.get("project_root") or "")
        prior_workspace_id = str(entry.get("workspace_id") or "")
        if prior_project and prior_project != project_value and prior_workspace_id == workspace_id:
            warnings.append(
                "Replaced an existing LOOPPLANE_HOME registry entry for this workspace_id at "
                f"{prior_project}; project-local metadata at {project_value} is authoritative."
            )
        if prior_workspace_id and prior_workspace_id != workspace_id and prior_project == project_value:
            warnings.append(
                "Replaced a stale LOOPPLANE_HOME registry entry for this project path with "
                f"workspace_id {workspace_id}."
            )
    return warnings


def _registry_entry_under_scan_root(entry: Mapping[str, Any], scan_root: Path) -> bool:
    project_root = str(entry.get("project_root") or "").strip()
    if not project_root:
        return False
    try:
        path = Path(project_root).expanduser().resolve()
    except OSError:
        return False
    return _path_is_relative_to(path, scan_root)


def _entry_matches_discovered(
    entry: Mapping[str, Any],
    discovered_projects: set[str],
    discovered_workspace_ids: set[str],
) -> bool:
    return (
        str(entry.get("project_root") or "") in discovered_projects
        or str(entry.get("workspace_id") or "") in discovered_workspace_ids
    )


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _absolute_project_path(project: Path, value: object) -> Path:
    text = str(value or ".").strip() or "."
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project / path).resolve()


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _workflow_records(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    workflows = registry.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        return []
    return [dict(record) for record in workflows if isinstance(record, Mapping)]


def _workflow_record(records: Sequence[Mapping[str, Any]], workflow_id: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("workflow_id") == workflow_id:
            return dict(record)
    return None


def _global_workspace_list_record(entry: Mapping[str, Any], index: int) -> dict[str, Any]:
    workspace_id = str(entry.get("workspace_id") or "").strip()
    project_root_text = str(entry.get("project_root") or "").strip()
    health = _global_workspace_entry_health(entry, workspace_id, project_root_text)
    record = {
        "registry_index": index,
        "workspace_id": workspace_id,
        "name": str(entry.get("name") or ""),
        "project_root": project_root_text,
        "loopplane_dir": str(entry.get("loopplane_dir") or ""),
        "repo_root": str(entry.get("repo_root") or ""),
        "status": str(entry.get("status") or "unknown"),
        "last_seen_at": str(entry.get("last_seen_at") or ""),
        "current_workflow_id": str(entry.get("current_workflow_id") or ""),
        "health": health,
        "registry_entry": dict(entry),
    }
    if isinstance(entry.get("dashboard"), Mapping):
        record["dashboard"] = dict(entry["dashboard"])
    return record


def _global_workspace_entry_health(
    entry: Mapping[str, Any],
    workspace_id: str,
    project_root_text: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not workspace_id:
        errors.append("Registry entry is missing workspace_id.")
    elif WORKSPACE_ID_RE.match(workspace_id) is None:
        errors.append(f"Registry entry workspace_id must match {WORKSPACE_ID_RE.pattern}")
    if not project_root_text:
        return {
            "ok": False,
            "status": "missing_project_root",
            "errors": errors + ["Registry entry is missing project_root."],
            "warnings": warnings,
        }

    try:
        project = Path(project_root_text).expanduser()
        if not project.is_absolute():
            project = project.resolve()
    except OSError as error:
        return {
            "ok": False,
            "status": "invalid_project",
            "errors": errors + [f"Unable to resolve project_root: {error}"],
            "warnings": warnings,
        }

    if not project.exists():
        return {
            "ok": False,
            "status": "missing_project",
            "errors": errors + [f"Project path does not exist: {project}"],
            "warnings": warnings,
        }
    if not project.is_dir():
        return {
            "ok": False,
            "status": "invalid_project",
            "errors": errors + [f"Project path is not a directory: {project}"],
            "warnings": warnings,
        }

    candidate = _load_workspace_scan_candidate(project.resolve())
    if candidate.get("ok") is not True:
        return {
            "ok": False,
            "status": str(candidate.get("status") or "invalid_project_local_truth"),
            "errors": errors + [str(error) for error in candidate.get("errors", [])],
            "warnings": warnings,
            "missing_files": list(candidate.get("missing_files", [])),
            "project_local_workspace_id": candidate.get("workspace_id"),
            "project_local_current_workflow_id": candidate.get("current_workflow_id"),
        }

    project_workspace_id = str(candidate.get("workspace_id") or "")
    project_current_workflow_id = str(candidate.get("current_workflow_id") or "")
    registry_current_workflow_id = str(entry.get("current_workflow_id") or "")
    status = "ok"
    if project_workspace_id != workspace_id:
        status = "stale_registry_entry"
        errors.append(
            f"Registry workspace_id {workspace_id or 'missing'} does not match project-local "
            f"workspace_id {project_workspace_id}."
        )
    if registry_current_workflow_id and registry_current_workflow_id != project_current_workflow_id:
        status = "stale_registry_entry"
        warnings.append(
            "Registry current_workflow_id differs from project-local current_workflow.json; "
            "project-local workflow truth is authoritative."
        )

    return {
        "ok": status == "ok" and not errors,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "project_local_workspace_id": project_workspace_id,
        "project_local_current_workflow_id": project_current_workflow_id,
        "project_local_workflow_status": str(candidate.get("workflow_status") or "unknown"),
        "project_local_workflow_count": candidate.get("workflow_count"),
        "project_local_workflow_root": str(candidate.get("workflow_root") or ""),
    }
