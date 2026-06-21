from __future__ import annotations

import json
from collections.abc import Mapping

from runtime.adapters.base import (
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
)
from runtime.adapters.shell_adapter import (
    ShellAdapter,
    StdoutTransform,
    build_shell_invocation,
    shell_doctor_checks,
)

_DEFAULT_RESULT_PREVIEW_CHARS = 200


class ClaudeCodeCliAdapter(ShellAdapter):
    adapter_name = "claude_code_cli"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        return super().run(adapter_input)

    def build_invocation(self, adapter_input: AdapterInput) -> tuple[list[str], str | None]:
        argv, stdin_text = build_claude_code_invocation(adapter_input)
        return argv, stdin_text

    def make_stdout_transform(self, adapter_input: AdapterInput) -> "StdoutTransform | None":
        """Render Claude's stream-json stdout into compact, human-readable lines.

        With the streaming flags active, raw stdout is one JSON event per line
        and is dominated by echoed tool inputs/results (often >70% of bytes),
        making stdout.log both unreadable and very large. The renderer rewrites
        each event to a short line (assistant text verbatim, tool calls and
        their results condensed to a single line with a size + preview), which
        keeps the dashboard log tail live while shrinking the log ~35x. It also
        captures the final answer for final.md.

        When streaming is disabled (text output), there is nothing to render and
        we passthrough so stdout.log stays the plain text the CLI emits.
        """
        if not _streaming_enabled(adapter_input):
            return None
        options = _adapter_options(adapter_input)
        preview = _option_text(options, ("claude_log_result_preview_chars",))
        try:
            preview_chars = int(preview) if preview else _DEFAULT_RESULT_PREVIEW_CHARS
        except ValueError:
            preview_chars = _DEFAULT_RESULT_PREVIEW_CHARS
        return ClaudeStreamRenderer(result_preview_chars=max(0, preview_chars))

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
    _append_claude_streaming_flags(argv, adapter_input)
    return argv, stdin_text


def _append_claude_streaming_flags(argv: list[str], adapter_input: AdapterInput) -> None:
    """Make the Claude CLI emit incremental stream-json so the dashboard log tail updates live.

    The default ``--print`` text mode buffers the entire response and only
    flushes stdout when the turn ends, so a long-running worker's stdout.log
    stays empty until completion. Streaming JSON writes one event per line as
    work happens, which is what the dashboard's 2s log-tail follows.

    Opt out with ``adapter_options.claude_stream_logs: false`` (reverts to text
    output), and skip injection if the run already configures an output format.
    """
    if not _streaming_enabled(adapter_input, argv):
        return
    # Use the single-token "--flag=value" form so no bare value (e.g.
    # "stream-json") can be mistaken for a positional subcommand by the command
    # policy classifier, which skips "--"-prefixed tokens.
    flags = ["--print", "--output-format=stream-json", "--verbose"]
    # --print and --verbose are idempotent but only add the ones not already
    # present so an operator-supplied --print is respected.
    insert = [flag for flag in flags if not _has_any_flag(argv, {flag})]
    argv[1:1] = insert


def _streaming_enabled(adapter_input: AdapterInput, argv: list[str] | None = None) -> bool:
    """Whether this run uses our injected stream-json output (and thus the renderer).

    True unless the operator opted out via ``claude_stream_logs: false`` or
    already configured an ``--output-format`` (in which case we neither inject
    nor render, leaving their chosen format untouched in stdout.log).
    """
    options = _adapter_options(adapter_input)
    if options.get("claude_stream_logs") is False:
        return False
    if argv is None:
        argv, _ = build_shell_invocation(adapter_input)
    return not _has_any_flag(argv, {"--output-format"})


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


class ClaudeStreamRenderer(StdoutTransform):
    """Render Claude stream-json events into compact human-readable log lines.

    Each raw stdout line is one JSON event. We emit:
      * assistant text blocks verbatim (the actual narration / answer),
      * tool calls as ``🔧 Name(arg)`` one-liners,
      * tool results as ``↳ result (Nb): <preview>`` (size + clipped preview),
      * session start and the terminal result/cost line as short markers.
    Streaming events, partial chunks, and other noise are dropped. Any line that
    is not JSON (text-output mode, a launch error, a banner) is passed through
    unchanged so nothing is silently lost.

    The final answer (the terminal ``result`` text, else concatenated assistant
    text) is accumulated for use as final.md.
    """

    def __init__(self, *, result_preview_chars: int = _DEFAULT_RESULT_PREVIEW_CHARS) -> None:
        self._result_preview_chars = result_preview_chars
        self._result_text: str | None = None
        self._assistant_texts: list[str] = []

    def render_line(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            event = json.loads(stripped)
        except (ValueError, TypeError):
            # Not stream-json (e.g. an error banner): keep it verbatim.
            return line
        if not isinstance(event, Mapping):
            return line
        return self._render_event(event)

    def final_output(self) -> str:
        if self._result_text is not None:
            return self._result_text
        if self._assistant_texts:
            return "\n".join(self._assistant_texts)
        return ""

    def _render_event(self, event: Mapping[str, object]) -> str | None:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            model = event.get("model")
            return f"▶ session start · model={model}" if isinstance(model, str) else "▶ session start"
        if event_type == "assistant":
            return self._render_assistant(event.get("message"))
        if event_type == "user":
            return self._render_tool_results(event.get("message"))
        if event_type == "result":
            return self._render_result(event)
        # stream_event, status, and anything else: drop from the log.
        return None

    def _render_assistant(self, message: object) -> str | None:
        if not isinstance(message, Mapping):
            return None
        content = message.get("content")
        if not isinstance(content, list):
            return None
        lines: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    self._assistant_texts.append(text.rstrip())
                    lines.append(text.rstrip())
            elif block_type == "tool_use":
                lines.append(self._render_tool_use(block))
        return "\n".join(lines) if lines else None

    def _render_tool_use(self, block: Mapping[str, object]) -> str:
        name = block.get("name") if isinstance(block.get("name"), str) else "tool"
        inp = block.get("input")
        arg = ""
        if isinstance(inp, Mapping):
            for key in ("file_path", "command", "path", "pattern", "url", "prompt", "description"):
                value = inp.get(key)
                if isinstance(value, str) and value.strip():
                    arg = value
                    break
        return f"🔧 {name}({_clip(arg, 120)})" if arg else f"🔧 {name}"

    def _render_tool_results(self, message: object) -> str | None:
        if not isinstance(message, Mapping):
            return None
        content = message.get("content")
        if not isinstance(content, list):
            return None
        lines: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                text = _tool_result_text(block.get("content"))
                size = len(text)
                if self._result_preview_chars <= 0:
                    lines.append(f"   ↳ result ({size}b)")
                else:
                    lines.append(f"   ↳ result ({size}b): {_clip(text, self._result_preview_chars)}")
        return "\n".join(lines) if lines else None

    def _render_result(self, event: Mapping[str, object]) -> str | None:
        value = event.get("result")
        if isinstance(value, str):
            self._result_text = value
        duration = event.get("duration_ms")
        cost = event.get("total_cost_usd")
        parts = ["✓ done"]
        if isinstance(duration, (int, float)):
            parts.append(f"{duration / 1000:.0f}s")
        if isinstance(cost, (int, float)) and cost:
            parts.append(f"${cost:.4f}")
        if event.get("is_error"):
            parts.append("⚠ ERROR")
        return " · ".join(parts)


def _clip(value: str, limit: int) -> str:
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…(+{len(collapsed) - limit}b)"


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return "" if content is None else str(content)
