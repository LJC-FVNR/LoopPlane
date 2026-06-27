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
from runtime.adapters.shell_adapter import ShellAdapter, StdoutTransform, shell_doctor_checks

_DEFAULT_RESULT_PREVIEW_CHARS = 200


class CodexCliAdapter(ShellAdapter):
    adapter_name = "codex_cli"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        return super().run(adapter_input)

    def build_invocation(self, adapter_input: AdapterInput) -> tuple[list[str], str | None]:
        return build_codex_invocation(adapter_input)

    def make_stdout_transform(self, adapter_input: AdapterInput) -> "StdoutTransform | None":
        """Render Codex JSONL stdout into compact, human-readable log lines."""
        if not _codex_json_logs_enabled(adapter_input):
            return None
        options = _adapter_options(adapter_input)
        preview = _option_text(options, ("codex_log_result_preview_chars", "log_result_preview_chars"))
        try:
            preview_chars = int(preview) if preview else _DEFAULT_RESULT_PREVIEW_CHARS
        except ValueError:
            preview_chars = _DEFAULT_RESULT_PREVIEW_CHARS
        return CodexJsonRenderer(
            result_preview_chars=max(0, preview_chars),
            include_warnings=options.get("codex_log_include_warnings") is True,
        )

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
    if not approval_args:
        approval_args = ["--ask-for-approval", "never"]
    if configured_args and configured_args[0] in {"exec", "e"}:
        configured_args = configured_args[1:]

    argv = [command_parts[0], *approval_args, "exec", *configured_args]
    _append_default_exec_flags(argv, adapter_input)
    argv.append("-")
    return argv, adapter_input.prompt_content


def _append_default_exec_flags(argv: list[str], adapter_input: AdapterInput) -> None:
    _append_codex_model_flags(argv, adapter_input)
    if _codex_json_logs_enabled(adapter_input) and not _has_any_flag(argv, {"--json"}):
        argv.append("--json")
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


def _append_codex_model_flags(argv: list[str], adapter_input: AdapterInput) -> None:
    options = _adapter_options(adapter_input)
    model = _option_text(options, ("model", "codex_model"))
    if model and not _has_any_flag(argv, {"--model", "-m"}):
        argv.extend(("--model", model))
    effort = _option_text(options, ("reasoning_effort", "model_reasoning_effort", "codex_reasoning_effort", "effort"))
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


def _codex_json_logs_enabled(adapter_input: AdapterInput) -> bool:
    return _adapter_options(adapter_input).get("codex_stream_logs") is not False


