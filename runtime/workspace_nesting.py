from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

NESTED_WORKSPACE_SCHEMA_VERSION = "1.6"
NESTED_WORKSPACE_POLICY_SCHEMA_VERSION = "1.6"
LOOPPLANE_INSTANCE_MARKERS = (
    ".loopplane/workspace.json",
    ".loopplane/config/instance.json",
    ".loopplane/config/workflow.json",
)


def detect_nested_loopplane_instances(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    parents = [_workspace_record(candidate, "parent", project) for candidate in _parent_workspace_projects(project)]
    children = [_workspace_record(candidate, "child", project) for candidate in _child_workspace_projects(project)]
    relationships = parents + children
    return {
        "schema_version": NESTED_WORKSPACE_SCHEMA_VERSION,
        "status": "nested_detected" if relationships else "isolated",
        "project_root": project.as_posix(),
        "requires_explicit_namespace_or_approval": bool(relationships),
        "parent_count": len(parents),
        "child_count": len(children),
        "overlap_count": 0,
        "nested_workspace_count": len(relationships),
        "parents": parents,
        "children": children,
        "overlapping": [],
        "relationships": relationships,
        "warnings": nested_workspace_warning_messages(relationships),
        "recovery_actions": nested_workspace_recovery_actions() if relationships else [],
    }


def nested_workspace_warning_messages(relationships: Sequence[Mapping[str, Any]]) -> list[str]:
    messages: list[str] = []
    for relationship in relationships:
        kind = str(relationship.get("relationship") or "nested")
        project = str(relationship.get("project_root") or "unknown")
        workspace_id = str(relationship.get("workspace_id") or "unknown")
        if kind == "parent":
            messages.append(
                "Nested LoopPlane parent workspace detected at "
                f"{project} (workspace_id={workspace_id}); nested operations require an explicit namespace or approval."
            )
        elif kind == "child":
            messages.append(
                "Nested LoopPlane child workspace detected at "
                f"{project} (workspace_id={workspace_id}); parent operations must not absorb child workspace truth."
            )
        else:
            messages.append(
                "Overlapping LoopPlane workspace detected at "
                f"{project} (workspace_id={workspace_id}); operations require explicit authority."
            )
    return messages


def nested_workspace_recovery_actions() -> list[str]:
    return [
        "Run loopplane workspace doctor --project <nested-project> to inspect each project-local .loopplane instance.",
        "Use an explicit namespace or approval path before running operations that cross nested workspace boundaries.",
        "Keep project-local .loopplane metadata authoritative; do not rely on LOOPPLANE_HOME to resolve nested workspace truth.",
    ]


def evaluate_nested_workspace_operation(
    project_root: Path | str,
    *,
    command: str,
    explicit_target: bool = False,
    workspace_namespace: str | None = None,
    allow_nested_workspace: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    nesting = detect_nested_loopplane_instances(project)
    nested_count = int(nesting.get("nested_workspace_count") or 0)
    namespace = str(workspace_namespace or "").strip()
    namespaces = _current_workspace_namespaces(project)
    base = {
        "schema_version": NESTED_WORKSPACE_POLICY_SCHEMA_VERSION,
        "command": command,
        "project_root": project.as_posix(),
        "workspace_namespace": namespace or None,
        "explicit_target": bool(explicit_target),
        "allow_nested_workspace": bool(allow_nested_workspace),
        "nested_workspaces": nesting,
        "valid_namespaces": sorted(namespaces),
        "warnings": list(nesting.get("warnings") or []),
        "errors": [],
        "recovery_actions": nested_workspace_recovery_actions() if nested_count else [],
    }
    if nested_count <= 0:
        return {
            **base,
            "ok": True,
            "status": "isolated",
            "authorization": "not_required",
        }
    if allow_nested_workspace:
        return {
            **base,
            "ok": True,
            "status": "allowed",
            "authorization": "explicit_allow_nested_workspace",
        }
    if namespace:
        if _namespace_matches_current_workspace(namespace, namespaces):
            return {
                **base,
                "ok": True,
                "status": "allowed",
                "authorization": "explicit_workspace_namespace",
            }
        return {
            **base,
            "ok": False,
            "status": "nested_workspace_namespace_mismatch",
            "authorization": "blocked",
            "errors": [
                (
                    f"{command} was invoked in a nested LoopPlane workspace, but "
                    f"--workspace-namespace={namespace!r} does not match the targeted project-local workspace."
                )
            ],
            "recovery_actions": [
                "Pass --workspace-namespace with the targeted workspace_id or absolute project path.",
                "Pass --project <target-project> when the command supports explicit project targeting.",
                "Use --allow-nested-workspace only after verifying the parent/child workspace relationship.",
            ],
        }
    if explicit_target:
        return {
            **base,
            "ok": True,
            "status": "allowed",
            "authorization": "explicit_target",
        }
    return {
        **base,
        "ok": False,
        "status": "nested_workspace_requires_explicit_namespace",
        "authorization": "blocked",
        "errors": [
            (
                f"{command} was invoked in a nested LoopPlane workspace without an explicit namespace, "
                "explicit target, explicit allow flag, or approval signal."
            )
        ],
        "recovery_actions": [
            "Pass --project <target-project> when the command supports explicit project targeting.",
            "Pass --workspace-namespace with the targeted workspace_id or absolute project path.",
            "Use --allow-nested-workspace only after verifying the parent/child workspace relationship.",
            "Keep parent and child project-local .loopplane workflow truth separate.",
        ],
    }


def _parent_workspace_projects(project: Path) -> list[Path]:
    parents: list[Path] = []
    for candidate in project.parents:
        if _has_loopplane_instance(candidate):
            parents.append(candidate.resolve())
    return parents


def _child_workspace_projects(project: Path) -> list[Path]:
    if not project.exists() or not project.is_dir():
        return []
    children: list[Path] = []
    seen: set[str] = set()

    def add_candidate(candidate: Path) -> None:
        resolved = candidate.resolve()
        if resolved == project:
            return
        key = resolved.as_posix()
        if key not in seen:
            seen.add(key)
            children.append(resolved)

    def onerror(error: OSError) -> None:
        raise error

    for current_root, dirnames, _filenames in os.walk(project, onerror=onerror):
        current = Path(current_root)
        kept: list[str] = []
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                continue
            if dirname == ".loopplane":
                if current != project and _has_loopplane_instance(current):
                    add_candidate(current)
                continue
            kept.append(dirname)
        dirnames[:] = kept
    return children


def _has_loopplane_instance(project: Path) -> bool:
    return any((project / marker).is_file() for marker in LOOPPLANE_INSTANCE_MARKERS)


def _workspace_record(project: Path, relationship: str, base_project: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "relationship": relationship,
        "project_root": project.as_posix(),
        "loopplane_dir": (project / ".loopplane").as_posix(),
        "relative_to_project": _relative(project, base_project),
        "markers": [marker for marker in LOOPPLANE_INSTANCE_MARKERS if (project / marker).is_file()],
    }
    workspace = _load_json_object(project / ".loopplane" / "workspace.json")
    current = _load_json_object(project / ".loopplane" / "current_workflow.json")
    workflow_config = _load_json_object(project / ".loopplane" / "config" / "workflow.json")
    instance = _load_json_object(project / ".loopplane" / "config" / "instance.json")
    workspace_id = workspace.get("workspace_id") if isinstance(workspace.get("workspace_id"), str) else None
    current_workflow_id = (
        current.get("current_workflow_id") if isinstance(current.get("current_workflow_id"), str) else None
    )
    if current_workflow_id is None and isinstance(workflow_config.get("workflow_id"), str):
        current_workflow_id = str(workflow_config["workflow_id"])
    if current_workflow_id is None and isinstance(instance.get("workflow_id"), str):
        current_workflow_id = str(instance["workflow_id"])
    if workspace_id:
        record["workspace_id"] = workspace_id
    if current_workflow_id:
        record["current_workflow_id"] = current_workflow_id
    for key in ("workspace_boundary", "allow_out_of_boundary_writes"):
        if key in workspace:
            record[key] = workspace[key]
    return record


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _relative(path: Path, base: Path) -> str:
    try:
        relative = os.path.relpath(path, start=base)
    except ValueError:
        return path.as_posix()
    return relative.replace(os.sep, "/")


def _current_workspace_namespaces(project: Path) -> set[str]:
    namespaces = {project.as_posix()}
    workspace = _load_json_object(project / ".loopplane" / "workspace.json")
    workspace_id = workspace.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id.strip():
        namespaces.add(workspace_id.strip())
    return namespaces


def _namespace_matches_current_workspace(namespace: str, namespaces: set[str]) -> bool:
    if namespace in namespaces:
        return True
    if namespace in {".", "./"}:
        return Path.cwd().resolve().as_posix() in namespaces
    if namespace.startswith("/") or namespace.startswith("~") or namespace.startswith("."):
        try:
            return Path(namespace).expanduser().resolve().as_posix() in namespaces
        except OSError:
            return False
    return False
