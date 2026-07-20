from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from runtime.adapters.base import AdapterInput
from runtime.file_discovery import FileDiscoveryResult, discover_files_bounded
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.workspace_boundary_policy import evaluate_worker_write_boundary
from runtime.workspace_identity import resolve_identity_path, workspace_boundary_root


ADAPTER_BOUNDARY_ROLES = frozenset({"worker", "recovery", "recovery_worker"})
SKIPPED_WATCH_DIR_NAMES = frozenset({".git"})
BOUNDARY_SCAN_ENTRY_LIMIT = 20_000
BOUNDARY_SCAN_MATCH_LIMIT = 20_000


@dataclass(frozen=True)
class AdapterBoundarySnapshot:
    enabled: bool
    reason: str
    project_root: Path | None = None
    watch_root: Path | None = None
    boundary_root: Path | None = None
    files: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    scanned_entries: int = 0
    scan_truncated: bool = False


def snapshot_adapter_workspace_boundary(adapter_input: AdapterInput) -> AdapterBoundarySnapshot:
    role = str(adapter_input.role)
    if role not in ADAPTER_BOUNDARY_ROLES:
        return AdapterBoundarySnapshot(enabled=False, reason="role_not_enforced")

    project = _adapter_project_root(adapter_input)
    workspace_path = project / ".loopplane" / "workspace.json"
    workspace = _read_json_object(workspace_path)
    if workspace is None:
        return AdapterBoundarySnapshot(enabled=False, reason="workspace_identity_missing", project_root=project)

    try:
        boundary = workspace_boundary_root(project, workspace)
        repo_root = resolve_identity_path(project, workspace.get("repo_root", "."), allow_parent=True)
    except (OSError, ValueError) as error:
        return AdapterBoundarySnapshot(
            enabled=False,
            reason=f"workspace_identity_invalid:{type(error).__name__}",
            project_root=project,
        )

    if not repo_root.exists() or not repo_root.is_dir():
        return AdapterBoundarySnapshot(
            enabled=False,
            reason="repo_root_missing",
            project_root=project,
            watch_root=repo_root,
            boundary_root=boundary,
        )
    if _is_relative_to(repo_root, boundary) and _is_relative_to(boundary, repo_root):
        return AdapterBoundarySnapshot(
            enabled=False,
            reason="repo_root_matches_workspace_boundary",
            project_root=project,
            watch_root=repo_root,
            boundary_root=boundary,
        )

    files, discovery = _snapshot_out_of_boundary_files(repo_root, boundary)
    if discovery.truncated:
        return AdapterBoundarySnapshot(
            enabled=False,
            reason=f"out_of_boundary_watch_budget_exceeded:{discovery.limit_reason or 'bounded_scan'}",
            project_root=project,
            watch_root=repo_root,
            boundary_root=boundary,
            scanned_entries=discovery.scanned_entries,
            scan_truncated=True,
        )
    return AdapterBoundarySnapshot(
        enabled=True,
        reason="watching_repo_outside_workspace_boundary",
        project_root=project,
        watch_root=repo_root,
        boundary_root=boundary,
        files=MappingProxyType(files),
        scanned_entries=discovery.scanned_entries,
    )