class CodexJsonRenderer(StdoutTransform):
    """Render Codex exec JSONL events into a compact dashboard log.

    Codex JSONL includes token deltas, warnings, patch bodies, and command
    output chunks that are useful for machines but noisy in a dashboard tail.
    The renderer keeps assistant messages, command/tool starts, compact output
    previews, patch/diff markers, errors, and a done line. Unknown JSON events
    are dropped, while non-JSON lines pass through unchanged so launch errors
    remain visible.
    """

    def __init__(
        self,
        *,
        result_preview_chars: int = _DEFAULT_RESULT_PREVIEW_CHARS,
        include_warnings: bool = False,
    ) -> None:
        self._result_preview_chars = result_preview_chars
        self._include_warnings = include_warnings
        self._result_text: str | None = None
        self._assistant_texts: list[str] = []
        self._passthrough_lines: list[str] = []
        self._text_buffers: dict[str, list[str]] = {}
        self._buffer_kinds: dict[str, str] = {}
        self._output_sizes: dict[str, int] = {}
        self._output_previews: dict[str, str] = {}
        self._diff_seen = False

    def render_line(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            event = json.loads(stripped)
        except (ValueError, TypeError):
            self._passthrough_lines.append(line.rstrip())
            return line
        if not isinstance(event, Mapping):
            self._passthrough_lines.append(line.rstrip())
            return line
        return self._render_event(event)

    def final_output(self) -> str:
        if self._result_text is not None:
            return self._result_text
        if self._assistant_texts:
            return "\n".join(self._assistant_texts)
        if self._passthrough_lines:
            return "\n".join(self._passthrough_lines)
        return ""

    def _render_event(self, event: Mapping[str, object]) -> str | None:
        method = _string_value(event.get("method"))
        if method:
            params = event.get("params")
            return self._render_method(method, params if isinstance(params, Mapping) else {}, event)
        event_type = _string_value(event.get("type") or event.get("event_type") or event.get("event"))
        if not event_type:
            return None
        return self._render_typed_event(event_type, event)

    def _render_method(
        self,
        method: str,
        params: Mapping[str, object],
        event: Mapping[str, object],
    ) -> str | None:
        normalized = method.lower()
        if _is_warning_kind(normalized):
            return self._render_warning(params or event)
        if "error" in normalized:
            return self._render_error(params or event)
        if normalized in {"thread/started", "turn/started"}:
            model = _first_text(params, ("model", "modelName", "model_name"))
            return f"▶ turn start · model={model}" if model else "▶ turn start"
        if normalized == "item/agentmessage/delta":
            self._append_text_buffer(_item_id(params, "agent_message"), "agent_message", _message_text(params))
            return None
        if normalized == "item/plan/delta":
            self._append_text_buffer(_item_id(params, "plan"), "plan", _message_text(params))
            return None
        if normalized.startswith("item/reasoning/"):
            return None
        if normalized in {
            "item/commandexecution/outputdelta",
            "command/exec/outputdelta",
            "process/outputdelta",
            "item/filechange/outputdelta",
        }:
            self._append_output_buffer(_item_id(params, "output"), _message_text(params))
            return None
        if normalized == "item/commandexecution/terminalinteraction":
            return self._render_command(params)
        if normalized in {"item/tool/call", "item/mcptoolcall/progress"}:
            return self._render_tool_call(params)
        if normalized == "item/started":
            return self._render_item_started(params)
        if normalized in {"item/filechange/patchupdated", "turn/diff/updated"}:
            return self._render_diff_summary(params)
        if "requestapproval" in normalized:
            return self._render_approval_request(params)
        if normalized in {"item/completed", "rawresponseitem/completed"}:
            return self._render_item_completed(params)
        if normalized == "process/exited":
            return self._render_process_exited(params)
        if normalized == "turn/completed":
            return self._render_turn_completed(params)
        if normalized in {"thread/tokenusage/updated", "thread/status/changed", "serverrequest/resolved"}:
            return None
        return None

    def _render_typed_event(self, event_type: str, event: Mapping[str, object]) -> str | None:
        normalized = event_type.lower()
        if _is_warning_kind(normalized):
            return self._render_warning(event)
        if "error" in normalized:
            return self._render_error(event)
        if "reason" in normalized or "token" in normalized:
            return None
        if normalized in {"agent_message", "assistant_message", "assistant"} or (
            "agent" in normalized and "message" in normalized
        ):
            return self._render_assistant_text(_message_text(event))
        if "command" in normalized and ("exec" in normalized or _command_text(event)):
            return self._render_command(event)
        if "tool" in normalized and "result" not in normalized:
            return self._render_tool_call(event)
        if "output" in normalized or "tool_result" in normalized or "result_delta" in normalized:
            text = _message_text(event)
            return self._format_output_summary("output", text)
        if "patch" in normalized or "diff" in normalized:
            return self._render_diff_summary(event)
        if normalized in {"turn_complete", "turn_completed", "result", "done"} or "complete" in normalized:
            return self._render_turn_completed(event)
        return None

    def _render_assistant_text(self, text: str) -> str | None:
        text = text.rstrip()
        if not text:
            return None
        self._assistant_texts.append(text)
        return text

    def _render_warning(self, value: Mapping[str, object]) -> str | None:
        if not self._include_warnings:
            return None
        message = _message_text(value)
        return f"warning: {_clip(message, self._result_preview_chars)}" if message else "warning"

    def _render_error(self, value: Mapping[str, object]) -> str:
        message = _message_text(value) or _first_text(value, ("error", "errorMessage", "reason"))
        return f"error: {_clip(message, self._result_preview_chars)}" if message else "error"

    def _render_command(self, value: Mapping[str, object]) -> str | None:
        command = _command_text(value)
        return f"$ {_clip(command, 160)}" if command else None

    def _render_tool_call(self, value: Mapping[str, object]) -> str | None:
        name = _first_text(value, ("name", "tool", "toolName", "tool_name")) or "tool"
        arg = _first_text(value, ("command", "path", "file_path", "filePath", "pattern", "url", "description"))
        if not arg:
            arg = _command_text(value)
        return f"🔧 {name}({_clip(arg, 120)})" if arg else f"🔧 {name}"

    def _render_item_started(self, params: Mapping[str, object]) -> str | None:
        item = params.get("item")
        record = item if isinstance(item, Mapping) else params
        kind = _item_kind(record)
        if "command" in kind or _command_text(record):
            return self._render_command(record)
        if "tool" in kind:
            return self._render_tool_call(record)
        if "file" in kind or "patch" in kind:
            path = _first_text(record, ("path", "file_path", "filePath"))
            return f"patch: {_clip(path, 160)}" if path else "patch started"
        return None

    def _render_approval_request(self, params: Mapping[str, object]) -> str:
        command = _command_text(params)
        path = _first_text(params, ("path", "file_path", "filePath"))
        target = command or path
        return f"approval requested: {_clip(target, 160)}" if target else "approval requested"

    def _render_item_completed(self, params: Mapping[str, object]) -> str | None:
        item = params.get("item")
        record = item if isinstance(item, Mapping) else params
        item_id = _item_id(params, _item_id(record, "item"))
        kind = _item_kind(record)
        buffered = self._pop_text_buffer(item_id)
        text = _message_text(record) or buffered
        lines: list[str] = []
        if text:
            if "agent" in kind or "message" in kind:
                rendered = self._render_assistant_text(text)
                if rendered:
                    lines.append(rendered)
            elif "plan" in kind:
                lines.append(f"plan: {_clip(text, 240)}")
        output = self._pop_output_summary(item_id)
        if output:
            lines.append(output)
        return "\n".join(lines) if lines else None

    def _render_process_exited(self, params: Mapping[str, object]) -> str | None:
        item_id = _item_id(params, "output")
        lines: list[str] = []
        output = self._pop_output_summary(item_id)
        if output:
            lines.append(output)
        code = params.get("exitCode", params.get("exit_code"))
        if isinstance(code, int):
            lines.append(f"   ↳ command exited {code}")
        return "\n".join(lines) if lines else None

    def _render_diff_summary(self, value: Mapping[str, object]) -> str | None:
        if self._diff_seen:
            return None
        self._diff_seen = True
        text = _message_text(value) or _first_text(value, ("unifiedDiff", "diff", "patch"))
        path = _first_text(value, ("path", "file_path", "filePath"))
        parts = ["diff updated"]
        if path:
            parts.append(_clip(path, 120))
        if text:
            parts.append(f"{len(text)}b")
        return " · ".join(parts)

    def _render_turn_completed(self, value: Mapping[str, object]) -> str:
        result = _first_text(value, ("finalMessage", "final_message", "last_agent_message", "result", "message"))
        if result:
            self._result_text = result
        pending = self._drain_pending()
        parts = ["✓ done"]
        tokens = _token_count(value)
        if tokens is not None:
            parts.append(f"{tokens} tokens")
        status = _first_text(value, ("status", "outcome"))
        if status and status.lower() not in {"ok", "success", "completed"}:
            parts.append(status)
        done = " · ".join(parts)
        return f"{pending}\n{done}" if pending else done

    def _append_text_buffer(self, item_id: str, kind: str, text: str) -> None:
        if not text:
            return
        self._text_buffers.setdefault(item_id, []).append(text)
        self._buffer_kinds[item_id] = kind

    def _pop_text_buffer(self, item_id: str) -> str:
        values = self._text_buffers.pop(item_id, [])
        self._buffer_kinds.pop(item_id, None)
        return "".join(values).rstrip()

    def _append_output_buffer(self, item_id: str, text: str) -> None:
        if not text:
            return
        self._output_sizes[item_id] = self._output_sizes.get(item_id, 0) + len(text)
        if self._result_preview_chars <= 0:
            return
        current = self._output_previews.get(item_id, "")
        remaining = max(0, self._result_preview_chars - len(current))
        if remaining > 0:
            self._output_previews[item_id] = current + text[:remaining]

    def _pop_output_summary(self, item_id: str) -> str | None:
        size = self._output_sizes.pop(item_id, 0)
        preview = self._output_previews.pop(item_id, "")
        if size <= 0:
            return None
        if self._result_preview_chars <= 0 or not preview:
            return f"   ↳ output ({size}b)"
        suffix = f"…(+{size - len(preview)}b)" if size > len(preview) else ""
        return f"   ↳ output ({size}b): {_clip(preview, self._result_preview_chars)}{suffix}"

    def _format_output_summary(self, label: str, text: str) -> str | None:
        if not text:
            return None
        size = len(text)
        if self._result_preview_chars <= 0:
            return f"   ↳ {label} ({size}b)"
        return f"   ↳ {label} ({size}b): {_clip(text, self._result_preview_chars)}"

    def _drain_pending(self) -> str:
        lines: list[str] = []
        for item_id in list(self._text_buffers):
            kind = self._buffer_kinds.get(item_id, "")
            text = self._pop_text_buffer(item_id)
            if not text:
                continue
            if "agent" in kind or "message" in kind:
                rendered = self._render_assistant_text(text)
                if rendered:
                    lines.append(rendered)
            elif "plan" in kind:
                lines.append(f"plan: {_clip(text, 240)}")
        for item_id in list(self._output_sizes):
            output = self._pop_output_summary(item_id)
            if output:
                lines.append(output)
        return "\n".join(lines)


def _is_warning_kind(value: str) -> bool:
    return (
        value in {"warning", "guardianwarning", "deprecationnotice", "confignotice", "configwarning"}
        or value.endswith("warning")
    )


def _item_id(value: Mapping[str, object], fallback: str) -> str:
    for key in ("itemId", "item_id", "id"):
        current = value.get(key)
        if isinstance(current, str) and current:
            return current
    item = value.get("item")
    if isinstance(item, Mapping):
        return _item_id(item, fallback)
    return fallback


def _item_kind(value: Mapping[str, object]) -> str:
    return _first_text(value, ("type", "kind", "itemType", "item_type")).lower()


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_message_text(item) for item in value)
    if not isinstance(value, Mapping):
        return ""
    for key in (
        "delta",
        "text",
        "message",
        "content",
        "result",
        "finalMessage",
        "final_message",
        "last_agent_message",
        "output",
        "chunk",
        "data",
    ):
        current = value.get(key)
        text = _message_text(current)
        if text:
            return text
    params = value.get("params")
    if isinstance(params, Mapping):
        return _message_text(params)
    return ""


def _command_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if not isinstance(value, Mapping):
        return ""
    for key in ("command", "cmd", "shell_command", "argv"):
        current = value.get(key)
        text = _command_text(current)
        if text:
            return text
    for key in ("input", "arguments", "params", "item"):
        current = value.get(key)
        if isinstance(current, Mapping):
            text = _command_text(current)
            if text:
                return text
    return ""


def _first_text(value: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        current = value.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
        if isinstance(current, (int, float)):
            return str(current)
        if isinstance(current, Mapping):
            text = _message_text(current)
            if text:
                return text.strip()
    return ""


def _token_count(value: Mapping[str, object]) -> int | None:
    for key in ("total_tokens", "totalTokens", "tokens"):
        current = value.get(key)
        if isinstance(current, int):
            return current
    for key in ("usage", "tokenUsage", "token_usage"):
        current = value.get(key)
        if isinstance(current, Mapping):
            nested = _token_count(current)
            if nested is not None:
                return nested
    return None


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _clip(value: str, limit: int) -> str:
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…(+{len(collapsed) - limit}b)"
