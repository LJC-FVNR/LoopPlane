from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence


WORKSPACE_ROOT_VALUE = ".loopplane"
COMPATIBILITY_WORKFLOW_ROOT_VALUE = ".loopplane"
WORKFLOW_REGISTRY_PATH = PurePosixPath(".loopplane/workflow_registry.json")
CURRENT_WORKFLOW_PATH = PurePosixPath(".loopplane/current_workflow.json")
WORKFLOW_CONFIG_PATH = PurePosixPath(".loopplane/config/workflow.json")

WORKFLOW_PATH_FIELDS = (
    "brief_file",
    "plan_file",
    "shared_context_file",
    "results_dir",
    "runtime_dir",
    "read_models_dir",
    "requests_dir",
    "planning_dir",
    "version_control_config_file",
)

DEFAULT_WORKFLOW_PATHS = MappingProxyType(
    {
        "brief_file": "PROJECT_BRIEF.md",
        "plan_file": "PLAN.md",
        "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
        "results_dir": ".loopplane/results",
        "runtime_dir": ".loopplane/runtime",
        "read_models_dir": ".loopplane/read_models",
        "requests_dir": ".loopplane/requests",
        "planning_dir": ".loopplane/planning",
        "version_control_config_file": ".loopplane/config/version_control.json",
    }
)


class WorkflowPathError(ValueError):
    pass


class PathSerializationError(WorkflowPathError):
    pass


@dataclass(frozen=True)
class WorkflowRootResolution:
    project_root: Path
    workspace_root_value: str
    workflow_root_value: str
    workflow_id: str | None
    workflow_config_file_value: str
    registry_record: Mapping[str, Any] | None
    source: str

    @property
    def workspace_root(self) -> Path:
        return self.project_root / self.workspace_root_value

    @property
    def workflow_root(self) -> Path:
        return self.project_root / self.workflow_root_value

    @property
    def workflow_config_file(self) -> Path:
        return self.project_root / self.workflow_config_file_value


@dataclass(frozen=True)
class WorkflowPaths:
    project_root: Path
    workspace_root_value: str
    workflow_root_value: str
    workflow_id: str | None
    workflow_config_file_value: str
    values: Mapping[str, str]
    resolved: Mapping[str, Path]

    @classmethod
    def from_config(
        cls,
        project_root: Path,
        workflow_config: Mapping[str, Any],
        *,
        use_defaults: bool = True,
    ) -> "WorkflowPaths":
        root = project_root.expanduser().resolve()
        roots = workflow_roots_from_config(root, workflow_config)
        values = workflow_path_values(
            workflow_config,
            use_defaults=use_defaults,
            workspace_root=roots.workspace_root_value,
            workflow_root=roots.workflow_root_value,
            workflow_id=roots.workflow_id,
        )
        resolved = {field: root / value for field, value in values.items()}
        return cls(
            project_root=root,
            workspace_root_value=roots.workspace_root_value,
            workflow_root_value=roots.workflow_root_value,
            workflow_id=roots.workflow_id,
            workflow_config_file_value=roots.workflow_config_file_value,
            values=MappingProxyType(values),
            resolved=MappingProxyType(resolved),
        )

    def value(self, field: str) -> str:
        try:
            return self.values[field]
        except KeyError as error:
            raise WorkflowPathError(f"unknown workflow path field: {field}") from error

    def path(self, field: str) -> Path:
        try:
            return self.resolved[field]
        except KeyError as error:
            raise WorkflowPathError(f"unknown workflow path field: {field}") from error

    def template_variables(self) -> dict[str, str]:
        variables = {
            "project_root": ".",
            "workspace_root": self.workspace_root_value,
            "workflow_root": self.workflow_root_value,
            **dict(self.values),
        }
        if self.workflow_id:
            variables["workflow_id"] = self.workflow_id
        return variables

    @property
    def workspace_root(self) -> Path:
        return self.project_root / self.workspace_root_value

    @property
    def workflow_root(self) -> Path:
        return self.project_root / self.workflow_root_value

    @property
    def workflow_config_file(self) -> Path:
        return self.project_root / self.workflow_config_file_value

    @property
    def workflow_config_dir_value(self) -> str:
        return PurePosixPath(self.workflow_config_file_value).parent.as_posix()

    @property
    def workflow_config_dir(self) -> Path:
        return self.project_root / self.workflow_config_dir_value

    def config_file_value(self, filename: str) -> str:
        if "/" in filename or "\\" in filename or not filename:
            raise WorkflowPathError(f"config filename must be a simple file name: {filename!r}")
        return (PurePosixPath(self.workflow_config_dir_value) / filename).as_posix()

    def config_file(self, filename: str) -> Path:
        return self.project_root / self.config_file_value(filename)

    @property
    def brief_file(self) -> Path:
        return self.path("brief_file")

    @property
    def plan_file(self) -> Path:
        return self.path("plan_file")

    @property
    def shared_context_file(self) -> Path:
        return self.path("shared_context_file")

    @property
    def results_dir(self) -> Path:
        return self.path("results_dir")

    @property
    def runtime_dir(self) -> Path:
        return self.path("runtime_dir")

    @property
    def read_models_dir(self) -> Path:
        return self.path("read_models_dir")

    @property
    def requests_dir(self) -> Path:
        return self.path("requests_dir")

    @property
    def planning_dir(self) -> Path:
        return self.path("planning_dir")

    @property
    def version_control_config_file(self) -> Path:
        return self.path("version_control_config_file")