def evaluate_adapter_workspace_boundary(
    adapter_input: AdapterInput,
    snapshot: AdapterBoundarySnapshot,
) -> dict[str, Any]:
    if not snapshot.enabled:
        return {
            "schema_version": adapter_input.schema_version,
            "status": "not_applicable",
            "ok": True,
            "enforced": False,
            "reason": snapshot.reason,
        }
    if snapshot.project_root is None or snapshot.watch_root is None or snapshot.boundary_root is None:
        return {
            "schema_version": adapter_input.schema_version,
            "status": "not_applicable",
            "ok": True,
            "enforced": False,
            "reason": "snapshot_incomplete",
        }

    after, discovery = _snapshot_out_of_boundary_files(snapshot.watch_root, snapshot.boundary_root)
    if discovery.truncated:
        return {
            "schema_version": adapter_input.schema_version,
            "status": "not_applicable",
            "ok": True,
            "enforced": False,
            "reason": f"out_of_boundary_watch_budget_exceeded:{discovery.limit_reason or 'bounded_scan'}",
            "scanned_entries": discovery.scanned_entries,
        }
    changes = _diff_snapshots(
        project=snapshot.project_root,
        before=snapshot.files,
        after=after,
    )
    if not changes:
        return {
            "schema_version": adapter_input.schema_version,
            "status": "pass",
            "ok": True,
            "enforced": True,
            "reason": "no_out_of_boundary_adapter_changes",
            "project_root": snapshot.project_root.as_posix(),
            "repo_root": snapshot.watch_root.as_posix(),
            "resolved_workspace_boundary": snapshot.boundary_root.as_posix(),
            "observed_changes": [],
        }

    workflow_paths = _workflow_paths(snapshot.project_root)
    path_policy = evaluate_worker_write_boundary(
        snapshot.project_root,
        workflow_paths,
        task_id=adapter_input.task_id,
        run_dir=adapter_input.role_output_dir,
        adapter_result={"produced_files": [change["path"] for change in changes]},
    )
    return {
        "schema_version": adapter_input.schema_version,
        "status": "pass" if path_policy.get("ok") else "violation",
        "ok": bool(path_policy.get("ok")),
        "enforced": True,
        "reason": (
            "observed_changes_explicitly_allowed"
            if path_policy.get("ok")
            else "observed_out_of_boundary_adapter_changes"
        ),
        "project_root": snapshot.project_root.as_posix(),
        "repo_root": snapshot.watch_root.as_posix(),
        "resolved_workspace_boundary": snapshot.boundary_root.as_posix(),
        "observed_changes": changes,
        "worker_write_boundary": path_policy,
    }


def observed_boundary_change_paths(policy: Mapping[str, Any] | None) -> tuple[Path, ...]:
    if not isinstance(policy, Mapping):
        return ()
    changes = policy.get("observed_changes")
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
        return ()
    paths: list[Path] = []
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        path = change.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(Path(path.strip()))
    return tuple(paths)


def _adapter_project_root(adapter_input: AdapterInput) -> Path:
    configured = adapter_input.env.get("LOOPPLANE_PROJECT_ROOT")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return Path(adapter_input.cwd).expanduser().resolve()


def _workflow_paths(project: Path) -> WorkflowPaths:
    try:
        workflow = load_workflow_config(project)
    except Exception:
        workflow = {}
    return WorkflowPaths.from_config(project, workflow)


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _snapshot_out_of_boundary_files(
    watch_root: Path,
    boundary_root: Path,
) -> tuple[dict[str, Mapping[str, Any]], FileDiscoveryResult]:
    files: dict[str, Mapping[str, Any]] = {}
    discovery = _discover_out_of_boundary_files(watch_root, boundary_root)
    for path in discovery.paths:
        fingerprint = _fingerprint(path)
        if fingerprint is not None:
            files[path.as_posix()] = fingerprint
    return files, discovery


def _discover_out_of_boundary_files(watch_root: Path, boundary_root: Path) -> FileDiscoveryResult:
    root = watch_root.resolve()
    boundary = boundary_root.resolve()

    def inside_boundary(path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return _is_relative_to(resolved, boundary)

    return discover_files_bounded(
        (root,),
        prune_directory_names=SKIPPED_WATCH_DIR_NAMES,
        max_entries=BOUNDARY_SCAN_ENTRY_LIMIT,
        max_matches=BOUNDARY_SCAN_MATCH_LIMIT,
        max_depth=20,
        exclude_path=inside_boundary,
    )


def _fingerprint(path: Path) -> Mapping[str, Any] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return {
        "mtime_ns": int(info.st_mtime_ns),
        "size": int(info.st_size),
        "mode": int(stat.S_IMODE(info.st_mode)),
        "kind": "symlink" if stat.S_ISLNK(info.st_mode) else "file",
    }


def _diff_snapshots(
    *,
    project: Path,
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    before_keys = set(before)
    after_keys = set(after)
    for key in sorted(after_keys - before_keys):
        changes.append({"path": _display_path(project, Path(key)), "change_type": "added"})
    for key in sorted(before_keys & after_keys):
        if before[key] != after[key]:
            changes.append({"path": _display_path(project, Path(key)), "change_type": "modified"})
    for key in sorted(before_keys - after_keys):
        changes.append({"path": _display_path(project, Path(key)), "change_type": "deleted"})
    return changes


def _display_path(project: Path, path: Path) -> str:
    return os.path.relpath(path, start=project).replace(os.sep, "/")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
