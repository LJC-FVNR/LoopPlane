from __future__ import annotations

from runtime.adapters.base import AgentAdapter
from runtime.adapters.claude_code_cli_adapter import ClaudeCodeCliAdapter
from runtime.adapters.codex_cli_adapter import CodexCliAdapter
from runtime.adapters.noop_adapter import NoopAdapter
from runtime.adapters.shell_adapter import ShellAdapter


class AdapterLookupError(KeyError):
    pass


class AdapterRegistrationError(ValueError):
    pass


ADAPTER_CLASSES: dict[str, type[AgentAdapter]] = {
    "noop": NoopAdapter,
    "noop_adapter": NoopAdapter,
    "shell": ShellAdapter,
    "shell_adapter": ShellAdapter,
    "codex_cli": CodexCliAdapter,
    "codex_cli_adapter": CodexCliAdapter,
    "claude_code_cli": ClaudeCodeCliAdapter,
    "claude_code_cli_adapter": ClaudeCodeCliAdapter,
}


def available_adapter_names() -> tuple[str, ...]:
    return tuple(sorted(ADAPTER_CLASSES))


def register_adapter(adapter_name: str, adapter_class: type[AgentAdapter]) -> None:
    normalized = adapter_name.strip()
    if not normalized:
        raise AdapterRegistrationError("adapter name must be non-empty")
    if not isinstance(adapter_class, type) or not issubclass(adapter_class, AgentAdapter):
        raise AdapterRegistrationError("adapter_class must subclass AgentAdapter")
    ADAPTER_CLASSES[normalized] = adapter_class


def get_adapter(adapter_name: str) -> AgentAdapter:
    normalized = adapter_name.strip()
    try:
        return ADAPTER_CLASSES[normalized]()
    except KeyError as error:
        raise AdapterLookupError(f"unknown adapter: {adapter_name}") from error