def load_workflow_config(project_root: Path, *, workflow_id: str | None = None) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    resolution = resolve_current_workflow_roots(project, workflow_id=workflow_id)
    required = resolution.source == "compatibility_config" or resolution.workflow_root_value == COMPATIBILITY_WORKFLOW_ROOT_VALUE
    data = _load_workflow_json(resolution.workflow_config_file, required=required)
    return _workflow_config_from_resolution(resolution, data)


def resolve_current_workflow_roots(
    project_root: Path,
    *,
    workflow_id: str | None = None,
) -> WorkflowRootResolution:
    project = project_root.expanduser().resolve()
    current_file = project / CURRENT_WORKFLOW_PATH.as_posix()
    registry_file = project / WORKFLOW_REGISTRY_PATH.as_posix()

    if not current_file.exists() and not registry_file.exists():
        return WorkflowRootResolution(
            project_root=project,
            workspace_root_value=WORKSPACE_ROOT_VALUE,
            workflow_root_value=COMPATIBILITY_WORKFLOW_ROOT_VALUE,
            workflow_id=workflow_id,
            workflow_config_file_value=WORKFLOW_CONFIG_PATH.as_posix(),
            registry_record=None,
            source="compatibility_config",
        )
    if not current_file.is_file() or not registry_file.is_file():
        missing = []
        if not current_file.is_file():
            missing.append(CURRENT_WORKFLOW_PATH.as_posix())
        if not registry_file.is_file():
            missing.append(WORKFLOW_REGISTRY_PATH.as_posix())
        raise WorkflowPathError(
            "v1.6 workflow resolution requires both current workflow and registry files; "
            f"missing: {', '.join(missing)}"
        )

    current = _load_json_object(current_file)
    registry = _load_json_object(registry_file)
    current_workflow_id = current.get("current_workflow_id")
    if not isinstance(current_workflow_id, str) or not current_workflow_id.strip():
        raise WorkflowPathError(f"{CURRENT_WORKFLOW_PATH.as_posix()}: current_workflow_id must be a non-empty string")
    current_workflow_id = current_workflow_id.strip()
    if workflow_id is not None and not str(workflow_id).strip():
        raise WorkflowPathError("workflow_id must be a non-empty string")
    selected_workflow_id = str(workflow_id).strip() if workflow_id is not None else current_workflow_id
    if not isinstance(selected_workflow_id, str) or not selected_workflow_id.strip():
        raise WorkflowPathError(f"{CURRENT_WORKFLOW_PATH.as_posix()}: current_workflow_id must be a non-empty string")
    selected_workflow_id = selected_workflow_id.strip()

    workflows = registry.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        raise WorkflowPathError(f"{WORKFLOW_REGISTRY_PATH.as_posix()}: workflows must be an array")
    if not any(
        isinstance(candidate, Mapping) and candidate.get("workflow_id") == current_workflow_id
        for candidate in workflows
    ):
        raise WorkflowPathError(
            f"{CURRENT_WORKFLOW_PATH.as_posix()}: current_workflow_id {current_workflow_id!r} "
            f"does not reference a workflow in {WORKFLOW_REGISTRY_PATH.as_posix()}"
        )
    record = next(
        (
            dict(candidate)
            for candidate in workflows
            if isinstance(candidate, Mapping) and candidate.get("workflow_id") == selected_workflow_id
        ),
        None,
    )
    if record is None:
        raise WorkflowPathError(
            f"{WORKFLOW_REGISTRY_PATH.as_posix()}: workflow {selected_workflow_id!r} is not registered"
        )

    workflow_root = _normalize_root_value(
        "workflow_root",
        record.get("workflow_root") or COMPATIBILITY_WORKFLOW_ROOT_VALUE,
        workflow_id=selected_workflow_id,
    )
    config_file_value = record.get("workflow_config_file")
    if not isinstance(config_file_value, str) or not config_file_value.strip():
        config_file_value = f"{workflow_root}/config/workflow.json"
    config_file_value = _normalize_project_relative_posix_path(
        "workflow_config_file",
        _expand_path_templates(
            config_file_value,
            workspace_root=WORKSPACE_ROOT_VALUE,
            workflow_root=workflow_root,
            workflow_id=selected_workflow_id,
        ),
    )
    return WorkflowRootResolution(
        project_root=project,
        workspace_root_value=WORKSPACE_ROOT_VALUE,
        workflow_root_value=workflow_root,
        workflow_id=selected_workflow_id,
        workflow_config_file_value=config_file_value,
        registry_record=MappingProxyType(record),
        source="v1.6_metadata",
    )


