from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
SUPPORTED_WORKSPACE_BOUNDARIES = frozenset({"project_root"})


def normalize_identity_path_value(field: str, value: object, *, allow_parent: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip().replace("\\", "/")
    if WINDOWS_ABSOLUTE_RE.match(text) or text.startswith("//"):
        raise ValueError(f"{field} must be a relative POSIX path: {value}")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ValueError(f"{field} must be a relative POSIX path: {value}")
    if not allow_parent and ".." in path.parts:
        raise ValueError(f"{field} must stay inside the project root: {value}")
    return path.as_posix()


def relative_root_value(project_root: Path | str, root: Path | str) -> str:
    project = Path(project_root).expanduser().resolve()
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    relative = os.path.relpath(candidate.resolve(), start=project)
    normalized = relative.replace(os.sep, "/")
    if normalized == ".":
        return "."
    return normalize_identity_path_value("repo_root", normalized, allow_parent=True)


def repository_root_value(
    project_root: Path | str,
    version_control: Mapping[str, Any] | None = None,
) -> str:
    project = Path(project_root).expanduser().resolve()
    root = _repository_root_from_status(version_control)
    if root is None:
        root = discover_enclosing_git_root(project)
    if root is None:
        root = project
    return relative_root_value(project, root)


def discover_enclosing_git_root(project_root: Path | str) -> Path | None:
    project = Path(project_root).expanduser().resolve()
    for candidate in (project, *project.parents):
        git_marker = candidate / ".git"
        if git_marker.exists():
            return candidate
    return None


def resolve_identity_path(project_root: Path | str, value: object, *, allow_parent: bool = False) -> Path:
    project = Path(project_root).expanduser().resolve()
    normalized = normalize_identity_path_value("identity path", value, allow_parent=allow_parent)
    return (project / normalized).resolve()


def normalize_workspace_boundary(value: object) -> str:
    if value is None:
        return "project_root"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("workspace_boundary must be a non-empty string")
    text = value.strip()
    if text not in SUPPORTED_WORKSPACE_BOUNDARIES:
        supported = ", ".join(sorted(SUPPORTED_WORKSPACE_BOUNDARIES))
        raise ValueError(f"workspace_boundary must be one of: {supported}")
    return text


def workspace_boundary_root(project_root: Path | str, workspace: Mapping[str, Any]) -> Path:
    project = Path(project_root).expanduser().resolve()
    project_root_value = normalize_identity_path_value("project_root", workspace.get("project_root"), allow_parent=False)
    boundary = normalize_workspace_boundary(workspace.get("workspace_boundary"))
    if boundary == "project_root":
        return resolve_identity_path(project, project_root_value)
    raise ValueError(f"unsupported workspace_boundary: {boundary}")


def workspace_identity_summary(project_root: Path | str, workspace: Mapping[str, Any]) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    project_root_value = normalize_identity_path_value("project_root", workspace.get("project_root"), allow_parent=False)
    repo_root_value = normalize_identity_path_value("repo_root", workspace.get("repo_root"), allow_parent=True)
    boundary = normalize_workspace_boundary(workspace.get("workspace_boundary"))
    return {
        "project_root": project_root_value,
        "repo_root": repo_root_value,
        "resolved_project_root": resolve_identity_path(project, project_root_value).as_posix(),
        "resolved_repo_root": resolve_identity_path(project, repo_root_value, allow_parent=True).as_posix(),
        "workspace_boundary": boundary,
        "resolved_workspace_boundary": workspace_boundary_root(project, workspace).as_posix(),
        "allow_out_of_boundary_writes": workspace.get("allow_out_of_boundary_writes", False),
    }


def _repository_root_from_status(version_control: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(version_control, Mapping):
        return None
    repository = version_control.get("repository")
    if not isinstance(repository, Mapping):
        return None
    root = repository.get("root")
    if not isinstance(root, str) or not root.strip():
        return None
    return Path(root)
