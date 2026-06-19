"""Agent runner adapters for the LoopPlane runtime."""

from runtime.adapters.base import (
    ADAPTER_INPUT_FILENAME,
    ADAPTER_RESULT_FILENAME,
    DOCTOR_STATUS_OK,
    DOCTOR_STATUS_WAITING_CONFIG,
    AdapterContractError,
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
    AdapterOutputPaths,
    AgentAdapter,
    discover_adapter_produced_files,
    ensure_run_dirs,
    snapshot_adapter_files,
    utc_timestamp,
    write_adapter_input,
    write_adapter_result,
)
from runtime.adapters.claude_code_cli_adapter import ClaudeCodeCliAdapter
from runtime.adapters.codex_cli_adapter import CodexCliAdapter
from runtime.adapters.noop_adapter import NoopAdapter
from runtime.adapters.policy import (
    CommandPolicyDecision,
    CommandPolicyViolation,
    classify_command,
    enforce_command_policy,
)
from runtime.adapters.registry import (
    AdapterLookupError,
    AdapterRegistrationError,
    available_adapter_names,
    get_adapter,
    register_adapter,
)
from runtime.adapters.shell_adapter import ShellAdapter

__all__ = [
    "ADAPTER_RESULT_FILENAME",
    "ADAPTER_INPUT_FILENAME",
    "AdapterLookupError",
    "AdapterRegistrationError",
    "DOCTOR_STATUS_OK",
    "DOCTOR_STATUS_WAITING_CONFIG",
    "AdapterContractError",
    "AdapterDoctorResult",
    "AdapterInput",
    "AdapterOutput",
    "AdapterOutputPaths",
    "AgentAdapter",
    "ClaudeCodeCliAdapter",
    "CodexCliAdapter",
    "CommandPolicyDecision",
    "CommandPolicyViolation",
    "NoopAdapter",
    "ShellAdapter",
    "available_adapter_names",
    "classify_command",
    "discover_adapter_produced_files",
    "ensure_run_dirs",
    "enforce_command_policy",
    "get_adapter",
    "register_adapter",
    "snapshot_adapter_files",
    "utc_timestamp",
    "write_adapter_input",
    "write_adapter_result",
]