def workflow_roots_from_config(project_root: Path, workflow_config: Mapping[str, Any]) -> WorkflowRootResolution:
    root = project_root.expanduser().resolve()
    workflow_id = workflow_config.get("workflow_id")
    normalized_workflow_id = workflow_id.strip() if isinstance(workflow_id, str) and workflow_id.strip() else None
    workspace_root = _normalize_root_value(
        "workspace_root",
        workflow_config.get("workspace_root") or WORKSPACE_ROOT_VALUE,
        workflow_id=normalized_workflow_id,
    )
    workflow_root = _normalize_root_value(
        "workflow_root",
        workflow_config.get("workflow_root") or COMPATIBILITY_WORKFLOW_ROOT_VALUE,
        workspace_root=workspace_root,
        workflow_id=normalized_workflow_id,
    )
    config_file = workflow_config.get("workflow_config_file")
    if not isinstance(config_file, str) or not config_file.strip():
        config_file = f"{workflow_root}/config/workflow.json"
    config_file = _normalize_project_relative_posix_path(
        "workflow_config_file",
        _expand_path_templates(
            config_file,
            workspace_root=workspace_root,
            workflow_root=workflow_root,
            workflow_id=normalized_workflow_id,
        ),
    )
    return WorkflowRootResolution(
        project_root=root,
        workspace_root_value=workspace_root,
        workflow_root_value=workflow_root,
        workflow_id=normalized_workflow_id,
        workflow_config_file_value=config_file,
        registry_record=None,
        source="workflow_config",
    )


