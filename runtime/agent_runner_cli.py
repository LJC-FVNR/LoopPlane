from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import DOCTOR_STATUS_OK, DOCTOR_STATUS_WAITING_CONFIG, AdapterInput
from runtime.adapters.registry import AdapterLookupError, available_adapter_names, get_adapter
from runtime.agent_runners import (
    AGENT_RUNNERS_CONFIG_PATH,
    AgentRunnerConfigError,
    AgentRunnersConfig,
    RunnerConfig,
    merge_agent_runner_local_overrides,
    parse_agent_runners_config,
    project_local_agent_runner_override_file,
    prune_loopplane_home_agent_runner_overrides,
    remove_project_local_agent_runner_override,
    write_loopplane_home_agent_runner_override,
    write_project_local_agent_runner_overrides,
)
from runtime.loopplane_home import loopplane_home_layout, ensure_loopplane_home_layout
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_RUNNER_UNAVAILABLE, EXIT_SUCCESS, has_text
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.runner_locks import machine_resource_policy_from_runner, runner_lock_doctor_check


AGENT_COMMAND_STATUS_OK = "ok"
AGENT_COMMAND_STATUS_WAITING_CONFIG = "waiting_config"

_ADAPTER_ALIASES = {
    "noop_adapter": "noop",
    "shell_adapter": "shell",
    "codex_cli_adapter": "codex_cli",
    "claude_code_cli_adapter": "claude_code_cli",
}
_AUTH_REQUIRED_ADAPTERS = {"codex_cli", "claude_code_cli"}
_CODEX_SANDBOX_VALUES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_REASONING_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh"})


def inspect_agent_configuration(
    project_root: Path | str,
    *,
    runner_id: str | None = None,
) -> dict[str, Any]:
    project = _project_path(project_root)
    loaded = _load_project_agent_config(project)
    if not loaded["ok"]:
        return loaded
    config: AgentRunnersConfig = loaded["config"]

    try:
        runners = _selected_runners(config, runner_id=runner_id, all_runners=runner_id is None)
    except AgentRunnerConfigError as error:
        return _failure(
            project,
            action="inspect",
            message=str(error),
            errors=list(error.errors),
            config_path=config.config_path,
        )

    return _success(
        project,
        action="inspect",
        config=config,
        selected_runner_ids=[runner.runner_id for runner in runners],
        mutated=False,
        runners={runner.runner_id: _runner_dict(runner) for runner in runners},
    )


