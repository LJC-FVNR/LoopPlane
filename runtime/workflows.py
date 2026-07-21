from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.active_projections import sync_active_workflow_projections
from runtime.adapters.base import utc_timestamp
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.init_workflow import InitConflictError, materialize_canonical_workflow_files
from runtime.version_control import plan_local_repository_initialization
from runtime.workspace_identity import normalize_identity_path_value
from runtime.workflow_lifecycle import (
    WorkflowLifecycleError,
    archive_workflow as archive_workflow_record,
    create_workflow_record,
    fork_workflow as fork_workflow_record,
    restore_workflow as restore_workflow_record,
    set_current_workflow,
)


WORKFLOW_COMMAND_SCHEMA_VERSION = "1.6"
WORKSPACE_ID_RE = re.compile(r"^ws_[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
WORKFLOW_ID_RE = re.compile(r"^wf_[0-9]{8}_[0-9a-f]{8}$")
READ_MODEL_SUMMARY_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "workflow_graph.json",
    "version_control_status.json",
    "metrics.json",
)
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
RUNTIME_RUNNING_WORKFLOW_STATUSES = frozenset({"running"})
ACTIVE_RUN_LEASE_ACTIVE_STATUSES = frozenset({"starting", "running", "pending", "waiting"})
ACTIVE_RUN_LEASE_INACTIVE_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "aborted", "released"})
TERMINAL_SUPERVISOR_STATUSES = frozenset({"completed", "stopped", "requires_attention", "failed", "exited"})
ACTIVE_SUPERVISOR_STATUSES = frozenset(
    {
        "launching",
        "running",
        "paused",
        "waiting_config",
        "waiting_approval",
        "waiting_background",
        "waiting_runner_availability",
        "restarting_source_update",
        "unknown",
    }
)
SCHEDULER_LOCK_TTL_SECONDS = 120
ACTIVE_RUN_LEASE_TTL_SECONDS = 120
SUPERVISOR_HEARTBEAT_TTL_SECONDS = 120
WORKFLOW_SWITCH_SELECTION_REASON = "cli_workflow_switch"
WORKFLOW_SWITCH_UPDATED_BY = "loopplane workflow switch"
WORKFLOW_CREATE_SELECTION_REASON = "cli_workflow_create"
WORKFLOW_CREATE_UPDATED_BY = "loopplane workflow create"
WORKFLOW_ARCHIVE_UPDATED_BY = "loopplane workflow archive"
WORKFLOW_RESTORE_SELECTION_REASON = "cli_workflow_restore"
WORKFLOW_RESTORE_UPDATED_BY = "loopplane workflow restore"
WORKFLOW_FORK_SELECTION_REASON = "cli_workflow_fork"
WORKFLOW_FORK_UPDATED_BY = "loopplane workflow fork"


def list_workflows(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to list workflow history.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow list."],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not loopplane_dir.exists():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not registry_file.is_file():
        recovery = [
            "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
            "Run loopplane workspace current --project <project> intentionally for a valid v1.5 flat workflow.",
        ]
        if not workspace_file.is_file():
            recovery.insert(0, "Run loopplane init --project <project> --brief <brief> for a new workspace.")
        return _failure(
            status="missing_workspace",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=recovery,
            extra=_file_context(workspace_file, registry_file, current_file),
        )

    try:
        workspace = _load_optional_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Project-local .loopplane metadata is authoritative; LOOPPLANE_HOME is not used for workflow list.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )

    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before listing or switching workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    current_warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "The workflow registry is project-local truth; do not infer current selection from LOOPPLANE_HOME.",
                ],
                extra={
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_errors = _current_pointer_errors(current_pointer, registry=registry, workflows=workflows)
        if pointer_errors:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_errors,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use a mutating workflow command, not manual edits, when changing the current workflow pointer.",
                ],
                extra={
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        current_warnings.append(
            ".loopplane/current_workflow.json is missing; workflows are listed without a current selection marker."
        )

    workflow_records = [
        _workflow_list_record(project, record, index=index, current_workflow_id=current_workflow_id)
        for index, record in enumerate(workflows)
    ]
    current_found = any(record.get("current") is True for record in workflow_records)
    warnings = current_warnings
    if current_workflow_id and not current_found:
        warnings.append(
            f"current_workflow_id {current_workflow_id!r} was not marked because the registry has no matching record."
        )

    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "listed",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, registry),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "registry_generated_at": registry.get("generated_at"),
        "current_workflow_id": current_workflow_id,
        "current_workflow": current_pointer,
        "current_found": current_found,
        "workflow_count": len(workflow_records),
        "workflows": workflow_records,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow list is read-only; it reads project-local .loopplane/workflow_registry.json "
            "and does not update .loopplane/current_workflow.json."
        ),
    }


