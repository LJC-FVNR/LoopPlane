from __future__ import annotations

import json
import shlex
from collections.abc import Mapping

from runtime.adapters.base import (
    AdapterContractError,
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
)
from runtime.adapters.shell_adapter import ShellAdapter, shell_doctor_checks


class CodexCliAdapter(ShellAdapter):
    adapter_name = "codex_cli"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        return super().run(adapter_input)

    def build_invocation(self, adapter_input: AdapterInput) -> tuple[list[str], str | None]:
        return build_codex_invocation(adapter_input)

    def doctor(self, adapter_input: AdapterInput) -> AdapterDoctorResult:
        checks = shell_doctor_checks(adapter_input, invocation_builder=self.build_invocation)
        if any(check.get("status") == "waiting_config" for check in checks):
            return AdapterDoctorResult.waiting_config(
                adapter_input,
                checks=checks,
                message="Codex CLI adapter requires configuration before task execution.",
                adapter_metadata={
                    "external_execution": False,
                    "process_execution": True,
                    "supported_prompt_delivery_modes": ["file_argument", "stdin", "stdin_or_prompt_flag"],
                },
            )
        return AdapterDoctorResult.ok(
            adapter_input,
            checks=checks,
            message="Codex CLI adapter can execute configured CLI tasks.",
            adapter_metadata={
                "external_execution": False,
                "process_execution": True,
                "supported_prompt_delivery_modes": ["file_argument", "stdin", "stdin_or_prompt_flag"],
            },
        )


def build_codex_invocation(adapter_input: AdapterInput) -> tuple[list[str], str | None]:
    try:
        command_parts = shlex.split(adapter_input.command)
    except ValueError as error:
        raise AdapterContractError(f"command cannot be parsed: {error}") from error
    if not command_parts:
        raise AdapterContractError("command cannot be empty")

    mode = str(adapter_input.prompt_delivery.get("mode", "stdin"))
    if mode not in {"stdin", "file_argument", "stdin_or_prompt_flag"}:
        raise AdapterContractError(f"Prompt delivery mode {mode!r} is not supported by the Codex CLI adapter.")

    configured_args = [*command_parts[1:], *(_expand_template(value, adapter_input) for value in adapter_input.args)]
    approval_args, configured_args = _extract_approval_args(configured_args)
    legacy_effort, configured_args = _extract_legacy_effort_args(configured_args)
    if not approval_args:
        approval_args = ["--ask-for-approval", "never"]
    if configured_args and configured_args[0] in {"exec", "e"}:
        configured_args = configured_args[1:]

    argv = [command_parts[0], *approval_args, "exec", *configured_args]
    _append_default_exec_flags(argv, adapter_input, explicit_effort=legacy_effort)
    argv.append("-")
    return argv, adapter_input.prompt_content


def _append_default_exec_flags(
    argv: list[str],
    adapter_input: AdapterInput,
    *,
    explicit_effort: str | None = None,
) -> None:
    _append_codex_model_flags(argv, adapter_input, explicit_effort=explicit_effort)
    if not _has_any_flag(argv, {"--ask-for-approval", "-a"}):
        argv.extend(("--ask-for-approval", "never"))
    if not _has_any_flag(argv, {"--skip-git-repo-check"}):
        argv.append("--skip-git-repo-check")
    if not _has_any_flag(
        argv,
        {"--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox"},
    ):
        sandbox = _default_codex_sandbox(adapter_input)
        argv.extend(("--sandbox", sandbox))


def _append_codex_model_flags(
    argv: list[str],
    adapter_input: AdapterInput,
    *,
    explicit_effort: str | None = None,
) -> None:
    options = _adapter_options(adapter_input)
    model = _option_text(options, ("model", "codex_model"))
    if model and not _has_any_flag(argv, {"--model", "-m"}):
        argv.extend(("--model", model))
    effort = explicit_effort or _option_text(
        options,
        ("reasoning_effort", "model_reasoning_effort", "codex_reasoning_effort", "effort"),
    )
    config_key = _option_text(options, ("reasoning_effort_config_key", "codex_reasoning_effort_config_key"))
    if not config_key:
        config_key = "model_reasoning_effort"
    if effort and not _has_config_override(argv, config_key):
        argv.extend(("-c", f"{config_key}={json.dumps(effort)}"))


def _default_codex_sandbox(adapter_input: AdapterInput) -> str:
    if adapter_input.permission_policy.get("read_only") is True:
        return "read-only"
    runner_config = adapter_input.runner_config
    adapter_options = runner_config.get("adapter_options") if isinstance(runner_config, Mapping) else None
    if isinstance(adapter_options, Mapping):
        configured = str(adapter_options.get("codex_sandbox") or "").strip()
        if configured:
            return configured
    return "danger-full-access"


def _adapter_options(adapter_input: AdapterInput) -> Mapping[str, object]:
    runner_config = adapter_input.runner_config
    adapter_options = runner_config.get("adapter_options") if isinstance(runner_config, Mapping) else None
    return adapter_options if isinstance(adapter_options, Mapping) else {}


def _option_text(options: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = options.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_approval_args(args: list[str]) -> tuple[list[str], list[str]]:
    approval_args: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--ask-for-approval", "-a"}:
            approval_args.append(arg)
            if index + 1 < len(args):
                approval_args.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if arg.startswith("--ask-for-approval="):
            approval_args.append(arg)
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return approval_args, remaining


def _extract_legacy_effort_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Translate the legacy ``--effort`` convenience flag into Codex config.

    Some machine-local runner profiles predate the Codex CLI's current surface
    and pass ``--effort VALUE`` after ``codex exec``.  Current Codex rejects that
    argument, while the equivalent ``-c model_reasoning_effort=VALUE`` remains
    supported.  Treat the legacy form as an explicit override instead of
    forwarding an invocation that is guaranteed to fail.
    """

    effort: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--effort":
            if index + 1 >= len(args) or not str(args[index + 1]).strip():
                raise AdapterContractError("--effort requires a non-empty value")
            effort = str(args[index + 1]).strip()
            index += 2
            continue
        if arg.startswith("--effort="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise AdapterContractError("--effort requires a non-empty value")
            effort = value
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return effort, remaining


def _has_any_flag(argv: list[str], flags: set[str]) -> bool:
    return any(item in flags or any(item.startswith(f"{flag}=") for flag in flags if flag.startswith("--")) for item in argv)


def _has_config_override(argv: list[str], key: str) -> bool:
    for index, item in enumerate(argv):
        if item in {"-c", "--config"} and index + 1 < len(argv):
            if argv[index + 1].split("=", 1)[0] == key:
                return True
        if item.startswith("--config=") and item.removeprefix("--config=").split("=", 1)[0] == key:
            return True
    return False


def _expand_template(value: str, adapter_input: AdapterInput) -> str:
    replacements = {
        "{{prompt_path}}": adapter_input.prompt_path.as_posix(),
        "{{run_id}}": adapter_input.run_id,
        "{{workflow_id}}": adapter_input.workflow_id,
        "{{runner_id}}": adapter_input.runner_id,
        "{{role}}": adapter_input.role,
        "{{task_id}}": adapter_input.task_id or "",
    }
    expanded = value
    for marker, replacement in replacements.items():
        expanded = expanded.replace(marker, replacement)
    return expanded