def workflow_path_values(
    workflow_config: Mapping[str, Any],
    *,
    use_defaults: bool = True,
    workspace_root: str | None = None,
    workflow_root: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, str]:
    if workspace_root is None or workflow_root is None:
        roots = workflow_roots_from_config(Path("."), workflow_config)
        workspace_root = workspace_root or roots.workspace_root_value
        workflow_root = workflow_root or roots.workflow_root_value
        workflow_id = workflow_id or roots.workflow_id
    if workflow_id is None:
        raw_workflow_id = workflow_config.get("workflow_id")
        workflow_id = raw_workflow_id.strip() if isinstance(raw_workflow_id, str) and raw_workflow_id.strip() else None
    defaults = default_workflow_path_values(workflow_root=workflow_root)
    values: dict[str, str] = {}
    for field in WORKFLOW_PATH_FIELDS:
        raw_value = workflow_config.get(field)
        if raw_value is None and use_defaults:
            raw_value = defaults[field]
        expanded = _expand_path_templates(
            raw_value,
            workspace_root=workspace_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
        values[field] = _normalize_project_relative_posix_path(field, expanded)
    return values


def default_workflow_path_values(*, workflow_root: str = COMPATIBILITY_WORKFLOW_ROOT_VALUE) -> dict[str, str]:
    root = _normalize_root_value("workflow_root", workflow_root)
    if root == COMPATIBILITY_WORKFLOW_ROOT_VALUE:
        return dict(DEFAULT_WORKFLOW_PATHS)
    return {
        "brief_file": f"{root}/PROJECT_BRIEF.md",
        "plan_file": f"{root}/PLAN.md",
        "shared_context_file": f"{root}/SHARED_CONTEXT.md",
        "results_dir": f"{root}/results",
        "runtime_dir": f"{root}/runtime",
        "read_models_dir": f"{root}/read_models",
        "requests_dir": f"{root}/requests",
        "planning_dir": f"{root}/planning",
        "version_control_config_file": f"{root}/config/version_control.json",
    }


def path_lines(paths: WorkflowPaths) -> str:
    return "\n".join(f"- {field}: {paths.value(field)}" for field in WORKFLOW_PATH_FIELDS)


def serialize_project_path(
    project_root: Path | str,
    path: Path | str,
    *,
    allow_absolute: bool = False,
) -> str:
    """Return a migration-safe stored path label.

    Project-local paths are serialized as project-root-relative POSIX strings.
    Paths outside the project root are rejected unless explicitly allowed for
    machine-local records such as adapter logs.
    """

    project = Path(project_root).expanduser().resolve()
    if isinstance(path, Path):
        candidate = path.expanduser()
        if candidate.is_absolute():
            return _serialize_absolute_project_path(project, candidate, allow_absolute=allow_absolute)
        return _normalize_stored_project_path("path", candidate.as_posix())

    text = str(path).strip()
    if not text:
        raise PathSerializationError("path must be a non-empty string")
    if _looks_like_windows_absolute_path(text):
        if allow_absolute:
            return text.replace("\\", "/")
        raise PathSerializationError(f"path must stay inside the project root: {text}")

    normalized_text = text.replace("\\", "/")
    candidate = Path(normalized_text).expanduser()
    if candidate.is_absolute():
        return _serialize_absolute_project_path(project, candidate, allow_absolute=allow_absolute)
    return _normalize_stored_project_path("path", normalized_text)


def deserialize_project_path(
    project_root: Path | str,
    stored_path: str,
    *,
    allow_absolute: bool = False,
) -> Path:
    """Resolve a stored project path back to a local filesystem path."""

    project = Path(project_root).expanduser().resolve()
    text = str(stored_path).strip()
    if not text:
        raise PathSerializationError("stored path must be a non-empty string")
    if _looks_like_windows_absolute_path(text):
        if allow_absolute:
            return Path(text.replace("\\", "/"))
        raise PathSerializationError(f"stored path must stay inside the project root: {text}")

    normalized_text = text.replace("\\", "/")
    candidate = Path(normalized_text).expanduser()
    if candidate.is_absolute():
        if allow_absolute:
            return candidate
        try:
            relative = candidate.resolve().relative_to(project)
        except (OSError, ValueError) as error:
            raise PathSerializationError(f"stored path must stay inside the project root: {text}") from error
        return project / relative
    return project / _normalize_stored_project_path("stored path", normalized_text)


def _normalize_project_relative_posix_path(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowPathError(f"{field} must be a non-empty string")
    if "\\" in value:
        raise WorkflowPathError(f"{field} must use POSIX-style '/' separators: {value}")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise WorkflowPathError(f"{field} must be project-root-relative: {value}")
    if path == PurePosixPath(".") or ".." in path.parts:
        raise WorkflowPathError(f"{field} must stay inside the project root: {value}")

    return path.as_posix()


def _serialize_absolute_project_path(project: Path, path: Path, *, allow_absolute: bool) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except (OSError, ValueError) as error:
        if allow_absolute:
            return path.as_posix()
        raise PathSerializationError(f"path must stay inside the project root: {path.as_posix()}") from error


def _normalize_stored_project_path(field: str, value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute():
        raise PathSerializationError(f"{field} must be project-root-relative: {value}")
    if ".." in path.parts:
        raise PathSerializationError(f"{field} must stay inside the project root: {value}")
    return path.as_posix()


def _looks_like_windows_absolute_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\") or value.startswith("//")


def _workflow_config_from_resolution(
    resolution: WorkflowRootResolution,
    loaded: Mapping[str, Any],
) -> dict[str, Any]:
    config: dict[str, Any] = default_workflow_path_values(workflow_root=resolution.workflow_root_value)
    record = resolution.registry_record
    if record is not None:
        for field in WORKFLOW_PATH_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                config[field] = value
    config.update(dict(loaded))
    config["workspace_root"] = resolution.workspace_root_value
    config["workflow_root"] = resolution.workflow_root_value
    config["workflow_config_file"] = resolution.workflow_config_file_value
    if resolution.workflow_id:
        config["workflow_id"] = resolution.workflow_id
    return config


def _load_workflow_json(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise WorkflowPathError(f"{path}: workflow config file is missing")
        return {}
    return _load_json_object(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise WorkflowPathError(f"{path}: JSON value must be an object")
    return data


def _normalize_root_value(
    field: str,
    value: object,
    *,
    workspace_root: str = WORKSPACE_ROOT_VALUE,
    workflow_id: str | None = None,
) -> str:
    expanded = _expand_path_templates(
        value,
        workspace_root=workspace_root,
        workflow_root=COMPATIBILITY_WORKFLOW_ROOT_VALUE,
        workflow_id=workflow_id,
    )
    return _normalize_project_relative_posix_path(field, expanded)


def _expand_path_templates(
    value: object,
    *,
    workspace_root: str,
    workflow_root: str,
    workflow_id: str | None,
) -> object:
    if not isinstance(value, str):
        return value
    replacements = {
        "project_root": ".",
        "workspace_root": workspace_root,
        "workflow_root": workflow_root,
    }
    if workflow_id:
        replacements["workflow_id"] = workflow_id
    expanded = value
    for key, replacement in replacements.items():
        expanded = expanded.replace(f"{{{{{key}}}}}", replacement)
        expanded = expanded.replace(f"{{{key}}}", replacement)
    if "{" in expanded or "}" in expanded:
        raise WorkflowPathError(f"path contains unresolved template variable: {value}")
    return expanded