def configure_agent_runner(
    project_root: Path | str,
    *,
    runner_id: str | None = None,
    role: str | None = None,
    adapter: str | None = None,
    command: str | None = None,
    args: Sequence[str] | None = None,
    cwd: str | None = None,
    prompt_delivery_mode: str | None = None,
    prompt_argument_template: str | None = None,
    prompt_flag: str | None = None,
    prompt_file: str | None = None,
    timeout_seconds: int | None = None,
    env: Sequence[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    codex_sandbox: str | None = None,
    doctor_check_command: str | None = None,
    no_version_check: bool = False,
    prune_stale: bool = False,
) -> dict[str, Any]:
    project = _project_path(project_root)
    mutating = any(
        value is not None
        for value in (
            role,
            adapter,
            command,
            args,
            cwd,
            prompt_delivery_mode,
            prompt_argument_template,
            prompt_flag,
            prompt_file,
            timeout_seconds,
            env,
            model,
            reasoning_effort,
            codex_sandbox,
            doctor_check_command,
            True if no_version_check else None,
        )
    )
    if not mutating and not prune_stale:
        return inspect_agent_configuration(project, runner_id=runner_id)

    loaded = _load_project_agent_config(project)
    if not loaded["ok"]:
        return loaded
    config: AgentRunnersConfig = loaded["config"]
    raw_config: dict[str, Any] = loaded["raw_config"]
    config_path: Path = loaded["config_path"]

    if prune_stale and not mutating:
        try:
            ensure_loopplane_home_layout()
            prune_result = prune_loopplane_home_agent_runner_overrides(keep_project=project)
        except (OSError, AgentRunnerConfigError) as error:
            errors = list(error.errors) if isinstance(error, AgentRunnerConfigError) else [str(error)]
            return _failure(
                project,
                action="configure",
                message=f"Unable to prune machine-local runner overrides: {error}",
                errors=errors,
                config_path=config_path,
            )
        try:
            runners = _selected_runners(config, runner_id=runner_id, all_runners=runner_id is None)
        except AgentRunnerConfigError as error:
            return _failure(
                project,
                action="configure",
                message=str(error),
                errors=list(error.errors),
                config_path=config_path,
            )
        return _success(
            project,
            action="configure",
            config=config,
            selected_runner_ids=[runner.runner_id for runner in runners],
            mutated=bool(prune_result["pruned_projects"]),
            runners={runner.runner_id: _runner_dict(runner) for runner in runners},
            message=f"Pruned {prune_result['pruned_projects']} stale machine-local runner override project(s).",
        local_overrides={
            "write_path": str(prune_result["path"]),
            "effective_write_path": str(prune_result["path"]),
            "loopplane_home_override_path": str(loopplane_home_layout().agent_runners_local_file),
            "project_local_override_path": str(project_local_agent_runner_override_file(project)),
            "scope": str(prune_result["scope"]),
            "portable": False,
            "portability_note": "Machine-local runner overrides are not portable; re-run configure-agent after copying or importing this project.",
            "pruned_projects": int(prune_result["pruned_projects"]),
            "pruned_project_keys": list(prune_result["pruned_project_keys"]),
        },
        )

    try:
        selected_runner_id = runner_id or _select_runner_id_for_mutation(config, role=role, adapter=adapter)
    except AgentRunnerConfigError as error:
        return _failure(
            project,
            action="configure",
            message=str(error),
            errors=list(error.errors),
            config_path=config_path,
        )

    runners = raw_config.get("runners")
    if not isinstance(runners, dict) or selected_runner_id not in runners:
        return _failure(
            project,
            action="configure",
            message=f"unknown runner: {selected_runner_id}",
            errors=[f"unknown runner: {selected_runner_id}"],
            config_path=config_path,
        )
    raw_runner = runners[selected_runner_id]
    if not isinstance(raw_runner, dict):
        return _failure(
            project,
            action="configure",
            message=f"runner {selected_runner_id!r} must be an object",
            errors=[f"runner {selected_runner_id!r} must be an object"],
            config_path=config_path,
        )
    try:
        effective_runner = config.runner(selected_runner_id)
    except AgentRunnerConfigError as error:
        return _failure(
            project,
            action="configure",
            message=str(error),
            errors=list(error.errors),
            config_path=config_path,
        )
    effective_runner_data = effective_runner.as_dict(include_inherits=False)

    role_value = str(role if role is not None else effective_runner.role or "").strip()
    adapter_value = str(adapter if adapter is not None else effective_runner.adapter or "").strip()
    command_value = str(command if command is not None else effective_runner.command or "").strip()
    canonical_adapter = _canonical_adapter(adapter_value)
    missing = [
        name
        for name, value in (
            ("role", role_value),
            ("adapter", canonical_adapter),
            ("command", command_value),
        )
        if not value
    ]
    if missing:
        return _failure(
            project,
            action="configure",
            message="Configuring a runner requires role, adapter, and command; pass --runner plus any missing fields.",
            errors=[f"missing required runner field: {name}" for name in missing],
            config_path=config_path,
        )
    if canonical_adapter not in available_adapter_names():
        return _failure(
            project,
            action="configure",
            message=f"Adapter {adapter_value!r} is not registered.",
            errors=[f"unknown adapter: {adapter_value}"],
            config_path=config_path,
        )

    env_overrides, env_errors = _parse_env_overrides(env or ())
    if env_errors:
        return _failure(
            project,
            action="configure",
            message="Invalid --env override.",
            errors=env_errors,
            config_path=config_path,
        )
    if timeout_seconds is not None and timeout_seconds <= 0:
        return _failure(
            project,
            action="configure",
            message="Invalid --timeout-seconds value.",
            errors=["--timeout-seconds must be a positive integer"],
            config_path=config_path,
        )
    model_value = str(model or "").strip() if model is not None else None
    reasoning_effort_value = str(reasoning_effort or "").strip() if reasoning_effort is not None else None
    if model is not None and not model_value:
        return _failure(
            project,
            action="configure",
            message="Invalid --model value.",
            errors=["--model must be a non-empty string"],
            config_path=config_path,
        )
    if reasoning_effort is not None:
        if not reasoning_effort_value:
            return _failure(
                project,
                action="configure",
                message="Invalid --reasoning-effort value.",
                errors=["--reasoning-effort must be a non-empty string"],
                config_path=config_path,
            )
        if reasoning_effort_value not in _REASONING_EFFORT_VALUES:
            return _failure(
                project,
                action="configure",
                message="Invalid --reasoning-effort value.",
                errors=[f"--reasoning-effort must be one of: {', '.join(sorted(_REASONING_EFFORT_VALUES))}"],
                config_path=config_path,
            )
    codex_sandbox_value = codex_sandbox.strip() if codex_sandbox is not None else None
    if codex_sandbox_value is not None:
        if canonical_adapter != "codex_cli":
            return _failure(
                project,
                action="configure",
                message="--codex-sandbox can only be used with a codex_cli runner.",
                errors=["--codex-sandbox requires adapter codex_cli"],
                config_path=config_path,
            )
        if codex_sandbox_value not in _CODEX_SANDBOX_VALUES:
            return _failure(
                project,
                action="configure",
                message="Invalid --codex-sandbox value.",
                errors=[f"--codex-sandbox must be one of: {', '.join(sorted(_CODEX_SANDBOX_VALUES))}"],
                config_path=config_path,
            )

    doctor_check_command_value = str(doctor_check_command or "").strip() if doctor_check_command is not None else None
    if doctor_check_command is not None and not doctor_check_command_value:
        return _failure(
            project,
            action="configure",
            message="Invalid --doctor-check-command value.",
            errors=["--doctor-check-command must be a non-empty command string"],
            config_path=config_path,
        )
    if doctor_check_command_value is not None and no_version_check:
        return _failure(
            project,
            action="configure",
            message="Conflicting doctor check options.",
            errors=["--doctor-check-command and --no-version-check cannot be used together"],
            config_path=config_path,
        )

    local_override = {
        "role": role_value,
        "adapter": canonical_adapter,
        "command": command_value,
        "enabled": True,
    }
    if args is not None:
        local_override["args"] = [str(item) for item in args]
    if cwd is not None:
        local_override["cwd"] = cwd.strip() or "{{project_root}}"
    if timeout_seconds is not None:
        local_override["timeout_seconds"] = int(timeout_seconds)
    if env is not None:
        base_env = effective_runner_data.get("env") if isinstance(effective_runner_data.get("env"), Mapping) else {}
        local_override["env"] = {**{str(k): str(v) for k, v in dict(base_env).items()}, **env_overrides}
    prompt_delivery = _prompt_delivery_override(
        effective_runner_data.get("prompt_delivery"),
        mode=prompt_delivery_mode,
        argument_template=prompt_argument_template,
        prompt_flag=prompt_flag,
        prompt_file=prompt_file,
    )
    if prompt_delivery is not None:
        local_override["prompt_delivery"] = prompt_delivery
    adapter_options = (
        dict(effective_runner_data.get("adapter_options"))
        if isinstance(effective_runner_data.get("adapter_options"), Mapping)
        else {}
    )
    if model_value is not None:
        adapter_options["model"] = model_value
    if reasoning_effort_value is not None:
        adapter_options["reasoning_effort"] = reasoning_effort_value
    if codex_sandbox_value is not None:
        adapter_options["codex_sandbox"] = codex_sandbox_value
    if adapter_options:
        local_override["adapter_options"] = adapter_options

    doctor = effective_runner_data.get("doctor")
    if not isinstance(doctor, dict):
        doctor = {}
    else:
        doctor = dict(doctor)
    if no_version_check:
        doctor["check_command"] = ""
        doctor["check_kind"] = "none"
    elif doctor_check_command_value is not None:
        doctor["check_command"] = doctor_check_command_value
        doctor["check_kind"] = "doctor_check"
    elif canonical_adapter == "shell":
        doctor["check_command"] = str(doctor.get("check_command") or "")
        doctor.setdefault("check_kind", "doctor_check" if doctor["check_command"] else "none")
    else:
        doctor["check_command"] = f"{command_value} --version"
        doctor["check_kind"] = "version_command"
    doctor["requires_auth"] = canonical_adapter in _AUTH_REQUIRED_ADAPTERS
    auth_check_command = _default_auth_check_command(canonical_adapter, command_value)
    if auth_check_command is None:
        doctor["auth_check_command"] = ""
        doctor["check_auth_command"] = ""
    else:
        doctor["auth_check_command"] = auth_check_command
        doctor.pop("check_auth_command", None)
    local_override["doctor"] = doctor

    try:
        candidate_config_data, candidate_local_summary = merge_agent_runner_local_overrides(
            raw_config,
            project_root=project,
            extra_project_overrides={selected_runner_id: local_override},
        )
        updated_config = parse_agent_runners_config(
            candidate_config_data,
            config_path=config_path,
            template_variables=loaded["template_variables"],
        )
    except AgentRunnerConfigError as error:
        return _failure(
            project,
            action="configure",
            message=str(error),
            errors=list(error.errors),
            config_path=config_path,
        )

    try:
        ensure_loopplane_home_layout()
        write_project_local = _should_write_project_local_override(
            project,
            selected_runner_id,
            command_provided=command is not None,
            role_provided=role is not None,
            adapter_provided=adapter is not None,
        )
        if write_project_local:
            project_local_removal = {"path": project_local_agent_runner_override_file(project), "removed": False}
            write_result = write_project_local_agent_runner_overrides(project, {selected_runner_id: local_override})
            write_scope = str(write_result["scope"])
            project_key = None
        else:
            project_local_removal = remove_project_local_agent_runner_override(project, selected_runner_id)
            write_result = write_loopplane_home_agent_runner_override(
                project,
                selected_runner_id,
                local_override,
            )
            write_scope = str(write_result["scope"])
            project_key = str(write_result["project_key"])
    except (OSError, AgentRunnerConfigError) as error:
        errors = list(error.errors) if isinstance(error, AgentRunnerConfigError) else [str(error)]
        return _failure(
            project,
            action="configure",
            message=f"Unable to write machine-local runner override: {error}",
            errors=errors,
            config_path=config_path,
        )

    reloaded = _load_project_agent_config(project)
    if reloaded.get("ok") is True:
        updated_config = reloaded["config"]
    runner = updated_config.runner(selected_runner_id)
    shadowed_project_local = bool(project_local_removal.get("removed"))
    return _success(
        project,
        action="configure",
        config=updated_config,
        selected_runner_ids=[selected_runner_id],
        mutated=True,
        runners={selected_runner_id: _runner_dict(runner)},
        message=f"Configured runner {selected_runner_id}.",
        local_overrides={
            "write_path": str(write_result["path"]),
            "effective_write_path": str(write_result["path"]),
            "loopplane_home_override_path": str(loopplane_home_layout().agent_runners_local_file),
            "project_local_override_path": str(project_local_agent_runner_override_file(project)),
            "scope": write_scope,
            "project_key": project_key,
            "portable": False,
            "portability_note": "Machine-local runner overrides are not portable; re-run configure-agent after copying or importing this project.",
            "runner_ids": sorted(set(candidate_local_summary["runner_ids"]) | {selected_runner_id}),
            "pruned_projects": int(write_result.get("pruned_projects") or 0),
            "pruned_project_keys": list(write_result.get("pruned_project_keys") or []),
            "shadowed_project_local_override_removed": shadowed_project_local,
            "shadowed_project_local_override_path": str(project_local_removal["path"]),
        },
    )


def doctor_agent_runners(
    project_root: Path | str,
    *,
    runner_id: str | None = None,
    all_runners: bool = False,
    required_only: bool = False,
) -> dict[str, Any]:
    project = _project_path(project_root)
    loaded = _load_project_agent_config(project)
    if not loaded["ok"]:
        loaded["action"] = "doctor"
        return loaded
    config: AgentRunnersConfig = loaded["config"]
    workflow_config: Mapping[str, Any] = loaded["workflow_config"]

    try:
        runners = _selected_runners(config, runner_id=runner_id, all_runners=all_runners, required_only=required_only)
    except AgentRunnerConfigError as error:
        return _failure(
            project,
            action="doctor",
            message=str(error),
            errors=list(error.errors),
            config_path=config.config_path,
        )

    results = [_doctor_one_runner(project, workflow_config, config, runner) for runner in runners]
    blocking_results = [
        result
        for result in results
        if result.get("status") != DOCTOR_STATUS_OK and not (all_runners and result.get("enabled") is False)
    ]
    optional_results = [
        result
        for result in results
        if result.get("status") != DOCTOR_STATUS_OK and all_runners and result.get("enabled") is False
    ]
    status = AGENT_COMMAND_STATUS_OK if not blocking_results else AGENT_COMMAND_STATUS_WAITING_CONFIG
    errors = [
        f"{result['runner_id']}: {result.get('message', 'runner requires configuration')}"
        for result in blocking_results
    ]
    warnings = [
        f"{result['runner_id']}: optional disabled runner skipped by default; configure it only if this workflow needs that runner. {result.get('message', 'runner requires configuration')}"
        for result in optional_results
    ]
    next_steps = _doctor_next_steps(config, results)
    return {
        "schema_version": config.schema_version,
        "action": "doctor",
        "status": status,
        "ok": status == AGENT_COMMAND_STATUS_OK,
        "project_root": str(project),
        "config_path": str(config.config_path) if config.config_path is not None else None,
        "local_override_paths": [str(path) for path in config.local_override_paths],
        "effective_override_files": [str(path) for path in config.local_override_paths],
        "loopplane_home_override_path": str(loopplane_home_layout().agent_runners_local_file),
        "project_local_override_path": str(project_local_agent_runner_override_file(project)),
        "override_portability": {
            "portable": False,
            "note": "Machine-local runner overrides are not portable; re-run configure-agent after copying or importing this project.",
        },
        "local_override_runner_ids": list(config.local_override_runner_ids),
        "default_runner": config.default_runner,
        "selected_runner_ids": [runner.runner_id for runner in runners],
        "runner_results": results,
        "errors": errors,
        "warnings": warnings,
        "next_steps": next_steps,
    }


def _override_path_lines(result: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    local_overrides = result.get("local_overrides")
    local_mapping = local_overrides if isinstance(local_overrides, Mapping) else {}
    loopplane_home_path = result.get("loopplane_home_override_path") or local_mapping.get("loopplane_home_override_path")
    project_local_path = result.get("project_local_override_path") or local_mapping.get("project_local_override_path")
    effective_write_path = local_mapping.get("effective_write_path") or local_mapping.get("write_path")
    effective_files = result.get("effective_override_files") or result.get("local_override_paths")
    if loopplane_home_path or project_local_path or effective_write_path or effective_files:
        lines.append("Runner overrides are machine-local; portable agent_runners.json stays as workflow truth.")
    if loopplane_home_path:
        lines.append(f"LOOPPLANE_HOME override path: {loopplane_home_path}")
        lines.append("LOOPPLANE_HOME overrides are machine-local, not portable; re-run configure-agent after copying or importing this project.")
    if project_local_path:
        lines.append(f"Project-local override path: {project_local_path}")
    if isinstance(effective_files, list) and effective_files:
        lines.append("Effective override files: " + ", ".join(str(path) for path in effective_files))
    if effective_write_path:
        lines.append(f"Effective write path: {effective_write_path}")
    if local_mapping.get("shadowed_project_local_override_removed"):
        shadowed_path = local_mapping.get("shadowed_project_local_override_path") or project_local_path
        lines.append(f"Removed shadowed project-local override: {shadowed_path}")
    portability_note = local_mapping.get("portability_note")
    if portability_note:
        lines.append(str(portability_note))
    return lines


def format_agent_configuration_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"Agent runner configuration: {result.get('status')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("config_path"):
        lines.append(f"Config: {result.get('config_path')}")
    lines.extend(_override_path_lines(result))
    pruned = ((result.get("local_overrides") or {}) or {}).get("pruned_projects")
    if pruned:
        lines.append(f"Pruned stale override projects: {pruned}")
    if result.get("default_runner"):
        lines.append(f"Default runner: {result.get('default_runner')}")
    if result.get("message"):
        lines.extend(("", str(result["message"])))

    errors = result.get("errors")
    if errors:
        lines.extend(("", "Errors:"))
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if warnings:
        lines.extend(("", "Warnings:"))
        lines.extend(f"  - {warning}" for warning in warnings)
    next_steps = result.get("next_steps")
    if isinstance(next_steps, Sequence) and next_steps and not isinstance(next_steps, (str, bytes)):
        lines.extend(("", "Next steps:"))
        lines.extend(f"  - {step}" for step in next_steps)

    runners = result.get("runners")
    if isinstance(runners, Mapping) and runners:
        lines.extend(("", "Runners:"))
        for runner_id, runner in runners.items():
            if not isinstance(runner, Mapping):
                continue
            lines.append(
                "  - "
                f"{runner_id}: role={runner.get('role')} adapter={runner.get('adapter')} "
                f"enabled={runner.get('enabled')} command={runner.get('command')}"
            )
    return "\n".join(lines) + "\n"


def _doctor_next_steps(config: AgentRunnersConfig, results: Sequence[Mapping[str, Any]]) -> list[str]:
    steps: list[str] = []
    for result in results:
        if result.get("status") == DOCTOR_STATUS_OK:
            continue
        if result.get("enabled") is False:
            continue
        runner_id = str(result.get("runner_id") or "")
        role = str(result.get("role") or "")
        adapter = str(result.get("adapter") or "")
        if runner_id == config.default_runner or role == "worker":
            steps.append(
                "Configure a usable worker runner, for example: "
                f"loopplane configure-agent --runner {runner_id or config.default_runner} "
                "--role worker --adapter shell --command /usr/bin/python3 "
                "--arg path/to/worker.py --prompt-delivery-mode stdin"
            )
        elif adapter in _AUTH_REQUIRED_ADAPTERS:
            steps.append(f"Authenticate or reconfigure runner {runner_id}.")
    return _dedupe_text(steps)


def _dedupe_text(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def format_agent_doctor_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"Agent runner doctor: {result.get('status')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("config_path"):
        lines.append(f"Config: {result.get('config_path')}")
    lines.extend(_override_path_lines(result))

    errors = result.get("errors")
    if errors:
        lines.extend(("", "Errors:"))
        lines.extend(f"  - {error}" for error in errors)
    warnings = result.get("warnings")
    if warnings:
        lines.extend(("", "Warnings:"))
        lines.extend(f"  - {warning}" for warning in warnings)

    runner_results = result.get("runner_results")
    if isinstance(runner_results, list) and runner_results:
        lines.extend(("", "Runners:"))
        for runner_result in runner_results:
            if not isinstance(runner_result, Mapping):
                continue
            lines.append(
                "  - "
                f"{runner_result.get('runner_id')}: {runner_result.get('status')} "
                f"({runner_result.get('adapter')}) {runner_result.get('message')}"
            )
            checks = runner_result.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, Mapping):
                        continue
                    name = str(check.get("name"))
                    code = check.get("code")
                    label = f"{name} ({code})" if code else name
                    lines.append(
                        "      "
                        f"[{check.get('status')}] {label}: {check.get('message')}"
                    )
    return "\n".join(lines) + "\n"


def agent_command_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if result.get("action") == "doctor" and isinstance(result.get("runner_results"), list):
        return EXIT_RUNNER_UNAVAILABLE
    if has_text(
        result,
        (
            "not available",
            "executable not found",
            "not found on path",
            "command failed",
            "requires authentication",
        ),
        "message",
        "errors",
        "runner_results",
    ):
        return EXIT_RUNNER_UNAVAILABLE
    return EXIT_INVALID_CONFIG


def _project_path(project_root: Path | str) -> Path:
    return Path(project_root).expanduser().resolve()


def _load_project_agent_config(project: Path) -> dict[str, Any]:
    config_path = project / AGENT_RUNNERS_CONFIG_PATH.as_posix()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
        config_path = paths.config_file("agent_runners.json")
        raw_config = _load_json_object(config_path)
        merged_config_data, local_summary = merge_agent_runner_local_overrides(raw_config, project_root=project)
        parsed_config = parse_agent_runners_config(
            merged_config_data,
            config_path=config_path,
            template_variables=paths.template_variables(),
        )
        config = AgentRunnersConfig(
            schema_version=parsed_config.schema_version,
            default_runner=parsed_config.default_runner,
            runners=parsed_config.runners,
            runner_failover=parsed_config.runner_failover,
            config_path=parsed_config.config_path,
            template_variables=parsed_config.template_variables,
            local_override_paths=tuple(Path(path) for path in local_summary["paths"]),
            local_override_runner_ids=tuple(str(runner_id) for runner_id in local_summary["runner_ids"]),
        )
    except FileNotFoundError as error:
        return _failure(
            project,
            action="load",
            message="Missing LoopPlane workflow configuration; run loopplane init first.",
            errors=[str(error)],
            config_path=config_path,
        )
    except json.JSONDecodeError as error:
        return _failure(
            project,
            action="load",
            message=f"{config_path}: invalid JSON: {error.msg}",
            errors=[f"invalid JSON: {error.msg}"],
            config_path=config_path,
        )
    except (OSError, WorkflowPathError, AgentRunnerConfigError) as error:
        errors = list(error.errors) if isinstance(error, AgentRunnerConfigError) else [str(error)]
        return _failure(
            project,
            action="load",
            message=str(error),
            errors=errors,
            config_path=config_path,
        )
    return {
        "ok": True,
        "project": project,
        "workflow_config": workflow_config,
        "template_variables": paths.template_variables(),
        "raw_config": raw_config,
        "config": config,
        "config_path": config_path,
        "local_summary": local_summary,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AgentRunnerConfigError(f"{path}: agent runner config must be a JSON object")
    return data


def _should_write_project_local_override(
    project: Path,
    runner_id: str,
    *,
    command_provided: bool,
    role_provided: bool,
    adapter_provided: bool,
) -> bool:
    if command_provided or role_provided or adapter_provided:
        return False
    path = project_local_agent_runner_override_file(project)
    if not path.is_file():
        return False
    try:
        data = _load_json_object(path)
    except (OSError, json.JSONDecodeError, AgentRunnerConfigError):
        return False
    runners = data.get("runners")
    return isinstance(runners, Mapping) and isinstance(runners.get(runner_id), Mapping)


def _selected_runners(
    config: AgentRunnersConfig,
    *,
    runner_id: str | None,
    all_runners: bool,
    required_only: bool = False,
) -> list[RunnerConfig]:
    if required_only:
        return [config.runners[runner_id] for runner_id in sorted(config.runners) if config.runners[runner_id].enabled]
    if all_runners:
        return [config.runners[runner_id] for runner_id in sorted(config.runners)]
    return [config.runner(runner_id)]


def _select_runner_id(config: AgentRunnersConfig, *, role: str, adapter: str) -> str:
    matches = [
        runner.runner_id
        for runner in config.runners.values()
        if runner.role == role.strip() and runner.adapter == adapter
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentRunnerConfigError(
            f"multiple runners match role {role!r} and adapter {adapter!r}; pass --runner",
            errors=[f"multiple runners match role {role!r} and adapter {adapter!r}: {', '.join(matches)}"],
        )
    raise AgentRunnerConfigError(
        f"no runner matches role {role!r} and adapter {adapter!r}; pass --runner",
        errors=[f"no runner matches role {role!r} and adapter {adapter!r}"],
    )


def _select_runner_id_for_mutation(
    config: AgentRunnersConfig,
    *,
    role: str | None,
    adapter: str | None,
) -> str:
    if role is None and adapter is None:
        raise AgentRunnerConfigError(
            "pass --runner when updating runner fields without --role/--adapter",
            errors=["missing --runner for partial runner update"],
        )
    if role is None or adapter is None:
        raise AgentRunnerConfigError(
            "pass --runner, or provide both --role and --adapter to select a runner",
            errors=["partial runner selection requires --runner or both --role and --adapter"],
        )
    return _select_runner_id(config, role=role, adapter=_canonical_adapter(adapter))


def _parse_env_overrides(values: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    errors: list[str] = []
    for value in values:
        if "=" not in value:
            errors.append(f"--env value must use KEY=VALUE syntax: {value}")
            continue
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            errors.append(f"--env key must be non-empty: {value}")
            continue
        env[key] = raw
    return env, errors


def _prompt_delivery_override(
    current: Any,
    *,
    mode: str | None,
    argument_template: str | None,
    prompt_flag: str | None,
    prompt_file: str | None,
) -> dict[str, Any] | None:
    if all(value is None for value in (mode, argument_template, prompt_flag, prompt_file)):
        return None
    prompt_delivery = dict(current) if isinstance(current, Mapping) else {}
    if mode is not None:
        selected_mode = mode.strip()
        prompt_delivery["mode"] = selected_mode
        if selected_mode == "stdin":
            for stale_key in ("argument_template", "prompt_flag", "prompt_file"):
                prompt_delivery.pop(stale_key, None)
        elif selected_mode == "file_argument":
            for stale_key in ("prompt_flag", "prompt_file"):
                prompt_delivery.pop(stale_key, None)
        elif selected_mode == "stdin_or_prompt_flag":
            prompt_delivery.pop("argument_template", None)
        elif selected_mode in {"interactive_terminal", "custom_adapter"}:
            for stale_key in ("argument_template", "prompt_flag", "prompt_file"):
                prompt_delivery.pop(stale_key, None)
    if argument_template is not None:
        prompt_delivery["argument_template"] = argument_template
    if prompt_flag is not None:
        prompt_delivery["prompt_flag"] = prompt_flag
    if prompt_file is not None:
        prompt_delivery["prompt_file"] = prompt_file
    return prompt_delivery


def _doctor_one_runner(
    project: Path,
    workflow_config: Mapping[str, Any],
    config: AgentRunnersConfig,
    runner: RunnerConfig,
) -> dict[str, Any]:
    try:
        adapter = get_adapter(runner.adapter)
    except AdapterLookupError as error:
        return {
            "schema_version": config.schema_version,
            "runner_id": runner.runner_id,
            "role": runner.role,
            "adapter": runner.adapter,
            "enabled": runner.enabled,
            "status": DOCTOR_STATUS_WAITING_CONFIG,
            "checks": [
                {
                    "name": "adapter_lookup",
                    "status": DOCTOR_STATUS_WAITING_CONFIG,
                    "message": str(error),
                }
            ],
            "message": f"Adapter {runner.adapter!r} is not registered.",
            "adapter_metadata": {"external_execution": False},
        }

    workflow_id = workflow_config.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        workflow_id = "unknown_workflow"

    with tempfile.TemporaryDirectory(prefix="loopplane-agent-doctor-") as tmp:
        doctor_root = Path(tmp)
        prompt_path = doctor_root / "prompt.md"
        prompt_path.write_text("LoopPlane agent runner doctor.\n", encoding="utf-8")
        adapter_input = AdapterInput.from_runner_config(
            run_id=f"doctor_{runner.runner_id}",
            workflow_id=workflow_id,
            runner_config=runner,
            prompt_path=prompt_path,
            prompt_content="LoopPlane agent runner doctor.\n",
            scheduler_run_dir=doctor_root / "runtime" / runner.runner_id,
            role_output_dir=doctor_root / "results" / runner.runner_id,
            task_id=None,
            task_evidence_run_dir=None,
            cwd=str(_resolve_cwd(project, runner.cwd)),
        )
        doctor_result = adapter.doctor(adapter_input).to_dict()
        original_status = doctor_result.get("status")
        checks = doctor_result.get("checks")
        if not isinstance(checks, list):
            checks = []
            doctor_result["checks"] = checks

        family_check = _adapter_command_family_check(runner)
        if family_check is not None:
            checks.append(family_check)

        policy = machine_resource_policy_from_runner(runner)
        if policy is not None:
            checks.append(
                runner_lock_doctor_check(
                    str(policy.get("lock_key") or ""),
                    runner_ids=[runner.runner_id],
                )
            )

        if (
            doctor_result.get("status") != DOCTOR_STATUS_OK
            and runner.runner_id not in set(config.local_override_runner_ids)
        ):
            checks.append(
                {
                    "name": "local_override",
                    "status": DOCTOR_STATUS_WAITING_CONFIG,
                    "code": "local_override_missing",
                    "message": (
                        "No machine-local runner override is active for this runner; "
                        "run loopplane configure-agent with the local command path or ensure "
                        "$LOOPPLANE_HOME/runners/agent_runners.local.json is available."
                    ),
                }
            )
        combined_status = _aggregate_runner_doctor_status(tuple(check for check in checks if isinstance(check, Mapping)))
        doctor_result["status"] = combined_status
        if combined_status != DOCTOR_STATUS_OK and original_status == DOCTOR_STATUS_OK:
            doctor_result["message"] = "Runner requires machine runner lock recovery."
        return {**doctor_result, "enabled": runner.enabled}


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = cwd.replace("{{project_root}}", project.as_posix())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _aggregate_runner_doctor_status(checks: Sequence[Mapping[str, Any]]) -> str:
    if any(check.get("status") == DOCTOR_STATUS_WAITING_CONFIG for check in checks):
        return DOCTOR_STATUS_WAITING_CONFIG
    return DOCTOR_STATUS_OK


def _adapter_command_family_check(runner: RunnerConfig) -> dict[str, Any] | None:
    adapter = _canonical_adapter(runner.adapter)
    try:
        parts = shlex.split(runner.command)
    except ValueError:
        return None
    if not parts:
        return None
    executable = Path(parts[0]).name.lower()
    mismatched = (
        (adapter == "codex_cli" and "claude" in executable)
        or (adapter == "claude_code_cli" and "codex" in executable)
    )
    if not mismatched:
        return None
    return {
        "name": "adapter_command_family",
        "status": DOCTOR_STATUS_WAITING_CONFIG,
        "code": "adapter_command_family_mismatch",
        "message": (
            f"Runner adapter {runner.adapter!r} appears to be paired with command {runner.command!r}. "
            "Configure the adapter and command from the same CLI family."
        ),
    }


def _runner_dict(runner: RunnerConfig) -> dict[str, Any]:
    data = runner.as_dict()
    data["runner_id"] = runner.runner_id
    return data


def _canonical_adapter(adapter: str) -> str:
    normalized = adapter.strip()
    return _ADAPTER_ALIASES.get(normalized, normalized)


def _default_auth_check_command(adapter: str, command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    executable = Path(parts[0]).name
    if adapter == "codex_cli" and executable == "codex":
        return f"{command} login status"
    if adapter == "claude_code_cli" and executable == "claude":
        return f"{command} auth status"
    return None


def _success(
    project: Path,
    *,
    action: str,
    config: AgentRunnersConfig,
    selected_runner_ids: list[str],
    mutated: bool,
    runners: Mapping[str, Any],
    message: str | None = None,
    local_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": config.schema_version,
        "action": action,
        "status": AGENT_COMMAND_STATUS_OK,
        "ok": True,
        "project_root": str(project),
        "config_path": str(config.config_path) if config.config_path is not None else None,
        "local_override_paths": [str(path) for path in config.local_override_paths],
        "effective_override_files": [str(path) for path in config.local_override_paths],
        "loopplane_home_override_path": str(loopplane_home_layout().agent_runners_local_file),
        "project_local_override_path": str(project_local_agent_runner_override_file(project)),
        "local_override_runner_ids": list(config.local_override_runner_ids),
        "default_runner": config.default_runner,
        "selected_runner_ids": selected_runner_ids,
        "mutated": mutated,
        "runners": dict(runners),
        "errors": [],
    }
    if message:
        result["message"] = message
    if local_overrides is not None:
        local_override_result = dict(local_overrides)
        result["local_overrides"] = local_override_result
        if local_override_result.get("effective_write_path"):
            result["effective_write_path"] = local_override_result["effective_write_path"]
    return result


def _failure(
    project: Path,
    *,
    action: str,
    message: str,
    errors: list[str],
    config_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.5",
        "action": action,
        "status": AGENT_COMMAND_STATUS_WAITING_CONFIG,
        "ok": False,
        "project_root": str(project),
        "config_path": str(config_path) if config_path is not None else None,
        "message": message,
        "selected_runner_ids": [],
        "mutated": False,
        "runners": {},
        "errors": errors,
    }


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