def current_workflow(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to inspect the current workflow.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow current."],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "For v1.5 flat projects, materialize compatibility metadata with loopplane workspace current intentionally first.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow current.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not current_file.is_file():
        return _failure(
            status="missing_current_pointer",
            project=project,
            errors=["Project is missing authoritative .loopplane/current_workflow.json."],
            recovery_actions=[
                "Restore .loopplane/current_workflow.json from a checkpoint or backup.",
                "Use an explicit workflow switch/create/restore command when changing the current pointer.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workspace truth.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow current --project <project>.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before inspecting the current workflow.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    try:
        current_pointer = _load_json_object(current_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_current_pointer",
            project=project,
            errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                "Use an explicit workflow switch/create/restore command when changing the current pointer.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
        current_pointer,
        registry=registry,
        workflows=workflows,
    )
    if pointer_malformed:
        return _failure(
            status="malformed_current_pointer",
            project=project,
            errors=pointer_malformed,
            recovery_actions=[
                "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )
    if pointer_mismatch:
        return _failure(
            status="current_pointer_mismatch",
            project=project,
            errors=pointer_mismatch,
            recovery_actions=[
                "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                "Use a mutating workflow command, not manual edits, when changing the current workflow pointer.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
            },
        )

    current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    selected_index, selected_record = _workflow_record_with_index(workflows, current_workflow_id)
    if selected_record is None:
        return _failure(
            status="current_pointer_mismatch",
            project=project,
            errors=[
                ".loopplane/current_workflow.json points to a workflow_id that is not present in .loopplane/workflow_registry.json."
            ],
            recovery_actions=[
                "Repair .loopplane/current_workflow.json so it references a registered workflow.",
                "Project-local .loopplane/workflow_registry.json is authoritative for workflow membership.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    workflow = _workflow_list_record(
        project,
        selected_record,
        index=selected_index,
        current_workflow_id=current_workflow_id,
    )
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "current",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "registry_generated_at": registry.get("generated_at"),
        "current_workflow_id": current_workflow_id,
        "current_workflow": dict(current_pointer),
        "selection_reason": str(current_pointer.get("selection_reason") or ""),
        "updated_at": str(current_pointer.get("updated_at") or ""),
        "updated_by": str(current_pointer.get("updated_by") or ""),
        "workflow_count": len(workflows),
        "workflow": workflow,
        "errors": [],
        "warnings": [],
        "mutation_boundary": (
            "workflow current is read-only; it reads project-local .loopplane/current_workflow.json "
            "and .loopplane/workflow_registry.json without updating the current pointer."
        ),
    }


def show_workflow(project_root: Path | str, workflow_id: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_id = str(workflow_id or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to inspect a workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow show."],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if WORKFLOW_ID_RE.match(workflow_id) is None:
        return _failure(
            status="invalid_workflow_id",
            project=project,
            errors=[f"workflow_id must match {WORKFLOW_ID_RE.pattern}: {workflow_id!r}"],
            recovery_actions=[
                "Use a workflow_id listed by loopplane workflow list --project <project>.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "For v1.5 flat projects, materialize compatibility metadata with loopplane workspace current intentionally first.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow show.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workspace truth.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow show <workflow_id> --project <project>.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before inspecting workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Viewing workflow history is read-only; use workflow switch/create/restore when changing the pointer.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use a mutating workflow command, not manual edits, when changing the current workflow pointer.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow history is shown without a current selection marker."
        )

    selected_index, selected_record = _workflow_record_with_index(workflows, workflow_id)
    if selected_record is None:
        return _failure(
            status="unknown_workflow",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not present in .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Run loopplane workflow list --project <project> to see registered workflow IDs.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
            },
        )

    metadata_errors = _workflow_show_metadata_errors(selected_record, index=selected_index)
    if metadata_errors:
        return _failure(
            status="invalid_workflow_metadata",
            project=project,
            errors=metadata_errors,
            recovery_actions=[
                "Repair the selected .loopplane/workflow_registry.json workflow record or restore it from a checkpoint.",
                "Path fields must be project-relative and stay inside the workspace.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    workflow = _workflow_show_record(
        project,
        selected_record,
        index=selected_index,
        current_workflow_id=current_workflow_id,
    )
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "shown",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "registry_generated_at": registry.get("generated_at"),
        "workflow_id": workflow_id,
        "current_workflow_id": current_workflow_id,
        "current_workflow": dict(current_pointer) if current_pointer else None,
        "workflow_count": len(workflows),
        "workflow": workflow,
        "summary": workflow.get("summary", {}),
        "progress": workflow.get("progress", {}),
        "key_paths": workflow.get("key_paths", {}),
        "read_models": workflow.get("read_models", {}),
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow show is read-only; it reads project-local workflow registry, current pointer, "
            "and available workflow-local read models without updating workflow history."
        ),
    }


def switch_workflow(project_root: Path | str, workflow_id: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_id = str(workflow_id or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to switch workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow switch."],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if WORKFLOW_ID_RE.match(workflow_id) is None:
        return _failure(
            status="invalid_workflow_id",
            project=project,
            errors=[f"workflow_id must match {WORKFLOW_ID_RE.pattern}: {workflow_id!r}"],
            recovery_actions=[
                "Use a workflow_id listed by loopplane workflow list --project <project>.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "Do not use LOOPPLANE_HOME as a substitute for project-local workflow truth.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow switch.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workspace truth.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow switch <workflow_id> --project <project>.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before switching workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Use workflow switch only after current-pointer metadata is parseable.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use workflow switch only after project-local pointer and registry truth agree.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow switch will recreate it only after safety checks pass."
        )

    selected_index, selected_record = _workflow_record_with_index(workflows, workflow_id)
    if selected_record is None:
        return _failure(
            status="unknown_workflow",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not present in .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Run loopplane workflow list --project <project> to see registered workflow IDs.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
            },
        )

    metadata_errors = _workflow_show_metadata_errors(selected_record, index=selected_index)
    if metadata_errors:
        return _failure(
            status="invalid_workflow_metadata",
            project=project,
            errors=metadata_errors,
            recovery_actions=[
                "Repair the selected .loopplane/workflow_registry.json workflow record or restore it from a checkpoint.",
                "Path fields must be project-relative and stay inside the workspace before a workflow can become current.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    mutability_status = _workflow_mutability_status(selected_record)
    if mutability_status is not None:
        workflow = _workflow_show_record(
            project,
            selected_record,
            index=selected_index,
            current_workflow_id=current_workflow_id,
        )
        return _failure(
            status=mutability_status,
            project=project,
            errors=[
                (
                    f"workflow_id {workflow_id!r} is {workflow.get('status')}; "
                    "archived or read-only workflow histories cannot become current through workflow switch."
                )
            ],
            recovery_actions=[
                "Use loopplane workflow restore <workflow_id> before selecting an archived workflow.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating a read-only imported workflow.",
                "Use loopplane workflow show <workflow_id> for dashboard-style read-only inspection.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
                "workflows": [
                    _workflow_show_record(
                        project,
                        record,
                        index=index,
                        current_workflow_id=current_workflow_id,
                    )
                    for index, record in enumerate(workflows)
                ],
                "workflow": workflow,
            },
        )

    safety = _workflow_switch_safety(
        project,
        workflows=workflows,
        selected_record=selected_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return _failure(
            status=str(safety.get("status") or "workflow_switch_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before switching.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
                "Repair stale lock, supervisor, or active-run lease metadata before changing the current pointer.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    if current_workflow_id == workflow_id:
        workflow = _workflow_show_record(
            project,
            selected_record,
            index=selected_index,
            current_workflow_id=current_workflow_id,
        )
        return {
            "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
            "ok": True,
            "status": "already_current",
            "project_root": project.as_posix(),
            "loopplane_dir": loopplane_dir.as_posix(),
            "workspace_id": _workspace_id(workspace, registry),
            "workspace_file": workspace_file.as_posix(),
            "registry_file": registry_file.as_posix(),
            "current_workflow_file": current_file.as_posix(),
            "workflow_id": workflow_id,
            "previous_current_workflow_id": current_workflow_id,
            "current_workflow_id": current_workflow_id,
            "selection_reason": str(current_pointer.get("selection_reason") or "") if current_pointer else "",
            "updated_at": str(current_pointer.get("updated_at") or "") if current_pointer else "",
            "updated_by": str(current_pointer.get("updated_by") or "") if current_pointer else "",
            "current_workflow": dict(current_pointer) if current_pointer else None,
            "workflow": workflow,
            "safety": safety,
            "errors": [],
            "warnings": warnings,
            "mutation_boundary": (
                "workflow switch is an explicit CLI control operation. Dashboard-only visual selection "
                "must not update .loopplane/current_workflow.json."
            ),
        }

    try:
        current_update = set_current_workflow(
            project,
            workflow_id,
            selection_reason=WORKFLOW_SWITCH_SELECTION_REASON,
            updated_by=WORKFLOW_SWITCH_UPDATED_BY,
        )
    except WorkflowLifecycleError as error:
        return _failure(
            status="active_running_policy_conflict",
            project=project,
            errors=[str(error)],
            recovery_actions=[
                "Resolve active/running workflow conflicts in .loopplane/workflow_registry.json before switching.",
                "Only one active-running workflow is allowed per workspace by default.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    updated_registry = _load_json_object(registry_file)
    updated_current = _load_json_object(current_file)
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    updated_index, updated_record = _workflow_record_with_index(updated_workflows, workflow_id)
    workflow = _workflow_show_record(
        project,
        updated_record or selected_record,
        index=updated_index if updated_index >= 0 else selected_index,
        current_workflow_id=workflow_id,
    )
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "switched",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "workflow_id": workflow_id,
        "previous_current_workflow_id": current_workflow_id,
        "current_workflow_id": workflow_id,
        "selection_reason": WORKFLOW_SWITCH_SELECTION_REASON,
        "updated_at": str(current_update.get("updated_at") or updated_current.get("updated_at") or ""),
        "updated_by": WORKFLOW_SWITCH_UPDATED_BY,
        "current_update": current_update,
        "current_workflow": updated_current,
        "workflow": workflow,
        "safety": safety,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow switch is an explicit CLI control operation. Dashboard-only visual selection "
            "must not update .loopplane/current_workflow.json."
        ),
    }


def create_workflow(project_root: Path | str, brief: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    brief_text = str(brief or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not brief_text:
        return _failure(
            status="invalid_brief",
            project=project,
            errors=["--brief must be a non-empty string."],
            recovery_actions=["Pass loopplane workflow create --brief <text> with the new workflow request."],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory and initialize LoopPlane first.",
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass an existing LoopPlane project directory to loopplane workflow create."],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "Do not use LOOPPLANE_HOME as a substitute for project-local workflow truth.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Materialize compatibility metadata intentionally before creating more workflow history.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Project-local workspace truth must be valid before creating workflow history.",
            ],
            extra=_file_context(workspace_file, registry_file, current_file),
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow create --brief <text> --project <project>.",
            ],
            extra={**_file_context(workspace_file, registry_file, current_file), "workspace_id": str(workspace.get("workspace_id") or "")},
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before creating workflow history.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Use workflow create only after current-pointer metadata is parseable.",
                ],
                extra={
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use workflow create only after project-local pointer and registry truth agree.",
                ],
                extra={
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow create will create a new pointer only after safety checks pass."
        )

    workflow_name = _workflow_name_from_brief(brief_text)
    name_source = _summary_from_brief(brief_text)
    name_was_truncated = workflow_name != name_source
    if name_was_truncated:
        warnings.append(
            "Workflow display name was shortened from the brief for registry/dashboard readability; full brief is preserved in the workflow brief file."
        )

    created_at = utc_timestamp()
    workflow_id, workflow_root = _new_workflow_identity(project, workflows, created_at)
    provisional_record = {
        "workflow_id": workflow_id,
        "name": workflow_name,
        "status": "draft",
        "workflow_root": workflow_root,
        "runtime_dir": f"{workflow_root}/runtime",
    }
    safety = _workflow_create_safety(
        project,
        workflows=workflows,
        new_record=provisional_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return _failure(
            status=str(safety.get("status") or "workflow_create_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before creating a new current workflow.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
                "Repair stale lock, supervisor, or active-run lease metadata before changing workflow history.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": None,
                "proposed_workflow_id": workflow_id,
                "workflow_root": workflow_root,
                "safety": safety,
            },
        )

    try:
        version_control = plan_local_repository_initialization(project)
        files_result = materialize_canonical_workflow_files(
            project,
            brief_text,
            workflow_id=workflow_id,
            created_at=created_at,
            version_control=version_control,
        )
        paths = files_result["paths"]
        lifecycle = create_workflow_record(
            project,
            workflow_id=workflow_id,
            name=workflow_name,
            workflow_root=workflow_root,
            status="draft",
            make_current=True,
            selection_reason=WORKFLOW_CREATE_SELECTION_REASON,
            updated_by=WORKFLOW_CREATE_UPDATED_BY,
            created_at=created_at,
            summary={
                "one_line": _summary_from_brief(brief_text),
                "tasks_total": 0,
                "tasks_completed": 0,
                "tasks_blocked": 0,
            },
            path_values=dict(paths.values),
        )
        projection_sync = sync_active_workflow_projections(
            project,
            files_result["workflow_config"],
            paths,
            reason="workflow_create",
        )
    except InitConflictError as error:
        return _failure(
            status="workflow_create_conflict",
            project=project,
            errors=list(error.conflicts),
            recovery_actions=[
                "Choose a new workflow ID/root or inspect the existing path before retrying.",
                "Workflow create never writes into an existing workflow root.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": workflow_id,
                "workflow_root": workflow_root,
            },
        )
    except (OSError, WorkflowLifecycleError, ValueError) as error:
        return _failure(
            status="workflow_create_failed",
            project=project,
            errors=[str(error)],
            recovery_actions=[
                "Inspect project-local .loopplane metadata and filesystem permissions.",
                "Restore from a checkpoint if a partial workflow root was created.",
            ],
            extra={
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": workflow_id,
                "workflow_root": workflow_root,
            },
        )

    updated_registry = _load_json_object(registry_file)
    updated_current = _load_json_object(current_file)
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    selected_index, selected_record = _workflow_record_with_index(updated_workflows, workflow_id)
    workflow = _workflow_show_record(
        project,
        selected_record or lifecycle["record"],
        index=selected_index if selected_index >= 0 else len(updated_workflows) - 1,
        current_workflow_id=workflow_id,
    )
    projection_warnings = projection_sync.get("warnings") if isinstance(projection_sync, Mapping) else None
    if isinstance(projection_warnings, Sequence) and not isinstance(projection_warnings, (str, bytes)):
        warnings.extend(str(warning) for warning in projection_warnings)
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "created",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "workflow_id": workflow_id,
        "workflow_root": workflow_root,
        "workflow_config_file": files_result["workflow_config_file"],
        "previous_current_workflow_id": current_workflow_id,
        "current_workflow_id": workflow_id,
        "selection_reason": WORKFLOW_CREATE_SELECTION_REASON,
        "updated_at": str(lifecycle.get("updated_at") or updated_current.get("updated_at") or ""),
        "updated_by": WORKFLOW_CREATE_UPDATED_BY,
        "workflow_name": workflow_name,
        "workflow_name_source_excerpt": name_source,
        "workflow_name_was_truncated": name_was_truncated,
        "workflow_name_limit": 96,
        "created": list(files_result.get("created") or []),
        "preserved": list(files_result.get("preserved") or []),
        "current_update": lifecycle.get("current_update"),
        "current_workflow": updated_current,
        "workflow_count": len(updated_workflows),
        "workflow": workflow,
        "safety": safety,
        "active_projection": projection_sync,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow create allocates a new .loopplane/workflows/<workflow_id>/ root, appends "
            "to .loopplane/workflow_registry.json, and updates .loopplane/current_workflow.json only "
            "after runtime safety checks pass."
        ),
    }


def archive_workflow(project_root: Path | str, workflow_id: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_id = str(workflow_id or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to archive workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow archive."],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if WORKFLOW_ID_RE.match(workflow_id) is None:
        return _failure(
            status="invalid_workflow_id",
            project=project,
            errors=[f"workflow_id must match {WORKFLOW_ID_RE.pattern}: {workflow_id!r}"],
            recovery_actions=[
                "Use a workflow_id listed by loopplane workflow list --project <project>.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "Do not use LOOPPLANE_HOME as a substitute for project-local workflow truth.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow archive.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Project-local workspace truth must be valid before archiving workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow archive <workflow_id> --project <project>.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before archiving workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Use workflow archive only after current-pointer metadata is parseable.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use workflow archive only after project-local pointer and registry truth agree.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow archive will not create or move the current pointer."
        )

    selected_index, selected_record = _workflow_record_with_index(workflows, workflow_id)
    if selected_record is None:
        return _failure(
            status="unknown_workflow",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not present in .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Run loopplane workflow list --project <project> to see registered workflow IDs.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
            },
        )

    metadata_errors = _workflow_show_metadata_errors(selected_record, index=selected_index)
    if metadata_errors:
        return _failure(
            status="invalid_workflow_metadata",
            project=project,
            errors=metadata_errors,
            recovery_actions=[
                "Repair the selected .loopplane/workflow_registry.json workflow record or restore it from a checkpoint.",
                "Path fields must be project-relative and stay inside the workspace before archival.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    immutable_status = _workflow_mutability_status(selected_record)
    if immutable_status is not None:
        workflow = _workflow_show_record(
            project,
            selected_record,
            index=selected_index,
            current_workflow_id=current_workflow_id,
        )
        if immutable_status == "archived_workflow":
            return _failure(
                status="already_archived_workflow",
                project=project,
                errors=[f"workflow_id {workflow_id!r} is already archived."],
                recovery_actions=[
                    "Use loopplane workflow show <workflow_id> to inspect archived history.",
                    "Use loopplane workflow restore <workflow_id> when restore support is available and an archived workflow must become active again.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": current_workflow_id,
                    "workflow": workflow,
                },
            )
        return _failure(
            status=immutable_status,
            project=project,
            errors=[
                (
                    f"workflow_id {workflow_id!r} is read-only imported; "
                    "read-only workflow histories cannot be archived in place."
                )
            ],
            recovery_actions=[
                "Use loopplane workflow show <workflow_id> for read-only inspection.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating imported history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow": workflow,
            },
        )

    safety = _workflow_archive_safety(
        project,
        workflows=workflows,
        selected_record=selected_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return _failure(
            status=str(safety.get("status") or "workflow_archive_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before archiving workflow history.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
                "Repair stale lock, supervisor, or active-run lease metadata before changing workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    previous_status = str(selected_record.get("status") or "")
    try:
        lifecycle = archive_workflow_record(
            project,
            workflow_id,
            updated_by=WORKFLOW_ARCHIVE_UPDATED_BY,
        )
    except (OSError, WorkflowLifecycleError, ValueError) as error:
        return _failure(
            status="workflow_archive_failed",
            project=project,
            errors=[str(error)],
            recovery_actions=[
                "Inspect project-local .loopplane metadata and filesystem permissions.",
                "Restore .loopplane/workflow_registry.json from a checkpoint if archival partially wrote state.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    updated_registry = _load_json_object(registry_file)
    updated_current = _load_json_object(current_file) if current_file.exists() else None
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    updated_index, updated_record = _workflow_record_with_index(updated_workflows, workflow_id)
    workflow = _workflow_show_record(
        project,
        updated_record or lifecycle["record"],
        index=updated_index if updated_index >= 0 else selected_index,
        current_workflow_id=current_workflow_id,
    )
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "archived",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "workflow_id": workflow_id,
        "previous_workflow_status": previous_status,
        "current_workflow_id": current_workflow_id,
        "previous_current_workflow_id": current_workflow_id,
        "current_pointer_updated": False,
        "updated_at": str(lifecycle.get("updated_at") or workflow.get("last_seen_at") or ""),
        "updated_by": WORKFLOW_ARCHIVE_UPDATED_BY,
        "lifecycle_update": lifecycle,
        "current_workflow": updated_current,
        "workflow_count": len(updated_workflows),
        "workflow": workflow,
        "safety": safety,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow archive is an explicit registry lifecycle operation. It marks only the selected "
            "project-local .loopplane/workflow_registry.json record archived, preserves workflow roots and "
            "prior history, and does not update .loopplane/current_workflow.json."
        ),
    }


def restore_workflow(project_root: Path | str, workflow_id: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_id = str(workflow_id or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to restore workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow restore."],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if WORKFLOW_ID_RE.match(workflow_id) is None:
        return _failure(
            status="invalid_workflow_id",
            project=project,
            errors=[f"workflow_id must match {WORKFLOW_ID_RE.pattern}: {workflow_id!r}"],
            recovery_actions=[
                "Use a workflow_id listed by loopplane workflow list --project <project>.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "Do not use LOOPPLANE_HOME as a substitute for project-local workflow truth.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow restore.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Project-local workspace truth must be valid before restoring workflow history.",
            ],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow restore <workflow_id> --project <project>.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before restoring workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Use workflow restore only after current-pointer metadata is parseable.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use workflow restore only after project-local pointer and registry truth agree.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow restore will recreate it only after safety checks pass."
        )

    selected_index, selected_record = _workflow_record_with_index(workflows, workflow_id)
    if selected_record is None:
        return _failure(
            status="unknown_workflow",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not present in .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Run loopplane workflow list --project <project> to see registered workflow IDs.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
            },
        )

    metadata_errors = _workflow_show_metadata_errors(selected_record, index=selected_index)
    if metadata_errors:
        return _failure(
            status="invalid_workflow_metadata",
            project=project,
            errors=metadata_errors,
            recovery_actions=[
                "Repair the selected .loopplane/workflow_registry.json workflow record or restore it from a checkpoint.",
                "Path fields must be project-relative and stay inside the workspace before restore.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    workflow = _workflow_show_record(
        project,
        selected_record,
        index=selected_index,
        current_workflow_id=current_workflow_id,
    )
    selected_status = str(selected_record.get("status") or "").strip()
    selected_archived = bool(selected_record.get("archived")) or selected_status == "archived"
    selected_read_only = bool(selected_record.get("read_only")) or selected_status == "read_only_imported"
    if selected_read_only:
        return _failure(
            status="read_only_workflow",
            project=project,
            errors=[
                (
                    f"workflow_id {workflow_id!r} is read-only imported; "
                    "read-only workflow histories must be forked before mutation."
                )
            ],
            recovery_actions=[
                "Use loopplane workflow show <workflow_id> for read-only inspection.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating imported history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow": workflow,
            },
        )
    if not selected_archived:
        return _failure(
            status="workflow_not_archived",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not archived and cannot be restored."],
            recovery_actions=[
                "Use loopplane workflow switch <workflow_id> to select a mutable non-archived workflow.",
                "Use loopplane workflow archive <workflow_id> only when intentionally archiving workflow history.",
                "Use loopplane workflow fork <workflow_id> --name <name> before mutating read-only imported history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow": workflow,
            },
        )

    safety = _workflow_restore_safety(
        project,
        workflows=workflows,
        selected_record=selected_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return _failure(
            status=str(safety.get("status") or "workflow_restore_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before restoring workflow history.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
                "Repair stale lock, supervisor, or active-run lease metadata before changing workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    previous_status = selected_status
    try:
        lifecycle = restore_workflow_record(
            project,
            workflow_id,
            make_current=True,
            selection_reason=WORKFLOW_RESTORE_SELECTION_REASON,
            updated_by=WORKFLOW_RESTORE_UPDATED_BY,
        )
    except (OSError, WorkflowLifecycleError, ValueError) as error:
        return _failure(
            status="workflow_restore_failed",
            project=project,
            errors=[str(error)],
            recovery_actions=[
                "Inspect project-local .loopplane metadata and filesystem permissions.",
                "Restore .loopplane/workflow_registry.json and .loopplane/current_workflow.json from a checkpoint if restore partially wrote state.",
            ],
            extra={
                "workflow_id": workflow_id,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    updated_registry = _load_json_object(registry_file)
    updated_current = _load_json_object(current_file)
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    updated_index, updated_record = _workflow_record_with_index(updated_workflows, workflow_id)
    workflow = _workflow_show_record(
        project,
        updated_record or lifecycle["record"],
        index=updated_index if updated_index >= 0 else selected_index,
        current_workflow_id=workflow_id,
    )
    current_update = lifecycle.get("current_update") if isinstance(lifecycle.get("current_update"), Mapping) else {}
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "restored",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "workflow_id": workflow_id,
        "previous_workflow_status": previous_status,
        "previous_current_workflow_id": current_workflow_id,
        "current_workflow_id": workflow_id,
        "current_pointer_updated": True,
        "selection_reason": WORKFLOW_RESTORE_SELECTION_REASON,
        "updated_at": str(lifecycle.get("updated_at") or current_update.get("updated_at") or workflow.get("last_seen_at") or ""),
        "updated_by": WORKFLOW_RESTORE_UPDATED_BY,
        "lifecycle_update": lifecycle,
        "current_update": current_update,
        "current_workflow": updated_current,
        "workflow_count": len(updated_workflows),
        "workflow": workflow,
        "safety": safety,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow restore is an explicit registry lifecycle operation. It clears only the selected "
            "archived flag, marks that workflow active, preserves workflow roots and prior history, "
            "and updates .loopplane/current_workflow.json only after runtime safety checks pass."
        ),
    }


def fork_workflow(project_root: Path | str, workflow_id: str, name: str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    workflow_id = str(workflow_id or "").strip()
    fork_name = str(name or "").strip()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"

    if not fork_name:
        return _failure(
            status="invalid_name",
            project=project,
            errors=["--name must be a non-empty string."],
            recovery_actions=["Pass loopplane workflow fork <workflow_id> --name <new-attempt-name>."],
            extra={"workflow_id": workflow_id, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.exists():
        return _failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=[
                "Create the project directory first.",
                "Pass --project <existing-loopplane-project> to fork workflow history.",
            ],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not project.is_dir():
        return _failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass a LoopPlane project directory to loopplane workflow fork."],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )
    if WORKFLOW_ID_RE.match(workflow_id) is None:
        return _failure(
            status="invalid_workflow_id",
            project=project,
            errors=[f"workflow_id must match {WORKFLOW_ID_RE.pattern}: {workflow_id!r}"],
            recovery_actions=[
                "Use a workflow_id listed by loopplane workflow list --project <project>.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return _failure(
            status="missing_workspace",
            project=project,
            errors=[
                (
                    "No project-local .loopplane workspace identity was found. "
                    "Run loopplane init --project <project> --brief <brief> first."
                )
            ],
            recovery_actions=[
                "Run loopplane init --project <project> --brief <brief> for a new workspace.",
                "Run from an existing LoopPlane project or pass --project <project>.",
                "Do not use LOOPPLANE_HOME as a substitute for project-local workflow truth.",
            ],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )
    if not registry_file.is_file():
        return _failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Restore .loopplane/workflow_registry.json from a checkpoint or backup.",
                "Project-local .loopplane files are authoritative; LOOPPLANE_HOME is not used for workflow fork.",
            ],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )

    try:
        workspace = _load_json_object(workspace_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[f"Unable to read .loopplane/workspace.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workspace.json or restore it from a checkpoint.",
                "Project-local workspace truth must be valid before forking workflow history.",
            ],
            extra={"workflow_id": workflow_id, "name": fork_name, **_file_context(workspace_file, registry_file, current_file)},
        )
    workspace_errors = _workspace_errors(workspace)
    if workspace_errors:
        return _failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=[
                "Repair .loopplane/workspace.json so it matches the v1.6 workspace identity schema.",
                "Then rerun loopplane workflow fork <workflow_id> --name <name> --project <project>.",
            ],
            extra={
                "workflow_id": workflow_id,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )

    try:
        registry = _load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=[f"Unable to read .loopplane/workflow_registry.json: {error}"],
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json or restore it from a checkpoint.",
                "Do not rely on LOOPPLANE_HOME to replace project-local workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": str(workspace.get("workspace_id") or ""),
            },
        )
    registry_errors = _registry_errors(registry, workspace=workspace)
    if registry_errors:
        return _failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=[
                "Repair .loopplane/workflow_registry.json before forking workflow histories.",
                "Keep workflow IDs, statuses, and workflow_root paths in the v1.6 registry schema.",
            ],
            extra={
                "workflow_id": workflow_id,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
            },
        )

    workflows = [dict(record) for record in registry.get("workflows", [])]
    current_pointer: dict[str, Any] | None = None
    current_workflow_id: str | None = None
    warnings: list[str] = []
    if current_file.exists():
        try:
            current_pointer = _load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json or restore it from a checkpoint.",
                    "Use workflow fork only after current-pointer metadata is parseable.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    "name": fork_name,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        pointer_malformed, pointer_mismatch = _current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed:
            return _failure(
                status="malformed_current_pointer",
                project=project,
                errors=pointer_malformed,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it uses the v1.6 current pointer schema.",
                    "Do not edit .loopplane/current_workflow.json by hand unless restoring known-good metadata.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    "name": fork_name,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                },
            )
        if pointer_mismatch:
            return _failure(
                status="current_pointer_mismatch",
                project=project,
                errors=pointer_mismatch,
                recovery_actions=[
                    "Repair .loopplane/current_workflow.json so it references a workflow in .loopplane/workflow_registry.json.",
                    "Use workflow fork only after project-local pointer and registry truth agree.",
                ],
                extra={
                    "workflow_id": workflow_id,
                    "name": fork_name,
                    **_file_context(workspace_file, registry_file, current_file),
                    "workspace_id": _workspace_id(workspace, registry),
                    "current_workflow_id": str(current_pointer.get("current_workflow_id") or ""),
                },
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(
            ".loopplane/current_workflow.json is missing; workflow fork will create it only after safety checks pass."
        )

    selected_index, selected_record = _workflow_record_with_index(workflows, workflow_id)
    if selected_record is None:
        return _failure(
            status="unknown_workflow",
            project=project,
            errors=[f"workflow_id {workflow_id!r} is not present in .loopplane/workflow_registry.json."],
            recovery_actions=[
                "Run loopplane workflow list --project <project> to see registered workflow IDs.",
                "Workflow IDs are resolved only through project-local .loopplane/workflow_registry.json.",
            ],
            extra={
                "workflow_id": workflow_id,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "workflow_count": len(workflows),
            },
        )

    metadata_errors = _workflow_show_metadata_errors(selected_record, index=selected_index)
    if metadata_errors:
        return _failure(
            status="invalid_workflow_metadata",
            project=project,
            errors=metadata_errors,
            recovery_actions=[
                "Repair the selected .loopplane/workflow_registry.json workflow record or restore it from a checkpoint.",
                "Path fields must be project-relative and stay inside the workspace before fork.",
            ],
            extra={
                "workflow_id": workflow_id,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )

    created_at = utc_timestamp()
    forked_workflow_id, workflow_root = _new_workflow_identity(project, workflows, created_at)
    source_workflow = _workflow_show_record(
        project,
        selected_record,
        index=selected_index,
        current_workflow_id=current_workflow_id,
    )
    provisional_record = {
        "workflow_id": forked_workflow_id,
        "name": fork_name,
        "status": "forked",
        "workflow_root": workflow_root,
        "runtime_dir": f"{workflow_root}/runtime",
    }
    safety = _workflow_fork_safety(
        project,
        workflows=workflows,
        selected_record=selected_record,
        new_record=provisional_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return _failure(
            status=str(safety.get("status") or "workflow_fork_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before forking workflow history.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
                "Repair stale lock, supervisor, or active-run lease metadata before changing workflow history.",
            ],
            extra={
                "workflow_id": workflow_id,
                "source_workflow_id": workflow_id,
                "forked_workflow_id": forked_workflow_id,
                "workflow_root": workflow_root,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "source_workflow": source_workflow,
                "safety": safety,
            },
        )

    try:
        version_control = plan_local_repository_initialization(project)
        files_result = materialize_canonical_workflow_files(
            project,
            _fork_brief_from_source(selected_record, fork_name=fork_name, source_workflow_id=workflow_id),
            workflow_id=forked_workflow_id,
            created_at=created_at,
            version_control=version_control,
        )
        paths = files_result["paths"]
        lifecycle = fork_workflow_record(
            project,
            workflow_id,
            new_workflow_id=forked_workflow_id,
            name=fork_name,
            workflow_root=workflow_root,
            make_current=True,
            selection_reason=WORKFLOW_FORK_SELECTION_REASON,
            updated_by=WORKFLOW_FORK_UPDATED_BY,
            created_at=created_at,
            path_values=dict(paths.values),
        )
        projection_sync = sync_active_workflow_projections(
            project,
            files_result["workflow_config"],
            paths,
            reason="workflow_fork",
        )
    except InitConflictError as error:
        return _failure(
            status="workflow_fork_conflict",
            project=project,
            errors=list(error.conflicts),
            recovery_actions=[
                "Choose a new workflow ID/root or inspect the existing path before retrying.",
                "Workflow fork never writes into an existing workflow root.",
            ],
            extra={
                "workflow_id": workflow_id,
                "source_workflow_id": workflow_id,
                "forked_workflow_id": forked_workflow_id,
                "workflow_root": workflow_root,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
            },
        )
    except (OSError, WorkflowLifecycleError, ValueError) as error:
        return _failure(
            status="workflow_fork_failed",
            project=project,
            errors=[str(error)],
            recovery_actions=[
                "Inspect project-local .loopplane metadata and filesystem permissions.",
                "Restore from a checkpoint if a partial fork workflow root was created.",
            ],
            extra={
                "workflow_id": workflow_id,
                "source_workflow_id": workflow_id,
                "forked_workflow_id": forked_workflow_id,
                "workflow_root": workflow_root,
                "name": fork_name,
                **_file_context(workspace_file, registry_file, current_file),
                "workspace_id": _workspace_id(workspace, registry),
                "current_workflow_id": current_workflow_id,
                "safety": safety,
            },
        )

    updated_registry = _load_json_object(registry_file)
    updated_current = _load_json_object(current_file)
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    updated_index, updated_record = _workflow_record_with_index(updated_workflows, forked_workflow_id)
    source_index, updated_source_record = _workflow_record_with_index(updated_workflows, workflow_id)
    workflow = _workflow_show_record(
        project,
        updated_record or lifecycle["record"],
        index=updated_index if updated_index >= 0 else len(updated_workflows) - 1,
        current_workflow_id=forked_workflow_id,
    )
    updated_source_workflow = _workflow_show_record(
        project,
        updated_source_record or selected_record,
        index=source_index if source_index >= 0 else selected_index,
        current_workflow_id=forked_workflow_id,
    )
    current_update = lifecycle.get("current_update") if isinstance(lifecycle.get("current_update"), Mapping) else {}
    projection_warnings = projection_sync.get("warnings") if isinstance(projection_sync, Mapping) else None
    if isinstance(projection_warnings, Sequence) and not isinstance(projection_warnings, (str, bytes)):
        warnings.extend(str(warning) for warning in projection_warnings)
    return {
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": True,
        "status": "forked",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": _workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "source_workflow_id": workflow_id,
        "workflow_id": forked_workflow_id,
        "forked_workflow_id": forked_workflow_id,
        "workflow_root": workflow_root,
        "workflow_config_file": files_result["workflow_config_file"],
        "name": fork_name,
        "previous_current_workflow_id": current_workflow_id,
        "current_workflow_id": forked_workflow_id,
        "current_pointer_updated": True,
        "selection_reason": WORKFLOW_FORK_SELECTION_REASON,
        "updated_at": str(lifecycle.get("updated_at") or current_update.get("updated_at") or workflow.get("last_seen_at") or ""),
        "updated_by": WORKFLOW_FORK_UPDATED_BY,
        "created": list(files_result.get("created") or []),
        "preserved": list(files_result.get("preserved") or []),
        "lifecycle_update": lifecycle,
        "current_update": current_update,
        "current_workflow": updated_current,
        "workflow_count": len(updated_workflows),
        "workflow": workflow,
        "source_workflow": updated_source_workflow,
        "safety": safety,
        "active_projection": projection_sync,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow fork is an explicit registry lifecycle operation. It creates a new "
            ".loopplane/workflows/<workflow_id>/ root, records source lineage without mutating "
            "the source workflow history, and updates .loopplane/current_workflow.json only "
            "after runtime safety checks pass."
        ),
    }


def workflow_list_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_current_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_show_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_switch_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_create_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_archive_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_restore_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def workflow_fork_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") is True else EXIT_INVALID_CONFIG


def format_workflow_list_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow list: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"registry_file: {result.get('registry_file') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        lines.append(f"workflow_count: {result.get('workflow_count')}")
        workflows = result.get("workflows")
        if isinstance(workflows, Sequence) and workflows and not isinstance(workflows, (str, bytes)):
            lines.append("workflows:")
            for workflow in workflows:
                if not isinstance(workflow, Mapping):
                    continue
                marker = "*" if workflow.get("current") is True else "-"
                labels = workflow.get("labels")
                label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
                summary = _summary_one_line(workflow.get("summary"))
                lines.append(
                    "  "
                    f"{marker} {workflow.get('workflow_id') or 'unknown'} "
                    f"name={workflow.get('name') or 'unknown'} "
                    f"status={workflow.get('status') or 'unknown'} "
                    f"root={workflow.get('workflow_root') or 'unknown'} "
                    f"created_at={workflow.get('created_at') or 'unknown'} "
                    f"last_seen_at={workflow.get('last_seen_at') or 'unknown'} "
                    f"labels={label_text or 'none'}"
                )
                if summary:
                    lines.append(f"    summary: {summary}")
        else:
            lines.append("workflows: none")
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


def format_workflow_current_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow current: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"selection_reason: {result.get('selection_reason') or 'unknown'}",
                    f"updated_at: {result.get('updated_at') or 'unknown'}",
                    f"updated_by: {result.get('updated_by') or 'unknown'}",
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"workflow_root: {workflow.get('workflow_root') or 'unknown'}",
                    f"workflow_root_path: {workflow.get('workflow_root_path') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
            summary = _summary_one_line(workflow.get("summary"))
            if summary:
                lines.append(f"summary: {summary}")
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


def format_workflow_show_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow show: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"workflow_root: {workflow.get('workflow_root') or 'unknown'}",
                    f"workflow_root_path: {workflow.get('workflow_root_path') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"created_at: {workflow.get('created_at') or 'unknown'}",
                    f"last_seen_at: {workflow.get('last_seen_at') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
            summary = _summary_one_line(workflow.get("summary"))
            if summary:
                lines.append(f"summary: {summary}")
            progress = workflow.get("progress")
            if isinstance(progress, Mapping) and progress:
                lines.append("progress:")
                for key in sorted(progress):
                    lines.append(f"  {key}: {progress[key]}")
            key_paths = workflow.get("key_paths")
            if isinstance(key_paths, Mapping) and key_paths:
                lines.append("key_paths:")
                for name, info in key_paths.items():
                    if not isinstance(info, Mapping):
                        continue
                    exists = str(bool(info.get("exists"))).lower()
                    lines.append(f"  {name}: {info.get('relative') or 'unknown'} exists={exists}")
            read_models = workflow.get("read_models")
            if isinstance(read_models, Mapping):
                freshness = read_models.get("freshness")
                if isinstance(freshness, Mapping):
                    lines.append(f"read_model_freshness: {freshness.get('status') or 'unknown'}")
                    summary_text = str(freshness.get("summary") or "").strip()
                    if summary_text:
                        lines.append(f"read_model_freshness_summary: {summary_text}")
                files = read_models.get("files")
                if isinstance(files, Mapping) and files:
                    lines.append("read_model_files:")
                    for filename, info in files.items():
                        if not isinstance(info, Mapping):
                            continue
                        exists = str(bool(info.get("exists"))).lower()
                        generated_at = info.get("generated_at") or "unknown"
                        lines.append(f"  {filename}: exists={exists} generated_at={generated_at}")
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


def format_workflow_switch_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow switch: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"previous_current_workflow_id: {result.get('previous_current_workflow_id') or 'none'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        lines.extend(
            [
                f"selection_reason: {result.get('selection_reason') or 'unknown'}",
                f"updated_at: {result.get('updated_at') or 'unknown'}",
                f"updated_by: {result.get('updated_by') or 'unknown'}",
            ]
        )
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"workflow_root: {workflow.get('workflow_root') or 'unknown'}",
                    f"workflow_root_path: {workflow.get('workflow_root_path') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
        safety = result.get("safety")
        if isinstance(safety, Mapping):
            checked = safety.get("checked_workflows")
            checked_count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else 0
            lines.append(f"safety_checked_workflows: {checked_count}")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    safety = result.get("safety")
    if isinstance(safety, Mapping):
        blockers = safety.get("blockers")
        if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
            lines.append("safety_blockers:")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code") or "unknown"
                    message = blocker.get("message") or ""
                    path = blocker.get("path") or blocker.get("runtime_dir") or ""
                    suffix = f" path={path}" if path else ""
                    lines.append(f"  - {code}: {message}{suffix}")
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


def format_workflow_create_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow create: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"workflow_root: {result.get('workflow_root') or 'unknown'}",
        f"previous_current_workflow_id: {result.get('previous_current_workflow_id') or 'none'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        lines.extend(
            [
                f"selection_reason: {result.get('selection_reason') or 'unknown'}",
                f"updated_at: {result.get('updated_at') or 'unknown'}",
                f"updated_by: {result.get('updated_by') or 'unknown'}",
                f"workflow_config_file: {result.get('workflow_config_file') or 'unknown'}",
                f"workflow_count: {result.get('workflow_count')}",
                f"workflow_name_was_truncated: {str(bool(result.get('workflow_name_was_truncated'))).lower()}",
            ]
        )
        if result.get("workflow_name_was_truncated"):
            lines.append(f"workflow_name_source_excerpt: {result.get('workflow_name_source_excerpt') or 'unknown'}")
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
        created = result.get("created")
        if isinstance(created, Sequence) and not isinstance(created, (str, bytes)):
            lines.append(f"created_files: {len(created)}")
        active_projection = result.get("active_projection")
        if isinstance(active_projection, Mapping):
            lines.append(f"active_projection_status: {active_projection.get('status') or 'unknown'}")
        safety = result.get("safety")
        if isinstance(safety, Mapping):
            checked = safety.get("checked_workflows")
            checked_count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else 0
            lines.append(f"safety_checked_workflows: {checked_count}")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    safety = result.get("safety")
    if isinstance(safety, Mapping):
        blockers = safety.get("blockers")
        if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
            lines.append("safety_blockers:")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code") or "unknown"
                    message = blocker.get("message") or ""
                    path = blocker.get("path") or blocker.get("runtime_dir") or ""
                    suffix = f" path={path}" if path else ""
                    lines.append(f"  - {code}: {message}{suffix}")
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


def format_workflow_archive_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow archive: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"previous_workflow_status: {result.get('previous_workflow_status') or 'unknown'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
        f"current_pointer_updated: {str(bool(result.get('current_pointer_updated'))).lower()}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        lines.extend(
            [
                f"updated_at: {result.get('updated_at') or 'unknown'}",
                f"updated_by: {result.get('updated_by') or 'unknown'}",
                f"workflow_count: {result.get('workflow_count')}",
            ]
        )
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"workflow_root: {workflow.get('workflow_root') or 'unknown'}",
                    f"workflow_root_path: {workflow.get('workflow_root_path') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
        safety = result.get("safety")
        if isinstance(safety, Mapping):
            checked = safety.get("checked_workflows")
            checked_count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else 0
            lines.append(f"safety_checked_workflows: {checked_count}")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    safety = result.get("safety")
    if isinstance(safety, Mapping):
        blockers = safety.get("blockers")
        if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
            lines.append("safety_blockers:")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code") or "unknown"
                    message = blocker.get("message") or ""
                    path = blocker.get("path") or blocker.get("runtime_dir") or ""
                    suffix = f" path={path}" if path else ""
                    lines.append(f"  - {code}: {message}{suffix}")
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


def format_workflow_restore_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow restore: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"previous_workflow_status: {result.get('previous_workflow_status') or 'unknown'}",
        f"previous_current_workflow_id: {result.get('previous_current_workflow_id') or 'none'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
        f"current_pointer_updated: {str(bool(result.get('current_pointer_updated'))).lower()}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        lines.extend(
            [
                f"selection_reason: {result.get('selection_reason') or 'unknown'}",
                f"updated_at: {result.get('updated_at') or 'unknown'}",
                f"updated_by: {result.get('updated_by') or 'unknown'}",
                f"workflow_count: {result.get('workflow_count')}",
            ]
        )
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"workflow_root: {workflow.get('workflow_root') or 'unknown'}",
                    f"workflow_root_path: {workflow.get('workflow_root_path') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
        safety = result.get("safety")
        if isinstance(safety, Mapping):
            checked = safety.get("checked_workflows")
            checked_count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else 0
            lines.append(f"safety_checked_workflows: {checked_count}")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    safety = result.get("safety")
    if isinstance(safety, Mapping):
        blockers = safety.get("blockers")
        if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
            lines.append("safety_blockers:")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code") or "unknown"
                    message = blocker.get("message") or ""
                    path = blocker.get("path") or blocker.get("runtime_dir") or ""
                    suffix = f" path={path}" if path else ""
                    lines.append(f"  - {code}: {message}{suffix}")
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


def format_workflow_fork_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane workflow fork: {result.get('status', 'unknown')}",
        f"project: {result.get('project_root') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"source_workflow_id: {result.get('source_workflow_id') or result.get('workflow_id') or 'unknown'}",
        f"forked_workflow_id: {result.get('forked_workflow_id') or result.get('workflow_id') or 'unknown'}",
        f"workflow_root: {result.get('workflow_root') or 'unknown'}",
        f"previous_current_workflow_id: {result.get('previous_current_workflow_id') or 'none'}",
        f"current_workflow_id: {result.get('current_workflow_id') or 'none'}",
        f"current_pointer_updated: {str(bool(result.get('current_pointer_updated'))).lower()}",
    ]
    if result.get("ok") is True:
        workflow = result.get("workflow")
        source_workflow = result.get("source_workflow")
        lines.extend(
            [
                f"name: {result.get('name') or 'unknown'}",
                f"selection_reason: {result.get('selection_reason') or 'unknown'}",
                f"updated_at: {result.get('updated_at') or 'unknown'}",
                f"updated_by: {result.get('updated_by') or 'unknown'}",
                f"workflow_config_file: {result.get('workflow_config_file') or 'unknown'}",
                f"workflow_count: {result.get('workflow_count')}",
            ]
        )
        if isinstance(workflow, Mapping):
            labels = workflow.get("labels")
            label_text = ",".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
            registry_record = workflow.get("registry_record")
            forked_from = ""
            if isinstance(registry_record, Mapping):
                forked_from = str(registry_record.get("forked_from") or "")
            lines.extend(
                [
                    f"workflow_name: {workflow.get('name') or 'unknown'}",
                    f"workflow_status: {workflow.get('status') or 'unknown'}",
                    f"forked_from: {forked_from or result.get('source_workflow_id') or 'unknown'}",
                    f"layout: {workflow.get('layout') or 'unknown'}",
                    f"archived: {str(bool(workflow.get('archived'))).lower()}",
                    f"read_only: {str(bool(workflow.get('read_only'))).lower()}",
                    f"current: {str(bool(workflow.get('current'))).lower()}",
                    f"labels: {label_text or 'none'}",
                ]
            )
        if isinstance(source_workflow, Mapping):
            lines.extend(
                [
                    f"source_workflow_status: {source_workflow.get('status') or 'unknown'}",
                    f"source_archived: {str(bool(source_workflow.get('archived'))).lower()}",
                    f"source_read_only: {str(bool(source_workflow.get('read_only'))).lower()}",
                ]
            )
        created = result.get("created")
        if isinstance(created, Sequence) and not isinstance(created, (str, bytes)):
            lines.append(f"created_files: {len(created)}")
        active_projection = result.get("active_projection")
        if isinstance(active_projection, Mapping):
            lines.append(f"active_projection_status: {active_projection.get('status') or 'unknown'}")
        safety = result.get("safety")
        if isinstance(safety, Mapping):
            checked = safety.get("checked_workflows")
            checked_count = len(checked) if isinstance(checked, Sequence) and not isinstance(checked, (str, bytes)) else 0
            lines.append(f"safety_checked_workflows: {checked_count}")
        mutation_boundary = result.get("mutation_boundary")
        if mutation_boundary:
            lines.append(f"mutation_boundary: {mutation_boundary}")
    safety = result.get("safety")
    if isinstance(safety, Mapping):
        blockers = safety.get("blockers")
        if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
            lines.append("safety_blockers:")
            for blocker in blockers:
                if isinstance(blocker, Mapping):
                    code = blocker.get("code") or "unknown"
                    message = blocker.get("message") or ""
                    path = blocker.get("path") or blocker.get("runtime_dir") or ""
                    suffix = f" path={path}" if path else ""
                    lines.append(f"  - {code}: {message}{suffix}")
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
        "schema_version": WORKFLOW_COMMAND_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workspace_id": None,
        "current_workflow_id": None,
        "workflow_count": 0,
        "workflows": [],
        "errors": list(errors),
        "warnings": list(warnings),
        "recovery_actions": list(recovery_actions),
        **dict(extra or {}),
    }


def _file_context(workspace_file: Path, registry_file: Path, current_file: Path) -> dict[str, Any]:
    return {
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
    }


def _load_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _load_json_object(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("JSON value must be an object")
    return data


def _workspace_errors(workspace: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if workspace.get("schema_version") != WORKFLOW_COMMAND_SCHEMA_VERSION:
        errors.append(".loopplane/workspace.json schema_version must be 1.6.")
    workspace_id = str(workspace.get("workspace_id") or "").strip()
    if WORKSPACE_ID_RE.match(workspace_id) is None:
        errors.append(f".loopplane/workspace.json workspace_id must match {WORKSPACE_ID_RE.pattern}.")
    try:
        normalize_identity_path_value("project_root", workspace.get("project_root"), allow_parent=False)
    except ValueError as error:
        errors.append(f".loopplane/workspace.json: {error}.")
    if workspace.get("project_root") != ".":
        errors.append(".loopplane/workspace.json project_root must be '.'.")
    if workspace.get("loopplane_dir") != ".loopplane":
        errors.append(".loopplane/workspace.json loopplane_dir must be '.loopplane'.")
    try:
        normalize_identity_path_value("repo_root", workspace.get("repo_root"), allow_parent=True)
    except ValueError as error:
        errors.append(f".loopplane/workspace.json: {error}.")
    if workspace.get("workspace_boundary") != "project_root":
        errors.append(".loopplane/workspace.json workspace_boundary must be 'project_root'.")
    if not isinstance(workspace.get("allow_out_of_boundary_writes"), bool):
        errors.append(".loopplane/workspace.json allow_out_of_boundary_writes must be a boolean.")
    return errors


def _registry_errors(registry: Mapping[str, Any], *, workspace: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if registry.get("schema_version") != WORKFLOW_COMMAND_SCHEMA_VERSION:
        errors.append(".loopplane/workflow_registry.json schema_version must be 1.6.")
    registry_workspace_id = str(registry.get("workspace_id") or "").strip()
    if WORKSPACE_ID_RE.match(registry_workspace_id) is None:
        errors.append(f".loopplane/workflow_registry.json workspace_id must match {WORKSPACE_ID_RE.pattern}.")
    workspace_id = str(workspace.get("workspace_id") or "").strip()
    if workspace_id and registry_workspace_id and workspace_id != registry_workspace_id:
        errors.append(".loopplane/workflow_registry.json workspace_id does not match .loopplane/workspace.json.")
    workflows = registry.get("workflows")
    if not isinstance(workflows, list):
        errors.append(".loopplane/workflow_registry.json workflows must be an array.")
        return errors
    seen: set[str] = set()
    for index, raw_record in enumerate(workflows):
        if not isinstance(raw_record, Mapping):
            errors.append(f".loopplane/workflow_registry.json workflows[{index}] must be an object.")
            continue
        record = raw_record
        workflow_id = str(record.get("workflow_id") or "").strip()
        if WORKFLOW_ID_RE.match(workflow_id) is None:
            errors.append(
                f".loopplane/workflow_registry.json workflows[{index}].workflow_id must match {WORKFLOW_ID_RE.pattern}."
            )
            continue
        if workflow_id in seen:
            errors.append(f".loopplane/workflow_registry.json contains duplicate workflow_id {workflow_id}.")
        seen.add(workflow_id)
        status = str(record.get("status") or "").strip()
        if status not in WORKFLOW_HISTORY_STATUSES:
            errors.append(
                f".loopplane/workflow_registry.json workflow {workflow_id} has unsupported status {status!r}."
            )
        workflow_root = str(record.get("workflow_root") or "").strip()
        if not _valid_project_path(workflow_root):
            errors.append(
                f".loopplane/workflow_registry.json workflow {workflow_id} has invalid workflow_root {workflow_root!r}."
            )
    return errors


def _current_pointer_error_groups(
    current: Mapping[str, Any],
    *,
    registry: Mapping[str, Any],
    workflows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    malformed: list[str] = []
    mismatch: list[str] = []
    if current.get("schema_version") != WORKFLOW_COMMAND_SCHEMA_VERSION:
        malformed.append(".loopplane/current_workflow.json schema_version must be 1.6.")
    registry_workspace_id = str(registry.get("workspace_id") or "").strip()
    pointer_workspace_id = str(current.get("workspace_id") or "").strip()
    if pointer_workspace_id != registry_workspace_id:
        mismatch.append(".loopplane/current_workflow.json workspace_id does not match .loopplane/workflow_registry.json.")
    current_workflow_id = str(current.get("current_workflow_id") or "").strip()
    if WORKFLOW_ID_RE.match(current_workflow_id) is None:
        malformed.append(f".loopplane/current_workflow.json current_workflow_id must match {WORKFLOW_ID_RE.pattern}.")
    workflow_ids = {str(record.get("workflow_id") or "") for record in workflows}
    if current_workflow_id and current_workflow_id not in workflow_ids:
        mismatch.append(
            ".loopplane/current_workflow.json points to a workflow_id that is not present in .loopplane/workflow_registry.json."
        )
    return malformed, mismatch


def _current_pointer_errors(
    current: Mapping[str, Any],
    *,
    registry: Mapping[str, Any],
    workflows: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if current.get("schema_version") != WORKFLOW_COMMAND_SCHEMA_VERSION:
        errors.append(".loopplane/current_workflow.json schema_version must be 1.6.")
    registry_workspace_id = str(registry.get("workspace_id") or "").strip()
    pointer_workspace_id = str(current.get("workspace_id") or "").strip()
    if pointer_workspace_id != registry_workspace_id:
        errors.append(".loopplane/current_workflow.json workspace_id does not match .loopplane/workflow_registry.json.")
    current_workflow_id = str(current.get("current_workflow_id") or "").strip()
    if WORKFLOW_ID_RE.match(current_workflow_id) is None:
        errors.append(f".loopplane/current_workflow.json current_workflow_id must match {WORKFLOW_ID_RE.pattern}.")
    workflow_ids = {str(record.get("workflow_id") or "") for record in workflows}
    if current_workflow_id and current_workflow_id not in workflow_ids:
        errors.append(
            ".loopplane/current_workflow.json points to a workflow_id that is not present in .loopplane/workflow_registry.json."
        )
    return errors


def _workflow_record_with_index(
    workflows: Sequence[Mapping[str, Any]],
    workflow_id: str,
) -> tuple[int, Mapping[str, Any] | None]:
    for index, record in enumerate(workflows):
        if str(record.get("workflow_id") or "") == workflow_id:
            return index, record
    return -1, None


def _workflow_list_record(
    project: Path,
    record: Mapping[str, Any],
    *,
    index: int,
    current_workflow_id: str | None,
) -> dict[str, Any]:
    workflow_id = str(record.get("workflow_id") or "")
    workflow_root = str(record.get("workflow_root") or "")
    resolved_root = _resolve_project_path(project, workflow_root)
    archived = bool(record.get("archived")) or str(record.get("status") or "") == "archived"
    read_only = bool(record.get("read_only")) or str(record.get("status") or "") == "read_only_imported"
    current = bool(current_workflow_id and workflow_id == current_workflow_id)
    labels: list[str] = []
    if current:
        labels.append("current")
    if archived:
        labels.append("archived")
    if read_only:
        labels.append("read_only")
    layout = _workflow_layout(workflow_root)
    return {
        "index": index,
        "workflow_id": workflow_id,
        "name": str(record.get("name") or ""),
        "status": str(record.get("status") or ""),
        "workflow_root": workflow_root,
        "workflow_root_path": resolved_root.as_posix() if resolved_root is not None else "",
        "workflow_root_exists": bool(resolved_root and resolved_root.exists()),
        "layout": layout,
        "created_at": str(record.get("created_at") or ""),
        "last_seen_at": str(record.get("last_seen_at") or ""),
        "plan_file": str(record.get("plan_file") or ""),
        "read_models_dir": str(record.get("read_models_dir") or ""),
        "runtime_dir": str(record.get("runtime_dir") or ""),
        "requests_dir": str(record.get("requests_dir") or ""),
        "completion_marker": str(record.get("completion_marker") or ""),
        "read_only": read_only,
        "archived": archived,
        "current": current,
        "labels": labels,
        "summary": dict(record.get("summary")) if isinstance(record.get("summary"), Mapping) else {},
    }


def _workflow_show_record(
    project: Path,
    record: Mapping[str, Any],
    *,
    index: int,
    current_workflow_id: str | None,
) -> dict[str, Any]:
    base = _workflow_list_record(project, record, index=index, current_workflow_id=current_workflow_id)
    key_paths = _workflow_key_paths(project, record)
    read_models = _read_models_summary(project, key_paths)
    progress = _progress_summary(base.get("summary"), read_models)
    return {
        **base,
        "key_paths": key_paths,
        "read_models": read_models,
        "progress": progress,
        "registry_record": dict(record),
    }


def _workflow_show_metadata_errors(record: Mapping[str, Any], *, index: int) -> list[str]:
    errors: list[str] = []
    workflow_id = str(record.get("workflow_id") or f"workflows[{index}]")
    for field in ("name", "created_at", "last_seen_at"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f".loopplane/workflow_registry.json workflow {workflow_id} field {field} must be a non-empty string.")
    for field in (
        "plan_file",
        "read_models_dir",
        "runtime_dir",
        "requests_dir",
        "completion_marker",
        "workflow_config_file",
    ):
        value = record.get(field)
        if value is not None and str(value).strip() and not _valid_project_path(value):
            errors.append(f".loopplane/workflow_registry.json workflow {workflow_id} field {field} has invalid path {value!r}.")
    summary = record.get("summary")
    if summary is not None and not isinstance(summary, Mapping):
        errors.append(f".loopplane/workflow_registry.json workflow {workflow_id} summary must be an object when present.")
    for field in ("read_only", "archived"):
        value = record.get(field)
        if value is not None and not isinstance(value, bool):
            errors.append(f".loopplane/workflow_registry.json workflow {workflow_id} field {field} must be boolean when present.")
    return errors


def _workflow_key_paths(project: Path, record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    workflow_root = str(record.get("workflow_root") or "")
    normalized_root = workflow_root.rstrip("/")
    flat = normalized_root == ".loopplane"
    if flat:
        defaults = {
            "workflow_root": workflow_root,
            "project_brief_file": "PROJECT_BRIEF.md",
            "plan_file": str(record.get("plan_file") or "PLAN.md"),
            "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
            "config_dir": ".loopplane/config",
            "workflow_config_file": str(record.get("workflow_config_file") or ".loopplane/config/workflow.json"),
            "planning_dir": ".loopplane/planning",
            "runtime_dir": str(record.get("runtime_dir") or ".loopplane/runtime"),
            "read_models_dir": str(record.get("read_models_dir") or ".loopplane/read_models"),
            "requests_dir": str(record.get("requests_dir") or ".loopplane/requests"),
            "results_dir": ".loopplane/results",
            "completion_marker": str(record.get("completion_marker") or ".loopplane/runtime/plan_loop_complete.json"),
        }
    else:
        root = normalized_root
        defaults = {
            "workflow_root": workflow_root,
            "project_brief_file": f"{root}/PROJECT_BRIEF.md",
            "plan_file": str(record.get("plan_file") or f"{root}/PLAN.md"),
            "shared_context_file": f"{root}/SHARED_CONTEXT.md",
            "config_dir": f"{root}/config",
            "workflow_config_file": str(record.get("workflow_config_file") or f"{root}/config/workflow.json"),
            "planning_dir": f"{root}/planning",
            "runtime_dir": str(record.get("runtime_dir") or f"{root}/runtime"),
            "read_models_dir": str(record.get("read_models_dir") or f"{root}/read_models"),
            "requests_dir": str(record.get("requests_dir") or f"{root}/requests"),
            "results_dir": f"{root}/results",
            "completion_marker": str(record.get("completion_marker") or f"{root}/runtime/plan_loop_complete.json"),
        }
    return {name: _path_info(project, relative) for name, relative in defaults.items()}


def _path_info(project: Path, relative: str) -> dict[str, Any]:
    relative = str(relative or "")
    resolved = _resolve_project_path(project, relative)
    return {
        "relative": relative,
        "absolute": resolved.as_posix() if resolved is not None else "",
        "exists": bool(resolved and resolved.exists()),
    }


def _read_models_summary(project: Path, key_paths: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    read_models_info = key_paths.get("read_models_dir")
    read_models_dir = Path(str(read_models_info.get("absolute") or "")) if isinstance(read_models_info, Mapping) else None
    directory_exists = bool(read_models_dir and read_models_dir.exists())
    files: dict[str, dict[str, Any]] = {}
    if read_models_dir is not None:
        for filename in READ_MODEL_SUMMARY_FILES:
            path = read_models_dir / filename
            relative = _relative_to_project(project, path)
            files[filename] = _read_model_file_summary(path, relative=relative)
    return {
        "directory": dict(read_models_info) if isinstance(read_models_info, Mapping) else {},
        "directory_exists": directory_exists,
        "files": files,
        "freshness": _read_model_freshness_summary(files),
    }


def _read_model_file_summary(path: Path, *, relative: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "relative": relative,
        "absolute": path.as_posix(),
        "exists": path.exists(),
        "parseable": None,
    }
    if not path.exists():
        return info
    if path.suffix != ".json":
        info["parseable"] = True
        return info
    try:
        data = _load_json_object(path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        info.update({"parseable": False, "error": str(error)})
        return info
    progress = data.get("progress") if isinstance(data.get("progress"), Mapping) else None
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else None
    info.update(
        {
            "parseable": True,
            "schema_version": data.get("schema_version"),
            "workflow_id": data.get("workflow_id"),
            "generated_at": data.get("generated_at"),
            "status": data.get("status"),
            "phase": data.get("phase"),
            "last_event_seq": data.get("last_event_seq"),
            "source_event_id": data.get("source_event_id"),
            "source_hashes_present": isinstance(data.get("source_hashes"), Mapping),
        }
    )
    if progress is not None:
        info["progress"] = dict(progress)
    if summary is not None:
        info["summary"] = dict(summary)
    return info


def _read_model_freshness_summary(files: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    existing = [name for name, info in files.items() if isinstance(info, Mapping) and info.get("exists")]
    invalid = [name for name, info in files.items() if isinstance(info, Mapping) and info.get("parseable") is False]
    metadata_files = [
        name
        for name, info in files.items()
        if isinstance(info, Mapping)
        and info.get("exists")
        and info.get("parseable") is True
        and ("last_event_seq" in info or "source_event_id" in info or info.get("source_hashes_present") is True)
    ]
    if invalid:
        status = "invalid"
        summary = "One or more read-model files exist but could not be parsed."
    elif not existing:
        status = "missing"
        summary = "No known read-model files are present for this workflow."
    elif metadata_files:
        status = "metadata_available"
        summary = "Read-model freshness metadata is available; rebuild is not performed by workflow show."
    else:
        status = "unknown"
        summary = "Read-model files are present but freshness metadata is incomplete."
    return {
        "status": status,
        "summary": summary,
        "checked_files": list(files.keys()),
        "existing_files": existing,
        "metadata_files": metadata_files,
        "invalid_files": invalid,
    }


def _progress_summary(summary: Any, read_models: Mapping[str, Any]) -> dict[str, Any]:
    files = read_models.get("files") if isinstance(read_models, Mapping) else {}
    if isinstance(files, Mapping):
        workflow_status = files.get("workflow_status.json")
        if isinstance(workflow_status, Mapping) and isinstance(workflow_status.get("progress"), Mapping):
            return dict(workflow_status["progress"])
        plan_index = files.get("plan_index.json")
        if isinstance(plan_index, Mapping) and isinstance(plan_index.get("summary"), Mapping):
            return dict(plan_index["summary"])
    if isinstance(summary, Mapping):
        return {
            "total_tasks": summary.get("tasks_total", 0),
            "completed_tasks": summary.get("tasks_completed", 0),
            "blocked_tasks": summary.get("tasks_blocked", 0),
        }
    return {}


def _relative_to_project(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _workspace_id(workspace: Mapping[str, Any], registry: Mapping[str, Any]) -> str:
    return str(workspace.get("workspace_id") or registry.get("workspace_id") or "")


def _resolve_project_path(project: Path, value: str) -> Path | None:
    if not _valid_project_path(value):
        return None
    return (project / PurePosixPath(value)).resolve()


def _valid_project_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if "\\" in value or value.startswith("/") or value.startswith("~"):
        return False
    path = PurePosixPath(value)
    if path == PurePosixPath(".") or ".." in path.parts:
        return False
    return True


def _workflow_layout(workflow_root: str) -> str:
    normalized = workflow_root.rstrip("/")
    if normalized == ".loopplane":
        return "compatibility_flat"
    if normalized.startswith(".loopplane/workflows/"):
        return "canonical_v16"
    if normalized:
        return "custom"
    return "unknown"


def _workflow_mutability_status(record: Mapping[str, Any]) -> str | None:
    status = str(record.get("status") or "").strip()
    archived = bool(record.get("archived")) or status == "archived"
    read_only = bool(record.get("read_only")) or status == "read_only_imported"
    if archived:
        return "archived_workflow"
    if read_only:
        return "read_only_workflow"
    return None


def _workflow_switch_safety(
    project: Path,
    *,
    workflows: Sequence[Mapping[str, Any]],
    selected_record: Mapping[str, Any],
    current_workflow_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    selected_workflow_id = str(selected_record.get("workflow_id") or "")
    blockers: list[dict[str, Any]] = []
    active_records = [
        dict(record)
        for record in workflows
        if str(record.get("status") or "").strip() in RUNTIME_RUNNING_WORKFLOW_STATUSES
    ]
    if len(active_records) > 1:
        active_ids = ", ".join(
            f"{record.get('workflow_id')}:{record.get('status')}" for record in active_records
        )
        blockers.append(
            {
                "code": "active_running_policy_conflict",
                "workflow_id": selected_workflow_id,
                "message": f"one active-running workflow per workspace is allowed by default; found {active_ids}",
            }
        )
    elif active_records:
        active_id = str(active_records[0].get("workflow_id") or "")
        if active_id != selected_workflow_id:
            blockers.append(
                {
                    "code": "active_running_conflict",
                    "workflow_id": active_id,
                    "message": (
                        f"workflow {active_id!r} is {active_records[0].get('status')}; "
                        "switching the current pointer away from the active-running workflow is unsafe."
                    ),
                }
            )

    checked_records = _switch_safety_records(
        workflows,
        selected_workflow_id=selected_workflow_id,
        current_workflow_id=current_workflow_id,
        active_records=active_records,
    )
    checked_workflows: list[dict[str, Any]] = []
    for record in checked_records:
        workflow_id = str(record.get("workflow_id") or "")
        runtime_dir = _runtime_dir_for_record(project, record)
        if runtime_dir is None:
            blockers.append(
                {
                    "code": "invalid_runtime_path",
                    "workflow_id": workflow_id,
                    "message": f"workflow {workflow_id!r} has an invalid runtime_dir path.",
                }
            )
            continue
        runtime_dir_record = _relative_to_project(project, runtime_dir)
        checked_workflows.append(
            {
                "workflow_id": workflow_id,
                "status": str(record.get("status") or ""),
                "runtime_dir": runtime_dir_record,
            }
        )
        lock_blocker = _scheduler_lock_blocker(project, workflow_id, runtime_dir, now=now)
        if lock_blocker is not None:
            blockers.append(lock_blocker)
        blockers.extend(_active_run_lease_blockers(project, workflow_id, runtime_dir, now=now))
        supervisor_blocker = _supervisor_blocker(project, workflow_id, runtime_dir, now=now)
        if supervisor_blocker is not None:
            blockers.append(supervisor_blocker)

    return {
        "checked": True,
        "status": _switch_blocker_status(blockers),
        "selected_workflow_id": selected_workflow_id,
        "current_workflow_id": current_workflow_id,
        "checked_workflows": checked_workflows,
        "blockers": blockers,
    }


def _workflow_create_safety(
    project: Path,
    *,
    workflows: Sequence[Mapping[str, Any]],
    new_record: Mapping[str, Any],
    current_workflow_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    new_workflow_id = str(new_record.get("workflow_id") or "")
    blockers: list[dict[str, Any]] = []
    active_records = [
        dict(record)
        for record in workflows
        if str(record.get("status") or "").strip() in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    if len(active_records) > 1:
        active_ids = ", ".join(
            f"{record.get('workflow_id')}:{record.get('status')}" for record in active_records
        )
        blockers.append(
            {
                "code": "active_running_policy_conflict",
                "workflow_id": new_workflow_id,
                "message": f"one active-running workflow per workspace is allowed by default; found {active_ids}",
            }
        )
    elif active_records:
        active_id = str(active_records[0].get("workflow_id") or "")
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": active_id,
                "message": (
                    f"workflow {active_id!r} is {active_records[0].get('status')}; "
                    "creating a new current workflow is unsafe while another workflow is active-running."
                ),
            }
        )

    checked_workflows: list[dict[str, Any]] = []
    records = [*workflows, new_record]
    seen: set[str] = set()
    for record in records:
        workflow_id = str(record.get("workflow_id") or "")
        if not workflow_id or workflow_id in seen:
            continue
        seen.add(workflow_id)
        runtime_dir = _runtime_dir_for_record(project, record)
        if runtime_dir is None:
            blockers.append(
                {
                    "code": "invalid_runtime_path",
                    "workflow_id": workflow_id,
                    "message": f"workflow {workflow_id!r} has an invalid runtime_dir path.",
                }
            )
            continue
        runtime_dir_record = _relative_to_project(project, runtime_dir)
        checked_workflows.append(
            {
                "workflow_id": workflow_id,
                "status": str(record.get("status") or ""),
                "runtime_dir": runtime_dir_record,
            }
        )
        lock_blocker = _scheduler_lock_blocker(project, workflow_id, runtime_dir, now=now)
        if lock_blocker is not None:
            blockers.append(lock_blocker)
        blockers.extend(_active_run_lease_blockers(project, workflow_id, runtime_dir, now=now))
        supervisor_blocker = _supervisor_blocker(project, workflow_id, runtime_dir, now=now)
        if supervisor_blocker is not None:
            blockers.append(supervisor_blocker)

    return {
        "checked": True,
        "status": _switch_blocker_status(blockers) if blockers else "safe",
        "new_workflow_id": new_workflow_id,
        "current_workflow_id": current_workflow_id,
        "checked_workflows": checked_workflows,
        "blockers": blockers,
    }


def _workflow_archive_safety(
    project: Path,
    *,
    workflows: Sequence[Mapping[str, Any]],
    selected_record: Mapping[str, Any],
    current_workflow_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    selected_workflow_id = str(selected_record.get("workflow_id") or "")
    blockers: list[dict[str, Any]] = []
    active_records = [
        dict(record)
        for record in workflows
        if str(record.get("status") or "").strip() in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    selected_status = str(selected_record.get("status") or "").strip()
    if len(active_records) > 1:
        active_ids = ", ".join(
            f"{record.get('workflow_id')}:{record.get('status')}" for record in active_records
        )
        blockers.append(
            {
                "code": "active_running_policy_conflict",
                "workflow_id": selected_workflow_id,
                "message": f"one active-running workflow per workspace is allowed by default; found {active_ids}",
            }
        )
    elif selected_status in ACTIVE_RUNNING_WORKFLOW_STATUSES:
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": selected_workflow_id,
                "message": (
                    f"workflow {selected_workflow_id!r} is {selected_status}; "
                    "archiving an active-running workflow is unsafe."
                ),
            }
        )
    elif active_records:
        active_id = str(active_records[0].get("workflow_id") or "")
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": active_id,
                "message": (
                    f"workflow {active_id!r} is {active_records[0].get('status')}; "
                    "archiving workflow history while another workflow is active-running is unsafe."
                ),
            }
        )

    checked_records = _switch_safety_records(
        workflows,
        selected_workflow_id=selected_workflow_id,
        current_workflow_id=current_workflow_id,
        active_records=active_records,
    )
    checked_workflows: list[dict[str, Any]] = []
    for record in checked_records:
        workflow_id = str(record.get("workflow_id") or "")
        runtime_dir = _runtime_dir_for_record(project, record)
        if runtime_dir is None:
            blockers.append(
                {
                    "code": "invalid_runtime_path",
                    "workflow_id": workflow_id,
                    "message": f"workflow {workflow_id!r} has an invalid runtime_dir path.",
                }
            )
            continue
        runtime_dir_record = _relative_to_project(project, runtime_dir)
        checked_workflows.append(
            {
                "workflow_id": workflow_id,
                "status": str(record.get("status") or ""),
                "runtime_dir": runtime_dir_record,
            }
        )
        lock_blocker = _scheduler_lock_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow archive",
        )
        if lock_blocker is not None:
            blockers.append(lock_blocker)
        blockers.extend(
            _active_run_lease_blockers(
                project,
                workflow_id,
                runtime_dir,
                now=now,
                operation="workflow archive",
            )
        )
        supervisor_blocker = _supervisor_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow archive",
        )
        if supervisor_blocker is not None:
            blockers.append(supervisor_blocker)

    return {
        "checked": True,
        "status": _archive_blocker_status(blockers),
        "selected_workflow_id": selected_workflow_id,
        "current_workflow_id": current_workflow_id,
        "checked_workflows": checked_workflows,
        "blockers": blockers,
    }


def _workflow_restore_safety(
    project: Path,
    *,
    workflows: Sequence[Mapping[str, Any]],
    selected_record: Mapping[str, Any],
    current_workflow_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    selected_workflow_id = str(selected_record.get("workflow_id") or "")
    blockers: list[dict[str, Any]] = []
    active_records = [
        dict(record)
        for record in workflows
        if str(record.get("status") or "").strip() in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    selected_status = str(selected_record.get("status") or "").strip()
    if len(active_records) > 1:
        active_ids = ", ".join(
            f"{record.get('workflow_id')}:{record.get('status')}" for record in active_records
        )
        blockers.append(
            {
                "code": "active_running_policy_conflict",
                "workflow_id": selected_workflow_id,
                "message": f"one active-running workflow per workspace is allowed by default; found {active_ids}",
            }
        )
    elif selected_status in ACTIVE_RUNNING_WORKFLOW_STATUSES:
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": selected_workflow_id,
                "message": (
                    f"workflow {selected_workflow_id!r} is {selected_status}; "
                    "restoring an active-running workflow is unsafe."
                ),
            }
        )
    elif active_records:
        active_id = str(active_records[0].get("workflow_id") or "")
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": active_id,
                "message": (
                    f"workflow {active_id!r} is {active_records[0].get('status')}; "
                    "restoring workflow history while another workflow is active-running is unsafe."
                ),
            }
        )

    checked_records = _switch_safety_records(
        workflows,
        selected_workflow_id=selected_workflow_id,
        current_workflow_id=current_workflow_id,
        active_records=active_records,
    )
    checked_workflows: list[dict[str, Any]] = []
    for record in checked_records:
        workflow_id = str(record.get("workflow_id") or "")
        runtime_dir = _runtime_dir_for_record(project, record)
        if runtime_dir is None:
            blockers.append(
                {
                    "code": "invalid_runtime_path",
                    "workflow_id": workflow_id,
                    "message": f"workflow {workflow_id!r} has an invalid runtime_dir path.",
                }
            )
            continue
        runtime_dir_record = _relative_to_project(project, runtime_dir)
        checked_workflows.append(
            {
                "workflow_id": workflow_id,
                "status": str(record.get("status") or ""),
                "runtime_dir": runtime_dir_record,
            }
        )
        lock_blocker = _scheduler_lock_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow restore",
        )
        if lock_blocker is not None:
            blockers.append(lock_blocker)
        blockers.extend(
            _active_run_lease_blockers(
                project,
                workflow_id,
                runtime_dir,
                now=now,
                operation="workflow restore",
            )
        )
        supervisor_blocker = _supervisor_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow restore",
        )
        if supervisor_blocker is not None:
            blockers.append(supervisor_blocker)

    return {
        "checked": True,
        "status": _restore_blocker_status(blockers),
        "selected_workflow_id": selected_workflow_id,
        "current_workflow_id": current_workflow_id,
        "checked_workflows": checked_workflows,
        "blockers": blockers,
    }


def _workflow_fork_safety(
    project: Path,
    *,
    workflows: Sequence[Mapping[str, Any]],
    selected_record: Mapping[str, Any],
    new_record: Mapping[str, Any],
    current_workflow_id: str | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    selected_workflow_id = str(selected_record.get("workflow_id") or "")
    new_workflow_id = str(new_record.get("workflow_id") or "")
    blockers: list[dict[str, Any]] = []
    active_records = [
        dict(record)
        for record in workflows
        if str(record.get("status") or "").strip() in ACTIVE_RUNNING_WORKFLOW_STATUSES
    ]
    selected_status = str(selected_record.get("status") or "").strip()
    if len(active_records) > 1:
        active_ids = ", ".join(
            f"{record.get('workflow_id')}:{record.get('status')}" for record in active_records
        )
        blockers.append(
            {
                "code": "active_running_policy_conflict",
                "workflow_id": selected_workflow_id,
                "message": f"one active-running workflow per workspace is allowed by default; found {active_ids}",
            }
        )
    elif selected_status in ACTIVE_RUNNING_WORKFLOW_STATUSES:
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": selected_workflow_id,
                "message": (
                    f"workflow {selected_workflow_id!r} is {selected_status}; "
                    "forking an active-running workflow is unsafe."
                ),
            }
        )
    elif active_records:
        active_id = str(active_records[0].get("workflow_id") or "")
        blockers.append(
            {
                "code": "active_running_conflict",
                "workflow_id": active_id,
                "message": (
                    f"workflow {active_id!r} is {active_records[0].get('status')}; "
                    "forking workflow history while another workflow is active-running is unsafe."
                ),
            }
        )

    checked_workflows: list[dict[str, Any]] = []
    records = [*workflows, new_record]
    seen: set[str] = set()
    for record in records:
        workflow_id = str(record.get("workflow_id") or "")
        if not workflow_id or workflow_id in seen:
            continue
        seen.add(workflow_id)
        runtime_dir = _runtime_dir_for_record(project, record)
        if runtime_dir is None:
            blockers.append(
                {
                    "code": "invalid_runtime_path",
                    "workflow_id": workflow_id,
                    "message": f"workflow {workflow_id!r} has an invalid runtime_dir path.",
                }
            )
            continue
        runtime_dir_record = _relative_to_project(project, runtime_dir)
        checked_workflows.append(
            {
                "workflow_id": workflow_id,
                "status": str(record.get("status") or ""),
                "runtime_dir": runtime_dir_record,
            }
        )
        lock_blocker = _scheduler_lock_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow fork",
        )
        if lock_blocker is not None:
            blockers.append(lock_blocker)
        blockers.extend(
            _active_run_lease_blockers(
                project,
                workflow_id,
                runtime_dir,
                now=now,
                operation="workflow fork",
            )
        )
        supervisor_blocker = _supervisor_blocker(
            project,
            workflow_id,
            runtime_dir,
            now=now,
            operation="workflow fork",
        )
        if supervisor_blocker is not None:
            blockers.append(supervisor_blocker)

    return {
        "checked": True,
        "status": _fork_blocker_status(blockers),
        "selected_workflow_id": selected_workflow_id,
        "new_workflow_id": new_workflow_id,
        "current_workflow_id": current_workflow_id,
        "checked_workflows": checked_workflows,
        "blockers": blockers,
    }


def _switch_safety_records(
    workflows: Sequence[Mapping[str, Any]],
    *,
    selected_workflow_id: str,
    current_workflow_id: str | None,
    active_records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    wanted = {selected_workflow_id}
    if current_workflow_id:
        wanted.add(current_workflow_id)
    wanted.update(str(record.get("workflow_id") or "") for record in active_records)
    records: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in workflows:
        workflow_id = str(record.get("workflow_id") or "")
        if workflow_id in wanted and workflow_id not in seen:
            records.append(record)
            seen.add(workflow_id)
    return records


def _switch_blocker_status(blockers: Sequence[Mapping[str, Any]]) -> str:
    codes = {str(blocker.get("code") or "") for blocker in blockers}
    if "active_running_policy_conflict" in codes or "active_running_conflict" in codes:
        return "active_running_conflict"
    if any("scheduler_lock" in code for code in codes):
        return "scheduler_lock_conflict"
    if any("active_run_lease" in code for code in codes):
        return "active_run_conflict"
    if any("supervisor" in code for code in codes):
        return "scheduler_supervisor_conflict"
    if blockers:
        return "workflow_switch_blocked"
    return "safe"


def _archive_blocker_status(blockers: Sequence[Mapping[str, Any]]) -> str:
    status = _switch_blocker_status(blockers)
    if status == "workflow_switch_blocked":
        return "workflow_archive_blocked"
    return status


def _restore_blocker_status(blockers: Sequence[Mapping[str, Any]]) -> str:
    status = _switch_blocker_status(blockers)
    if status == "workflow_switch_blocked":
        return "workflow_restore_blocked"
    return status


def _fork_blocker_status(blockers: Sequence[Mapping[str, Any]]) -> str:
    status = _switch_blocker_status(blockers)
    if status == "workflow_switch_blocked":
        return "workflow_fork_blocked"
    return status


def _runtime_dir_for_record(project: Path, record: Mapping[str, Any]) -> Path | None:
    runtime_dir = str(record.get("runtime_dir") or "").strip()
    if not runtime_dir:
        workflow_root = str(record.get("workflow_root") or "").strip().rstrip("/")
        runtime_dir = ".loopplane/runtime" if workflow_root == ".loopplane" else f"{workflow_root}/runtime"
    if not _valid_project_path(runtime_dir):
        return None
    return (project / PurePosixPath(runtime_dir)).resolve()


def _scheduler_lock_blocker(
    project: Path,
    workflow_id: str,
    runtime_dir: Path,
    *,
    now: datetime,
    operation: str = "workflow switch",
) -> dict[str, Any] | None:
    owner_path = runtime_dir / "lock" / "scheduler_instance_lock" / "owner.json"
    if not owner_path.exists():
        return None
    rel = _relative_to_project(project, owner_path)
    try:
        owner = _load_json_object(owner_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return {
            "code": "malformed_scheduler_lock",
            "workflow_id": workflow_id,
            "path": rel,
            "message": f"Scheduler lock metadata exists but is unreadable: {error}",
        }
    heartbeat = _parse_utc_timestamp(owner.get("heartbeat_at"))
    ttl = _positive_int(owner.get("ttl_seconds")) or SCHEDULER_LOCK_TTL_SECONDS
    fresh = heartbeat is not None and heartbeat + timedelta(seconds=ttl) >= now
    if not fresh:
        try:
            owner_path.unlink()
        except FileNotFoundError:
            return None
        except OSError:
            pass
        else:
            return None
    return {
        "code": "active_scheduler_lock" if fresh else "stale_scheduler_lock",
        "workflow_id": workflow_id,
        "path": rel,
        "owner": str(owner.get("owner") or ""),
        "heartbeat_at": owner.get("heartbeat_at"),
        "ttl_seconds": ttl,
        "fresh": fresh,
        "message": (
            f"Scheduler lock metadata is present for workflow {workflow_id}; "
            f"{operation} will not update workflow history while scheduler ownership is unresolved."
        ),
    }


def _active_run_lease_blockers(
    project: Path,
    workflow_id: str,
    runtime_dir: Path,
    *,
    now: datetime,
    operation: str = "workflow switch",
) -> list[dict[str, Any]]:
    lease_dir = runtime_dir / "active_run_leases"
    if not lease_dir.exists():
        return []
    blockers: list[dict[str, Any]] = []
    for lease_file in sorted(path for path in lease_dir.glob("*.json") if path.is_file()):
        rel = _relative_to_project(project, lease_file)
        try:
            lease = _load_json_object(lease_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            blockers.append(
                {
                    "code": "malformed_active_run_lease",
                    "workflow_id": workflow_id,
                    "path": rel,
                    "message": f"Active-run lease metadata exists but is unreadable: {error}",
                }
            )
            continue
        status = str(lease.get("status") or "running").strip().lower()
        if not _lease_blocks_scheduler(lease):
            continue
        if status in ACTIVE_RUN_LEASE_INACTIVE_STATUSES:
            continue
        heartbeat = _parse_utc_timestamp(lease.get("heartbeat_at"))
        expires = _parse_utc_timestamp(lease.get("lease_expires_at"))
        fresh = (expires is not None and expires >= now) or (
            heartbeat is not None and heartbeat + timedelta(seconds=ACTIVE_RUN_LEASE_TTL_SECONDS) >= now
        )
        if status not in ACTIVE_RUN_LEASE_ACTIVE_STATUSES:
            code = "unknown_active_run_lease_status"
        else:
            code = "active_run_lease" if fresh else "stale_active_run_lease"
        blockers.append(
            {
                "code": code,
                "workflow_id": workflow_id,
                "path": rel,
                "status": status,
                "fresh": fresh,
                "heartbeat_at": lease.get("heartbeat_at"),
                "lease_expires_at": lease.get("lease_expires_at"),
                "message": (
                    f"Active-run lease {rel} is not terminal; {operation} must wait for active runs "
                    "to complete or be recovered."
                ),
            }
        )
    return blockers


def _lease_blocks_scheduler(lease: Mapping[str, Any]) -> bool:
    if lease.get("blocks_scheduler") is False:
        return False
    return str(lease.get("role") or "").strip().lower() != "inspector"


def _supervisor_blocker(
    project: Path,
    workflow_id: str,
    runtime_dir: Path,
    *,
    now: datetime,
    operation: str = "workflow switch",
) -> dict[str, Any] | None:
    metadata_path = runtime_dir / "supervisor.json"
    if not metadata_path.exists():
        return None
    rel = _relative_to_project(project, metadata_path)
    try:
        metadata = _load_json_object(metadata_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return {
            "code": "malformed_supervisor_metadata",
            "workflow_id": workflow_id,
            "path": rel,
            "message": f"Detached supervisor metadata exists but is unreadable: {error}",
        }
    status = str(metadata.get("status") or "unknown").strip().lower()
    if status in TERMINAL_SUPERVISOR_STATUSES:
        return None
    if status not in ACTIVE_SUPERVISOR_STATUSES:
        status = "unknown"
    heartbeat = _parse_utc_timestamp(metadata.get("heartbeat_at"))
    fresh = heartbeat is not None and heartbeat + timedelta(seconds=SUPERVISOR_HEARTBEAT_TTL_SECONDS) >= now
    return {
        "code": "active_supervisor" if fresh else "stale_supervisor",
        "workflow_id": workflow_id,
        "path": rel,
        "status": status,
        "fresh": fresh,
        "heartbeat_at": metadata.get("heartbeat_at"),
        "pid": metadata.get("pid"),
        "message": (
            f"Detached supervisor metadata is present for workflow {workflow_id}; "
            f"{operation} requires supervisor state to be terminal or recovered first."
        ),
    }


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _summary_one_line(summary: Any) -> str:
    if not isinstance(summary, Mapping):
        return ""
    return str(summary.get("one_line") or summary.get("summary") or "").strip()


def _fork_brief_from_source(
    source_record: Mapping[str, Any],
    *,
    fork_name: str,
    source_workflow_id: str,
) -> str:
    source_name = str(source_record.get("name") or "").strip()
    source_status = str(source_record.get("status") or "").strip()
    source_summary = _summary_one_line(source_record.get("summary"))
    lines = [
        fork_name,
        "",
        f"Forked from workflow {source_workflow_id}.",
    ]
    if source_name:
        lines.append(f"Source name: {source_name}.")
    if source_status:
        lines.append(f"Source status: {source_status}.")
    if source_summary:
        lines.append(f"Source summary: {source_summary}")
    return "\n".join(lines).strip() + "\n"


def _new_workflow_identity(
    project: Path,
    workflows: Sequence[Mapping[str, Any]],
    created_at: str,
) -> tuple[str, str]:
    existing_ids = {str(record.get("workflow_id") or "") for record in workflows}
    date_part = created_at[:10].replace("-", "")
    for _ in range(64):
        workflow_id = f"wf_{date_part}_{uuid.uuid4().hex[:8]}"
        workflow_root = f".loopplane/workflows/{workflow_id}"
        if workflow_id not in existing_ids and not (project / workflow_root).exists():
            return workflow_id, workflow_root
    raise RuntimeError("unable to allocate an unused workflow_id")


def _workflow_name_from_brief(brief: str) -> str:
    text = _summary_from_brief(brief)
    return _truncate_workflow_name(text, limit=96)


def _truncate_workflow_name(text: str, *, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    truncated = normalized[:limit].rstrip()
    boundary = truncated.rfind(" ")
    if boundary >= max(32, limit // 2):
        return truncated[:boundary].rstrip()
    return truncated


def _summary_from_brief(brief: str) -> str:
    for line in str(brief or "").splitlines():
        text = " ".join(line.strip().split())
        if text:
            return text
    return "Workflow created from CLI brief."
