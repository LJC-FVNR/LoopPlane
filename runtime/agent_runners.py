from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from runtime.loopplane_home import loopplane_home_layout
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config


SCHEMA_VERSION = "1.5"
AGENT_RUNNERS_CONFIG_PATH = PurePosixPath(".loopplane/config/agent_runners.json")
AGENT_RUNNERS_LOCAL_CONFIG_PATH = PurePosixPath(".loopplane/config/local/agent_runners.local.json")
LOCAL_SCHEMA_VERSION = "1.6"
LOCAL_AUTHORITY = "machine_local"
PRIMARY_AGENT_RUNNER_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("planner", "planner", "base"),
    ("auditor", "auditor", "planner"),
    ("validator", "validator", "planner"),
    ("change_request_planner", "change_request_planner", "planner"),
    ("expansion_planner", "expansion_planner", "planner"),
    ("objective_verifier", "objective_verifier", "planner"),
    ("summary", "summary", "base"),
    ("final_reviewer", "final_reviewer", "planner"),
    ("inspector", "inspector", "base"),
)
CLAUDE_AGENT_RUNNER_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("claude_planner", "planner"),
    ("claude_auditor", "auditor"),
    ("claude_validator", "validator"),
    ("claude_change_request_planner", "change_request_planner"),
    ("claude_expansion_planner", "expansion_planner"),
    ("claude_objective_verifier", "objective_verifier"),
    ("claude_summary", "summary"),
    ("claude_final_reviewer", "final_reviewer"),
    ("claude_inspector", "inspector"),
)
FALLBACK_AGENT_RUNNER_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("planner_fallback", "planner"),
    ("auditor_fallback", "auditor"),
    ("validator_fallback", "validator"),
    ("change_request_planner_fallback", "change_request_planner"),
    ("expansion_planner_fallback", "expansion_planner"),
    ("objective_verifier_fallback", "objective_verifier"),
    ("summary_fallback", "summary"),
    ("final_reviewer_fallback", "final_reviewer"),
    ("inspector_fallback", "inspector"),
)
LEGACY_RUNNER_ID_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "codex_worker": "worker",
        "claude_worker": "worker_fallback",
        "claude_planner": "planner_fallback",
        "claude_auditor": "auditor_fallback",
        "claude_validator": "validator_fallback",
        "claude_change_request_planner": "change_request_planner_fallback",
        "claude_expansion_planner": "expansion_planner_fallback",
        "claude_objective_verifier": "objective_verifier_fallback",
        "claude_summary": "summary_fallback",
        "claude_final_reviewer": "final_reviewer_fallback",
        "claude_inspector": "inspector_fallback",
    }
)

PROMPT_DELIVERY_MODES = frozenset(
    {
        "file_argument",
        "stdin",
        "stdin_or_prompt_flag",
        "interactive_terminal",
        "custom_adapter",
    }
)
RESOURCE_LOCK_SCOPES = frozenset({"machine", "workspace"})
RESOURCE_POLICY_FIELDS = frozenset(
    {
        "global_concurrency_limit",
        "lock_scope",
        "lock_key",
        "queue_when_busy",
    }
)
RESOURCE_LOCK_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")

RUNNER_FIELDS = frozenset(
    {
        "role",
        "adapter",
        "command",
        "cwd",
        "prompt_delivery",
        "args",
        "env",
        "timeout_seconds",
        "stream_logs",
        "permission_policy",
        "adapter_options",
        "doctor",
        "inherits",
        "enabled",
        "resource_policy",
    }
)
LOCAL_RUNNER_OVERRIDE_FIELDS = RUNNER_FIELDS - {"inherits"}

REQUIRED_RESOLVED_RUNNER_FIELDS = (
    "role",
    "adapter",
    "command",
    "cwd",
    "prompt_delivery",
    "args",
    "env",
    "timeout_seconds",
    "stream_logs",
    "permission_policy",
    "doctor",
    "enabled",
)

REQUIRED_PERMISSION_POLICY_FIELDS = (
    "allow_project_file_edit",
    "allow_command_execution",
    "require_approval_for_risky_commands",
    "read_only",
)


class AgentRunnerConfigError(ValueError):
    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        self.errors = tuple(errors or [message])
        super().__init__(message)


