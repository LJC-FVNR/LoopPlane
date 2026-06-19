from __future__ import annotations

from collections.abc import Mapping

from runtime.adapters.base import (
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
)
from runtime.adapters.shell_adapter import ShellAdapter, build_shell_invocation, shell_doctor_checks


class ClaudeCodeCliAdapter(ShellAdapter):
    adapter_name = "claude_code_cli"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        return super().run(adapter_input)

    def build_invocation(self, adapter_input: AdapterInput) -> tuple[list[str], str | None]:
        argv, stdin_text = build_claude_code_invocation(adapter_input)
        return argv, stdin_text

    def doctor(self, adapter_input: AdapterInput) -> AdapterDoctorResult:
        checks = shell_doctor_checks(adapter_input, invocation_builder=self.build_invocation)
        if any(check.get("status") == "waiting_config" for check in checks):
            return AdapterDoctorResult.waiting_config(
                adapter_input,
                checks=checks,
                message="Claude Code CLI adapter requires configuration before task execution.",
                adapter_metadata={
                    "external_execution": False,
                    "process_execution": True,
                    "supported_prompt_delivery_modes": ["file_argument", "stdin", "stdin_or_prompt_flag"],
                },
            )
        return AdapterDoctorResult.ok(
            adapter_input,
            checks=checks,
            message="Claude Code CLI adapter can execute configured CLI tasks.",
            adapter_metadata={
                "external_execution": False,
                "process_execution": True,
                "supported_prompt_delivery_modes": ["file_argument", "stdin", "stdin_or_prompt_flag"],
            },
        )


def build_claude_code_invocation(adapter_input: AdapterInput) -> tuple[list[str], str | None]:
    argv, stdin_text = build_shell_invocation(adapter_input)
    _append_claude_permission_flags(argv, adapter_input)
    _append_claude_model_flags(argv, adapter_input)
    return argv, stdin_text


def _append_claude_permission_flags(argv: list[str], adapter_input: AdapterInput) -> None:
    if _has_any_flag(argv, {"--dangerously-skip-permissions", "--permission-mode"}):
        return
    options = _adapter_options(adapter_input)
    mode = _option_text(
        options,
        (
            "claude_permission_mode",
            "permission_mode",
            "permission_policy",
        ),
    )
    normalized = mode.strip().lower().replace("-", "_") if mode else "bypass_permissions"
    if normalized in {"", "default", "none", "ask", "interactive"}:
        return
    if normalized in {"acceptedits", "accept_edits", "accept"}:
        argv[1:1] = ["--permission-mode", "acceptEdits"]
        return
    if normalized in {"bypasspermissions", "bypass_permissions", "dangerously_skip_permissions", "danger"}:
        argv[1:1] = ["--dangerously-skip-permissions"]
        return
    argv[1:1] = [mode]


def _append_claude_model_flags(argv: list[str], adapter_input: AdapterInput) -> None:
    options = _adapter_options(adapter_input)
    model = _option_text(options, ("model", "claude_model"))
    model_flag = _option_text(options, ("model_flag", "claude_model_flag")) or "--model"
    if model and not _has_any_flag(argv, {model_flag, "--model"}):
        argv[1:1] = [model_flag, model]
    effort = _option_text(options, ("reasoning_effort", "thinking_effort", "effort"))
    effort_flag = _option_text(options, ("reasoning_effort_flag", "claude_reasoning_effort_flag"))
    if effort and effort_flag and not _has_any_flag(argv, {effort_flag}):
        argv[1:1] = [effort_flag, effort]


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


def _has_any_flag(argv: list[str], flags: set[str]) -> bool:
    return any(item in flags or any(item.startswith(f"{flag}=") for flag in flags if flag.startswith("--")) for item in argv)