@dataclass(frozen=True)
class RunnerConfig:
    runner_id: str
    role: str
    adapter: str
    command: str
    cwd: str
    prompt_delivery: Mapping[str, Any]
    args: tuple[str, ...]
    env: Mapping[str, str]
    timeout_seconds: int
    stream_logs: bool
    permission_policy: Mapping[str, Any]
    doctor: Mapping[str, Any]
    enabled: bool
    resource_policy: Mapping[str, Any] | None = None
    inherits: str | None = None
    adapter_options: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self, *, include_inherits: bool = True) -> dict[str, Any]:
        data = {
            "role": self.role,
            "adapter": self.adapter,
            "enabled": self.enabled,
            "command": self.command,
            "cwd": self.cwd,
            "prompt_delivery": _thaw(self.prompt_delivery),
            "args": list(self.args),
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "stream_logs": self.stream_logs,
            "permission_policy": _thaw(self.permission_policy),
            "adapter_options": _thaw(self.adapter_options),
            "doctor": _thaw(self.doctor),
        }
        if self.resource_policy is not None:
            data["resource_policy"] = _thaw(self.resource_policy)
        if include_inherits and self.inherits is not None:
            data["inherits"] = self.inherits
        return data


@dataclass(frozen=True)
class AgentRunnersConfig:
    schema_version: str
    default_runner: str
    runners: Mapping[str, RunnerConfig]
    runner_failover: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    config_path: Path | None = None
    template_variables: Mapping[str, str] = MappingProxyType({"project_root": "."})
    local_override_paths: tuple[Path, ...] = ()
    local_override_runner_ids: tuple[str, ...] = ()

    def runner(self, runner_id: str | None = None) -> RunnerConfig:
        selected = runner_id or self.default_runner
        try:
            return self.runners[selected]
        except KeyError as error:
            raise AgentRunnerConfigError(f"unknown runner: {selected}") from error

    def runners_for_role(self, role: str, *, enabled_only: bool = False) -> tuple[RunnerConfig, ...]:
        matches = [
            runner
            for runner in self.runners.values()
            if runner.role == role and (runner.enabled or not enabled_only)
        ]
        return tuple(matches)


def load_agent_runners(project_root: Path) -> AgentRunnersConfig:
    project = project_root.expanduser().resolve()
    try:
        workflow_config = load_workflow_config(project)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        raise AgentRunnerConfigError(f"{project}: unable to load workflow.json: {error}") from error
    paths = WorkflowPaths.from_config(project, workflow_config)
    return load_agent_runners_file(
        paths.config_file("agent_runners.json"),
        project_root=project,
        workflow_config=workflow_config,
        merge_local_overrides=True,
    )


def load_agent_runners_file(
    config_file: Path,
    *,
    project_root: Path | None = None,
    workflow_config: Mapping[str, Any] | None = None,
    merge_local_overrides: bool = False,
) -> AgentRunnersConfig:
    config_path = config_file.expanduser().resolve()
    data = _with_implicit_agent_runners(_load_json_object(config_path))
    local_summary: dict[str, Any] = {"paths": (), "runner_ids": ()}
    if merge_local_overrides:
        if project_root is None:
            raise AgentRunnerConfigError("project_root is required when merging local runner overrides")
        data, local_summary = merge_agent_runner_local_overrides(data, project_root=project_root)
    template_variables = _template_variables(project_root, workflow_config)
    config = parse_agent_runners_config(
        data,
        config_path=config_path,
        template_variables=template_variables,
    )
    return AgentRunnersConfig(
        schema_version=config.schema_version,
        default_runner=config.default_runner,
        runners=config.runners,
        runner_failover=config.runner_failover,
        config_path=config.config_path,
        template_variables=config.template_variables,
        local_override_paths=tuple(Path(path) for path in local_summary["paths"]),
        local_override_runner_ids=tuple(str(runner_id) for runner_id in local_summary["runner_ids"]),
    )


def merge_agent_runner_local_overrides(
    data: Mapping[str, Any],
    *,
    project_root: Path,
    extra_project_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Overlay machine-local runner settings onto portable runner defaults."""

    project = project_root.expanduser().resolve()
    merged = _with_implicit_agent_runners(data)
    runners = merged.get("runners")
    if not isinstance(runners, dict):
        return merged, {"paths": (), "runner_ids": ()}

    paths: list[Path] = []
    runner_ids: set[str] = set()
    for path, overrides in _local_override_sources(project):
        if not path.exists():
            continue
        paths.append(path)
        runner_ids.update(_apply_local_overrides(runners, overrides, path))

    if extra_project_overrides:
        runner_ids.update(_apply_local_overrides(runners, extra_project_overrides, Path("<pending local override>")))

    return merged, {"paths": tuple(paths), "runner_ids": tuple(sorted(runner_ids))}


def project_local_agent_runner_override_file(project_root: Path) -> Path:
    return project_root.expanduser().resolve() / AGENT_RUNNERS_LOCAL_CONFIG_PATH.as_posix()


def write_loopplane_home_agent_runner_override(
    project_root: Path,
    runner_id: str,
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Write or update the project-scoped LOOPPLANE_HOME runner override."""

    project = project_root.expanduser().resolve()
    layout = loopplane_home_layout()
    path = layout.agent_runners_local_file
    payload = _load_or_default_local_override_file(path)
    prune_result = _prune_stale_project_overrides(payload, keep_project=project)
    projects = payload.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise AgentRunnerConfigError(f"{path}: projects must be an object")
    project_key = agent_runner_project_key(project)
    project_record = projects.setdefault(project_key, {})
    if not isinstance(project_record, dict):
        raise AgentRunnerConfigError(f"{path}: projects.{project_key} must be an object")
    project_record["project_root"] = project.as_posix()
    project_record["authority"] = LOCAL_AUTHORITY
    project_runners = project_record.setdefault("runners", {})
    if not isinstance(project_runners, dict):
        raise AgentRunnerConfigError(f"{path}: projects.{project_key}.runners must be an object")
    existing = project_runners.get(runner_id)
    if existing is None:
        existing = {}
    if not isinstance(existing, Mapping):
        raise AgentRunnerConfigError(f"{path}: local override for runner {runner_id!r} must be an object")
    merged_override = _merge_local_runner_override(existing, override)
    _validate_local_override_runner(runner_id, merged_override, path)
    project_runners[runner_id] = merged_override
    _write_json_atomic(path, payload)
    return {
        "path": path,
        "scope": "loopplane_home_project",
        "project_key": project_key,
        "runner_id": runner_id,
        "pruned_projects": prune_result["pruned_projects"],
        "pruned_project_keys": prune_result["pruned_project_keys"],
    }


def prune_loopplane_home_agent_runner_overrides(*, keep_project: Path | None = None) -> dict[str, Any]:
    """Remove LOOPPLANE_HOME runner overrides for project roots that no longer exist."""

    keep = keep_project.expanduser().resolve() if keep_project is not None else None
    path = loopplane_home_layout().agent_runners_local_file
    payload = _load_or_default_local_override_file(path)
    prune_result = _prune_stale_project_overrides(payload, keep_project=keep)
    if prune_result["pruned_projects"]:
        _write_json_atomic(path, payload)
    return {
        "path": path,
        "scope": "loopplane_home_project",
        "pruned_projects": prune_result["pruned_projects"],
        "pruned_project_keys": prune_result["pruned_project_keys"],
    }


def write_project_local_agent_runner_overrides(
    project_root: Path,
    overrides: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Write project-local machine runner overrides for install-time bootstrap."""

    project = project_root.expanduser().resolve()
    path = project_local_agent_runner_override_file(project)
    payload = _load_or_default_local_override_file(path)
    runners = payload.setdefault("runners", {})
    if not isinstance(runners, dict):
        raise AgentRunnerConfigError(f"{path}: runners must be an object")
    for runner_id, override in overrides.items():
        if not isinstance(runner_id, str) or not runner_id:
            raise AgentRunnerConfigError(f"{path}: local override runner ids must be non-empty strings")
        if not isinstance(override, Mapping):
            raise AgentRunnerConfigError(f"{path}: local override for runner {runner_id!r} must be an object")
        _validate_local_override_runner(runner_id, override, path)
        runners[runner_id] = copy.deepcopy(dict(override))
    _write_json_atomic(path, payload)
    return {
        "path": path,
        "scope": "project_local",
        "runner_ids": sorted(str(runner_id) for runner_id in overrides),
    }


def remove_project_local_agent_runner_override(
    project_root: Path,
    runner_id: str,
) -> dict[str, Any]:
    """Remove a project-local runner override that would shadow LOOPPLANE_HOME."""

    project = project_root.expanduser().resolve()
    path = project_local_agent_runner_override_file(project)
    if not path.exists():
        return {"path": path, "scope": "project_local", "runner_id": runner_id, "removed": False}
    payload = _load_or_default_local_override_file(path)
    runners = payload.get("runners")
    if not isinstance(runners, dict):
        raise AgentRunnerConfigError(f"{path}: runners must be an object")
    removed = runners.pop(runner_id, None) is not None
    if removed:
        _write_json_atomic(path, payload)
    return {"path": path, "scope": "project_local", "runner_id": runner_id, "removed": removed}


def agent_runner_project_key(project_root: Path) -> str:
    import hashlib

    project = project_root.expanduser().resolve().as_posix()
    return "project_" + hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]


def parse_agent_runners_config(
    data: Mapping[str, Any],
    *,
    config_path: Path | None = None,
    template_variables: Mapping[str, str] | None = None,
) -> AgentRunnersConfig:
    data = _with_implicit_agent_runners(data)
    errors = _top_level_errors(data)
    if errors:
        _raise_validation(errors, config_path)

    runners_data = data["runners"]
    assert isinstance(runners_data, Mapping)
    resolved = _resolve_all_runners(runners_data, config_path)
    runners = {
        runner_id: _runner_from_mapping(runner_id, runner, config_path)
        for runner_id, runner in resolved.items()
    }
    default_runner = str(data["default_runner"])
    if default_runner not in runners:
        _raise_validation([f"default_runner {default_runner!r} is not defined in runners"], config_path)

    return AgentRunnersConfig(
        schema_version=str(data["schema_version"]),
        default_runner=default_runner,
        runners=MappingProxyType(runners),
        runner_failover=_freeze_mapping(data.get("runner_failover", {})),
        config_path=config_path,
        template_variables=MappingProxyType(dict(template_variables or {"project_root": "."})),
    )


def _with_implicit_agent_runners(data: Mapping[str, Any]) -> dict[str, Any]:
    """Add standard role runners that inherit the configured base CLI runner."""

    expanded = copy.deepcopy(dict(data))
    runners = expanded.get("runners")
    if not isinstance(runners, Mapping):
        return expanded
    mutable_runners = copy.deepcopy(dict(runners))
    default_runner = expanded.get("default_runner")
    base_runner_id = _primary_agent_base_runner_id(mutable_runners, default_runner)
    if base_runner_id is not None:
        _add_primary_agent_runners(mutable_runners, base_runner_id)
    for fallback_base_id, fallback_defaults in (
        ("worker_fallback", FALLBACK_AGENT_RUNNER_DEFAULTS),
        ("claude_worker", CLAUDE_AGENT_RUNNER_DEFAULTS),
    ):
        if fallback_base_id in mutable_runners:
            _add_agent_runners_for_base(mutable_runners, fallback_base_id, fallback_defaults)
    expanded["runners"] = mutable_runners
    return expanded


def _primary_agent_base_runner_id(runners: Mapping[str, Any], default_runner: Any) -> str | None:
    if isinstance(default_runner, str) and default_runner in runners:
        return default_runner
    for runner_id, runner in runners.items():
        if (
            isinstance(runner_id, str)
            and isinstance(runner, Mapping)
            and str(runner.get("role") or "") == "worker"
            and runner.get("enabled") is True
        ):
            return runner_id
    for runner_id, runner in runners.items():
        if isinstance(runner_id, str) and isinstance(runner, Mapping) and str(runner.get("role") or "") == "worker":
            return runner_id
    return None


def _add_primary_agent_runners(runners: dict[str, Any], base_runner_id: str) -> None:
    for runner_id, role, parent_kind in PRIMARY_AGENT_RUNNER_DEFAULTS:
        if runner_id in runners:
            continue
        parent_id = base_runner_id if parent_kind == "base" else parent_kind
        if parent_id not in runners:
            parent_id = base_runner_id
        runners[runner_id] = {
            "inherits": parent_id,
            "role": role,
        }


def _add_agent_runners_for_base(
    runners: dict[str, Any],
    base_runner_id: str,
    defaults: tuple[tuple[str, str], ...],
) -> None:
    for runner_id, role in defaults:
        if runner_id in runners:
            continue
        runners[runner_id] = {
            "inherits": base_runner_id,
            "role": role,
        }


def _load_json_object(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise AgentRunnerConfigError(f"{path}: unable to read agent runner config: {error}") from error
    except json.JSONDecodeError as error:
        raise AgentRunnerConfigError(f"{path}: invalid JSON: {error.msg}") from error
    if not isinstance(data, Mapping):
        raise AgentRunnerConfigError(f"{path}: agent runner config must be a JSON object")
    return data


def _load_or_default_local_override_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": LOCAL_SCHEMA_VERSION,
            "authority": LOCAL_AUTHORITY,
            "runners": {},
            "projects": {},
        }
    data = _load_json_object(path)
    _validate_local_override_file(path, data)
    return copy.deepcopy(dict(data))


def _prune_stale_project_overrides(
    payload: dict[str, Any],
    *,
    keep_project: Path | None,
) -> dict[str, Any]:
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        return {"pruned_projects": 0, "pruned_project_keys": []}
    pruned: list[str] = []
    keep = keep_project.resolve() if keep_project is not None else None
    for project_key, project_record in list(projects.items()):
        if not isinstance(project_record, Mapping):
            continue
        raw_root = project_record.get("project_root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            pruned.append(str(project_key))
            projects.pop(project_key, None)
            continue
        try:
            project_root = Path(raw_root).expanduser().resolve()
        except OSError:
            pruned.append(str(project_key))
            projects.pop(project_key, None)
            continue
        if keep is not None and project_root == keep:
            continue
        if not project_root.exists():
            pruned.append(str(project_key))
            projects.pop(project_key, None)
    return {"pruned_projects": len(pruned), "pruned_project_keys": sorted(pruned)}


def _local_override_sources(project: Path) -> tuple[tuple[Path, Mapping[str, Any]], ...]:
    sources: list[tuple[Path, Mapping[str, Any]]] = []
    for path in (loopplane_home_layout().agent_runners_local_file, project_local_agent_runner_override_file(project)):
        if not path.exists():
            continue
        data = _load_json_object(path)
        _validate_local_override_file(path, data)
        overrides = _runner_overrides_for_project(data, project, path)
        if overrides:
            sources.append((path, overrides))
    return tuple(sources)


def _validate_local_override_file(path: Path, data: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if data.get("schema_version") != LOCAL_SCHEMA_VERSION:
        errors.append(f"schema_version must be {LOCAL_SCHEMA_VERSION!r}")
    runners = data.get("runners", {})
    if not isinstance(runners, Mapping):
        errors.append("runners must be an object when present")
    projects = data.get("projects", {})
    if not isinstance(projects, Mapping):
        errors.append("projects must be an object when present")
    if errors:
        _raise_validation(errors, path)


def _runner_overrides_for_project(
    data: Mapping[str, Any],
    project: Path,
    path: Path,
) -> Mapping[str, Mapping[str, Any]]:
    overrides: dict[str, Mapping[str, Any]] = {}
    runners = data.get("runners", {})
    if isinstance(runners, Mapping):
        for runner_id, runner_override in runners.items():
            if not isinstance(runner_id, str):
                _raise_validation(["local override runner ids must be strings"], path)
            if not isinstance(runner_override, Mapping):
                _raise_validation([f"local override for runner {runner_id!r} must be an object"], path)
            _validate_local_override_runner(runner_id, runner_override, path)
            overrides[runner_id] = runner_override

    projects = data.get("projects", {})
    if isinstance(projects, Mapping):
        project_keys = (agent_runner_project_key(project), project.as_posix(), os.fspath(project))
        for project_key in project_keys:
            project_data = projects.get(project_key)
            if project_data is None:
                continue
            if not isinstance(project_data, Mapping):
                _raise_validation([f"projects.{project_key} must be an object"], path)
            project_runners = project_data.get("runners", {})
            if not isinstance(project_runners, Mapping):
                _raise_validation([f"projects.{project_key}.runners must be an object"], path)
            for runner_id, runner_override in project_runners.items():
                if not isinstance(runner_id, str):
                    _raise_validation([f"projects.{project_key}.runners keys must be strings"], path)
                if not isinstance(runner_override, Mapping):
                    _raise_validation([f"local override for runner {runner_id!r} must be an object"], path)
                _validate_local_override_runner(runner_id, runner_override, path)
                existing = overrides.get(runner_id, {})
                overrides[runner_id] = _merge_local_runner_override(existing, runner_override)
    return MappingProxyType(overrides)


def _validate_local_override_runner(
    runner_id: str,
    runner_override: Mapping[str, Any],
    path: Path,
) -> None:
    errors: list[str] = []
    unknown = sorted(set(runner_override) - LOCAL_RUNNER_OVERRIDE_FIELDS)
    if unknown:
        errors.append(f"runner {runner_id!r} local override has unknown fields: {', '.join(unknown)}")
    if "inherits" in runner_override:
        errors.append(f"runner {runner_id!r} local override cannot set inherits")
    if errors:
        _raise_validation(errors, path)


def _apply_local_overrides(
    runners: dict[str, Any],
    overrides: Mapping[str, Mapping[str, Any]],
    path: Path,
) -> set[str]:
    applied_runner_ids: set[str] = set()
    for runner_id, runner_override in overrides.items():
        target_runner_id = _local_override_target_runner_id(runners, runner_id)
        if target_runner_id is None:
            _raise_validation(
                [
                    (
                        f"local override for runner {runner_id!r} cannot define a runner "
                        "that is absent from portable agent_runners.json"
                    )
                ],
                path,
            )
        runner = runners.get(target_runner_id)
        if not isinstance(runner, Mapping):
            _raise_validation([f"runner {target_runner_id!r} must be an object"], path)
        runners[target_runner_id] = _merge_local_runner_override(runner, runner_override)
        applied_runner_ids.add(target_runner_id)
    return applied_runner_ids


def _local_override_target_runner_id(runners: Mapping[str, Any], runner_id: str) -> str | None:
    if runner_id in runners:
        return runner_id
    alias = LEGACY_RUNNER_ID_ALIASES.get(runner_id)
    if isinstance(alias, str) and alias in runners:
        return alias
    return None


def _merge_local_runner_override(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(parent, child)
    for replace_field in ("doctor", "prompt_delivery"):
        if replace_field in child:
            merged[replace_field] = copy.deepcopy(child[replace_field])
    return merged


def _top_level_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    schema_version = data.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if not _non_empty_string(data.get("default_runner")):
        errors.append("default_runner must be a non-empty string")
    runners = data.get("runners")
    if not isinstance(runners, Mapping) or not runners:
        errors.append("runners must be a non-empty object")
        return errors
    runner_failover = data.get("runner_failover", {})
    if runner_failover is not None and not isinstance(runner_failover, Mapping):
        errors.append("runner_failover must be an object when present")
    elif isinstance(runner_failover, Mapping):
        errors.extend(_runner_failover_errors(runner_failover, set(runners)))
    for runner_id, runner in runners.items():
        if not _non_empty_string(runner_id):
            errors.append("runner ids must be non-empty strings")
            continue
        if not isinstance(runner, Mapping):
            errors.append(f"runner {runner_id!r} must be an object")
            continue
        unknown = sorted(set(runner) - RUNNER_FIELDS)
        if unknown:
            errors.append(f"runner {runner_id!r} has unknown fields: {', '.join(unknown)}")
        inherits = runner.get("inherits")
        if inherits is not None and not _non_empty_string(inherits):
            errors.append(f"runner {runner_id!r} inherits must be a non-empty string")
    return errors


def _runner_failover_errors(runner_failover: Mapping[str, Any], defined_runner_ids: set[Any]) -> list[str]:
    errors: list[str] = []
    for role, rule in runner_failover.items():
        if not _non_empty_string(role):
            errors.append("runner_failover role keys must be non-empty strings")
            continue
        if not isinstance(rule, Mapping):
            errors.append(f"runner_failover.{role} must be an object")
            continue
        strategy = rule.get("strategy", "ordered")
        if strategy != "ordered":
            errors.append(f"runner_failover.{role}.strategy must be 'ordered'")
        runners = rule.get("runners")
        if (
            not isinstance(runners, list)
            or not runners
            or not all(isinstance(runner_id, str) and runner_id.strip() for runner_id in runners)
        ):
            errors.append(f"runner_failover.{role}.runners must be a non-empty list of runner ids")
        elif any(runner_id not in defined_runner_ids for runner_id in runners):
            missing = sorted(str(runner_id) for runner_id in runners if runner_id not in defined_runner_ids)
            errors.append(f"runner_failover.{role}.runners references unknown runners: {', '.join(missing)}")
        for field in ("mark_unhealthy_after", "failure_window_seconds"):
            if not _positive_int(rule.get(field)):
                errors.append(f"runner_failover.{role}.{field} must be a positive integer")
        if "cooldown_seconds" in rule and not _positive_int(rule.get("cooldown_seconds")):
            errors.append(f"runner_failover.{role}.cooldown_seconds must be a positive integer")
        if "recover_after_doctor_ok" in rule and not isinstance(rule.get("recover_after_doctor_ok"), bool):
            errors.append(f"runner_failover.{role}.recover_after_doctor_ok must be boolean")
    return errors


def _resolve_all_runners(
    runners_data: Mapping[str, Any],
    config_path: Path | None,
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(runner_id: str, stack: tuple[str, ...]) -> dict[str, Any]:
        if runner_id in resolved:
            return copy.deepcopy(resolved[runner_id])
        if runner_id in stack:
            cycle = " -> ".join((*stack, runner_id))
            _raise_validation([f"inheritance cycle detected: {cycle}"], config_path)
        raw = runners_data.get(runner_id)
        if not isinstance(raw, Mapping):
            _raise_validation([f"runner {runner_id!r} must be an object"], config_path)

        parent_id = raw.get("inherits")
        if parent_id is None:
            merged: dict[str, Any] = {}
        else:
            if not isinstance(parent_id, str) or parent_id not in runners_data:
                _raise_validation(
                    [f"runner {runner_id!r} inherits unknown parent {parent_id!r}"],
                    config_path,
                )
            merged = resolve(parent_id, (*stack, runner_id))

        child = {key: value for key, value in raw.items() if key != "inherits"}
        merged = _deep_merge(merged, child)
        if parent_id is not None:
            merged["inherits"] = parent_id

        errors = _resolved_runner_errors(runner_id, merged)
        if errors:
            _raise_validation(errors, config_path)
        resolved[runner_id] = copy.deepcopy(merged)
        return copy.deepcopy(merged)

    for runner_id in runners_data:
        if not isinstance(runner_id, str):
            continue
        resolve(runner_id, ())
    return resolved


def _deep_merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(parent))
    for key, value in child.items():
        parent_value = merged.get(key)
        if isinstance(parent_value, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(parent_value, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _resolved_runner_errors(runner_id: str, runner: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_RESOLVED_RUNNER_FIELDS:
        if field not in runner:
            errors.append(f"runner {runner_id!r} missing required field {field!r}")

    for field in ("role", "adapter", "command", "cwd"):
        if field in runner and not _non_empty_string(runner[field]):
            errors.append(f"runner {runner_id!r} field {field!r} must be a non-empty string")

    if "enabled" in runner and not isinstance(runner["enabled"], bool):
        errors.append(f"runner {runner_id!r} field 'enabled' must be boolean")
    if "stream_logs" in runner and not isinstance(runner["stream_logs"], bool):
        errors.append(f"runner {runner_id!r} field 'stream_logs' must be boolean")
    if "timeout_seconds" in runner and not _positive_int(runner["timeout_seconds"]):
        errors.append(f"runner {runner_id!r} field 'timeout_seconds' must be a positive integer")

    prompt_delivery = runner.get("prompt_delivery")
    if "prompt_delivery" in runner:
        if not isinstance(prompt_delivery, Mapping):
            errors.append(f"runner {runner_id!r} field 'prompt_delivery' must be an object")
        else:
            mode = prompt_delivery.get("mode")
            if mode not in PROMPT_DELIVERY_MODES:
                errors.append(
                    f"runner {runner_id!r} prompt_delivery.mode must be one of "
                    f"{', '.join(sorted(PROMPT_DELIVERY_MODES))}"
                )

    args = runner.get("args")
    if "args" in runner:
        if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
            errors.append(f"runner {runner_id!r} field 'args' must be a list of strings")

    env = runner.get("env")
    if "env" in runner:
        if not isinstance(env, Mapping):
            errors.append(f"runner {runner_id!r} field 'env' must be an object")
        else:
            for key, value in env.items():
                if not _non_empty_string(key) or not isinstance(value, str):
                    errors.append(f"runner {runner_id!r} field 'env' must map strings to strings")
                    break

    permission_policy = runner.get("permission_policy")
    if "permission_policy" in runner:
        if not isinstance(permission_policy, Mapping):
            errors.append(f"runner {runner_id!r} field 'permission_policy' must be an object")
        else:
            for field in REQUIRED_PERMISSION_POLICY_FIELDS:
                if field not in permission_policy:
                    errors.append(f"runner {runner_id!r} permission_policy missing {field!r}")
                elif not isinstance(permission_policy[field], bool):
                    errors.append(f"runner {runner_id!r} permission_policy.{field} must be boolean")

    adapter_options = runner.get("adapter_options")
    if "adapter_options" in runner and not isinstance(adapter_options, Mapping):
        errors.append(f"runner {runner_id!r} field 'adapter_options' must be an object")

    doctor = runner.get("doctor")
    if "doctor" in runner:
        if not isinstance(doctor, Mapping):
            errors.append(f"runner {runner_id!r} field 'doctor' must be an object")
        else:
            check_kind = str(doctor.get("check_kind") or "").strip().lower()
            if check_kind != "none" and not _non_empty_string(doctor.get("check_command")):
                errors.append(f"runner {runner_id!r} doctor.check_command must be a non-empty string")
            if not isinstance(doctor.get("requires_auth"), bool):
                errors.append(f"runner {runner_id!r} doctor.requires_auth must be boolean")

    resource_policy = runner.get("resource_policy")
    if "resource_policy" in runner:
        if not isinstance(resource_policy, Mapping):
            errors.append(f"runner {runner_id!r} field 'resource_policy' must be an object")
        else:
            errors.extend(_resource_policy_errors(runner_id, resource_policy))

    return errors


def _resource_policy_errors(runner_id: str, resource_policy: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(resource_policy) - RESOURCE_POLICY_FIELDS)
    if unknown:
        errors.append(f"runner {runner_id!r} resource_policy has unknown fields: {', '.join(unknown)}")
    for field in sorted(RESOURCE_POLICY_FIELDS - set(resource_policy)):
        errors.append(f"runner {runner_id!r} resource_policy missing {field!r}")

    limit = resource_policy.get("global_concurrency_limit")
    if "global_concurrency_limit" in resource_policy and not _positive_int(limit):
        errors.append(
            f"runner {runner_id!r} resource_policy.global_concurrency_limit must be a positive integer"
        )

    lock_scope = resource_policy.get("lock_scope")
    if "lock_scope" in resource_policy and lock_scope not in RESOURCE_LOCK_SCOPES:
        errors.append(
            f"runner {runner_id!r} resource_policy.lock_scope must be one of "
            f"{', '.join(sorted(RESOURCE_LOCK_SCOPES))}"
        )

    lock_key = resource_policy.get("lock_key")
    if "lock_key" in resource_policy:
        if (
            not isinstance(lock_key, str)
            or not lock_key.strip()
            or lock_key in {".", ".."}
            or RESOURCE_LOCK_KEY_RE.fullmatch(lock_key) is None
        ):
            errors.append(
                f"runner {runner_id!r} resource_policy.lock_key must be a filename-safe "
                f"non-empty string matching {RESOURCE_LOCK_KEY_RE.pattern!r}"
            )

    queue_when_busy = resource_policy.get("queue_when_busy")
    if "queue_when_busy" in resource_policy and not isinstance(queue_when_busy, bool):
        errors.append(f"runner {runner_id!r} resource_policy.queue_when_busy must be boolean")
    return errors


def _runner_from_mapping(
    runner_id: str,
    runner: Mapping[str, Any],
    config_path: Path | None,
) -> RunnerConfig:
    errors = _resolved_runner_errors(runner_id, runner)
    if errors:
        _raise_validation(errors, config_path)

    prompt_delivery = runner["prompt_delivery"]
    env = runner["env"]
    permission_policy = runner["permission_policy"]
    adapter_options = runner.get("adapter_options", {})
    doctor = runner["doctor"]
    resource_policy = runner.get("resource_policy")
    assert isinstance(prompt_delivery, Mapping)
    assert isinstance(env, Mapping)
    assert isinstance(permission_policy, Mapping)
    assert isinstance(adapter_options, Mapping)
    assert isinstance(doctor, Mapping)
    assert resource_policy is None or isinstance(resource_policy, Mapping)

    return RunnerConfig(
        runner_id=runner_id,
        role=str(runner["role"]),
        adapter=str(runner["adapter"]),
        command=str(runner["command"]),
        cwd=str(runner["cwd"]),
        prompt_delivery=_freeze_mapping(prompt_delivery),
        args=tuple(runner["args"]),
        env=MappingProxyType({str(key): str(value) for key, value in env.items()}),
        timeout_seconds=int(runner["timeout_seconds"]),
        stream_logs=bool(runner["stream_logs"]),
        permission_policy=_freeze_mapping(permission_policy),
        adapter_options=_freeze_mapping(adapter_options),
        doctor=_freeze_mapping(doctor),
        enabled=bool(runner["enabled"]),
        resource_policy=_freeze_mapping(resource_policy) if resource_policy is not None else None,
        inherits=runner.get("inherits") if isinstance(runner.get("inherits"), str) else None,
    )


def _template_variables(
    project_root: Path | None,
    workflow_config: Mapping[str, Any] | None,
) -> Mapping[str, str]:
    if project_root is None or workflow_config is None:
        return MappingProxyType({"project_root": "."})
    paths = WorkflowPaths.from_config(project_root, workflow_config)
    return MappingProxyType(paths.template_variables())


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _raise_validation(errors: list[str], config_path: Path | None) -> None:
    prefix = f"{config_path}: " if config_path is not None else ""
    raise AgentRunnerConfigError(prefix + "; ".join(errors), errors=errors)


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
