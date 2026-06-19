from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Mapping, Sequence


WORKER_GIT_READ_ONLY_OPERATIONS = ("status", "diff")
WORKER_DENIED_GIT_OPERATIONS = (
    "commit",
    "reset",
    "clean",
    "checkout",
    "switch",
    "branch -D",
    "rebase",
    "push",
    "update-ref",
    "tag",
    "stash",
    "gc",
)
WORKER_DENIED_GIT_BRANCH_MUTATION_OPTIONS = (
    "-D",
    "-d",
    "--delete",
    "-m",
    "-M",
    "--move",
    "-c",
    "-C",
    "--copy",
    "--set-upstream-to",
    "--unset-upstream",
)
VERSION_CONTROL_MANAGER_ONLY_OPERATIONS = (
    "loopplane vc checkpoint",
    "loopplane vc rollback",
)
WORKER_BOUNDARY_ROLES = ("worker", "recovery", "recovery_worker")
VERSION_CONTROL_MANAGER_ROLES = ("version_control_manager", "vc_manager", "vcm")

_SHELL_PROGRAMS = {"bash", "dash", "sh", "zsh"}
_PYTHON_PROGRAMS = {"python", "python3"}
_COMMAND_SEPARATORS = {"&&", "||", ";", "|", "&"}
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--work-tree",
}


@dataclass(frozen=True)
class CommandClassification:
    command: tuple[str, ...]
    matched_command: tuple[str, ...]
    program: str | None
    decision: str
    operation: str | None
    reason: str


@dataclass(frozen=True)
class CommandPolicyDecision:
    allowed: bool
    role: str
    command: tuple[str, ...]
    matched_command: tuple[str, ...]
    decision: str
    operation: str | None
    reason: str


class CommandPolicyViolation(PermissionError):
    def __init__(self, decision: CommandPolicyDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


def classify_command(command: Sequence[str] | str) -> CommandClassification:
    """Classify a command for adapter permission checks.

    The classifier accepts structured argv as the preferred input. It also
    handles common shell wrappers so adapters can preflight commands such as
    ``bash -lc "git status && git commit -m x"`` before execution.
    """

    normalized = _normalize_command(command)
    if not normalized:
        return CommandClassification((), (), None, "empty", None, "No command was supplied.")

    first_read_only: CommandClassification | None = None
    first_unclassified: CommandClassification | None = None
    for segment in _iter_command_segments(normalized):
        classification = _classify_segment(normalized, segment)
        if classification.decision in {
            "git_write",
            "loopplane_vc_checkpoint",
            "loopplane_vc_rollback",
        }:
            return classification
        if classification.decision == "git_read_only" and first_read_only is None:
            first_read_only = classification
        elif first_unclassified is None:
            first_unclassified = classification

    if first_read_only is not None:
        return first_read_only
    if first_unclassified is not None:
        return first_unclassified
    return CommandClassification(
        normalized,
        normalized,
        _program_name(normalized[0]),
        "unclassified",
        None,
        "Command is not a recognized Git or LoopPlane version-control operation.",
    )


def enforce_command_policy(
    *,
    role: str,
    command: Sequence[str] | str,
    permission_policy: Mapping[str, Any] | None = None,
) -> CommandPolicyDecision:
    classification = classify_command(command)
    normalized_role = _normalize_role(role)

    if permission_policy is not None and permission_policy.get("allow_command_execution") is False:
        return _decision(
            False,
            normalized_role,
            classification,
            "command_execution_disabled",
            "Command execution is disabled by the runner permission policy.",
        )

    if classification.decision == "git_write":
        if _unattended_full_access(permission_policy):
            return _decision(
                True,
                normalized_role,
                classification,
                "allowed_unattended_git_write",
                "Unattended full-access runner policy allows write-oriented Git operations.",
            )
        if normalized_role in VERSION_CONTROL_MANAGER_ROLES and classification.operation == "update-ref":
            return _decision(
                True,
                normalized_role,
                classification,
                "allowed_version_control_manager_ref_update",
                "Version Control Manager may update managed checkpoint refs.",
            )
        return _decision(
            False,
            normalized_role,
            classification,
            "blocked_git_write",
            f"Role {normalized_role!r} may not run write-oriented Git operation "
            f"{classification.operation!r}.",
        )

    if classification.decision in {"loopplane_vc_checkpoint", "loopplane_vc_rollback"}:
        if _unattended_full_access(permission_policy):
            return _decision(
                True,
                normalized_role,
                classification,
                f"allowed_unattended_{classification.decision}",
                "Unattended full-access runner policy allows LoopPlane version-control operations.",
            )
        if normalized_role in VERSION_CONTROL_MANAGER_ROLES:
            return _decision(
                True,
                normalized_role,
                classification,
                f"allowed_{classification.decision}",
                "Version Control Manager may use the LoopPlane version-control manager path.",
            )
        return _decision(
            False,
            normalized_role,
            classification,
            f"blocked_{classification.decision}",
            f"Only the Version Control Manager may run {classification.operation}.",
        )

    if classification.decision == "git_read_only":
        return _decision(
            True,
            normalized_role,
            classification,
            "allowed_git_read_only",
            "Read-only Git inspection is allowed.",
        )

    return _decision(
        True,
        normalized_role,
        classification,
        "allowed_unclassified",
        "No Git boundary rule blocks this command.",
    )


def _unattended_full_access(permission_policy: Mapping[str, Any] | None) -> bool:
    if not isinstance(permission_policy, Mapping):
        return True
    if permission_policy.get("read_only") is True:
        return False
    if permission_policy.get("allow_command_execution") is False:
        return False
    return permission_policy.get("require_approval_for_risky_commands") is not True


def assert_command_allowed(
    *,
    role: str,
    command: Sequence[str] | str,
    permission_policy: Mapping[str, Any] | None = None,
) -> CommandPolicyDecision:
    decision = enforce_command_policy(
        role=role,
        command=command,
        permission_policy=permission_policy,
    )
    if not decision.allowed:
        raise CommandPolicyViolation(decision)
    return decision


def git_command_policy_config() -> dict[str, Any]:
    return {
        "enforce_worker_boundaries": False,
        "worker_allowed_read_only_operations": list(WORKER_GIT_READ_ONLY_OPERATIONS),
        "worker_denied_write_operations": list(WORKER_DENIED_GIT_OPERATIONS),
        "worker_denied_branch_mutation_options": list(WORKER_DENIED_GIT_BRANCH_MUTATION_OPTIONS),
        "version_control_manager_only_operations": list(VERSION_CONTROL_MANAGER_ONLY_OPERATIONS),
        "version_control_manager_roles": list(VERSION_CONTROL_MANAGER_ROLES),
        "adapter_enforcement": "runtime.adapters.policy.enforce_command_policy",
    }


def worker_permission_git_policy() -> dict[str, Any]:
    return {
        "git_boundary_policy": "unattended_full_access",
        "allowed_git_read_only_operations": list(WORKER_GIT_READ_ONLY_OPERATIONS),
        "denied_git_write_operations": list(WORKER_DENIED_GIT_OPERATIONS),
        "version_control_manager_only_operations": list(VERSION_CONTROL_MANAGER_ONLY_OPERATIONS),
        "adapter_enforcement": "runtime.adapters.policy.enforce_command_policy",
    }


def _decision(
    allowed: bool,
    role: str,
    classification: CommandClassification,
    decision: str,
    reason: str,
) -> CommandPolicyDecision:
    return CommandPolicyDecision(
        allowed=allowed,
        role=role,
        command=classification.command,
        matched_command=classification.matched_command,
        decision=decision,
        operation=classification.operation,
        reason=reason,
    )


def _classify_segment(
    original: tuple[str, ...],
    segment: tuple[str, ...],
) -> CommandClassification:
    program_index = _first_program_index(segment)
    if program_index is None:
        return CommandClassification(
            original,
            segment,
            None,
            "unclassified",
            None,
            "Command segment has no executable program.",
        )

    program = _program_name(segment[program_index])
    command = segment[program_index:]
    if program == "git":
        return _classify_git_command(original, command)
    if _is_loopplane_program(program):
        return _classify_loopplane_command(original, command)
    if program in _PYTHON_PROGRAMS and len(command) > 1 and _is_loopplane_program(_program_name(command[1])):
        return _classify_loopplane_command(original, command[1:])
    return CommandClassification(
        original,
        command,
        program,
        "unclassified",
        None,
        "Command is not a recognized Git or LoopPlane version-control operation.",
    )


def _classify_git_command(
    original: tuple[str, ...],
    command: tuple[str, ...],
) -> CommandClassification:
    subcommand, args = _git_subcommand(command)
    if subcommand is None:
        return CommandClassification(
            original,
            command,
            "git",
            "unclassified",
            None,
            "Git command has no subcommand.",
        )

    if subcommand in WORKER_GIT_READ_ONLY_OPERATIONS:
        return CommandClassification(
            original,
            command,
            "git",
            "git_read_only",
            subcommand,
            f"git {subcommand} is read-only inspection.",
        )

    if subcommand == "branch":
        mutation = _branch_mutation_operation(args)
        if mutation is not None:
            return CommandClassification(
                original,
                command,
                "git",
                "git_write",
                mutation,
                f"git {mutation} mutates branch metadata.",
            )

    if subcommand in {operation for operation in WORKER_DENIED_GIT_OPERATIONS if " " not in operation}:
        return CommandClassification(
            original,
            command,
            "git",
            "git_write",
            subcommand,
            f"git {subcommand} is write-oriented.",
        )

    return CommandClassification(
        original,
        command,
        "git",
        "git_unclassified",
        subcommand,
        f"git {subcommand} is not covered by the worker write-operation denylist.",
    )


def _classify_loopplane_command(
    original: tuple[str, ...],
    command: tuple[str, ...],
) -> CommandClassification:
    if len(command) >= 3 and command[1] == "vc" and command[2] in {"checkpoint", "rollback"}:
        operation = f"loopplane vc {command[2]}"
        decision = "loopplane_vc_checkpoint" if command[2] == "checkpoint" else "loopplane_vc_rollback"
        return CommandClassification(
            original,
            command,
            "loopplane",
            decision,
            operation,
            f"{operation} is reserved for the Version Control Manager.",
        )
    return CommandClassification(
        original,
        command,
        "loopplane",
        "loopplane_unclassified",
        None,
        "LoopPlane command is not a version-control checkpoint or rollback operation.",
    )


def _git_subcommand(command: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    index = 1
    while index < len(command):
        token = command[index]
        if token == "--":
            index += 1
            continue
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, command[index + 1 :]
    return None, ()


def _branch_mutation_operation(args: tuple[str, ...]) -> str | None:
    for arg in args:
        if arg in WORKER_DENIED_GIT_BRANCH_MUTATION_OPTIONS:
            return "branch -D" if arg in {"-D", "--delete"} else f"branch {arg}"
        if arg.startswith("-") and not arg.startswith("--") and any(flag in arg[1:] for flag in ("D", "d", "m", "M", "c", "C")):
            return "branch -D" if "D" in arg[1:] else f"branch {arg}"
    return None


def _iter_command_segments(command: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    shell_words = _shell_script_words(command)
    if shell_words is not None:
        return _split_segments(shell_words)
    return _split_segments(command)


def _shell_script_words(command: tuple[str, ...]) -> tuple[str, ...] | None:
    if not command:
        return None
    program = _program_name(command[0])
    if program not in _SHELL_PROGRAMS:
        return None

    index = 1
    while index < len(command):
        token = command[index]
        if token == "-c" or (token.startswith("-") and not token.startswith("--") and "c" in token[1:]):
            if index + 1 >= len(command):
                return ()
            return _split_shell_words(command[index + 1])
        index += 1
    return None


def _split_shell_words(value: str) -> tuple[str, ...]:
    lexer = shlex.shlex(value, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return tuple(lexer)


def _split_segments(words: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for word in words:
        if word in _COMMAND_SEPARATORS:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(word)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _first_program_index(segment: tuple[str, ...]) -> int | None:
    for index, token in enumerate(segment):
        if _is_environment_assignment(token):
            continue
        return index
    return None


def _is_environment_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return name.replace("_", "").isalnum() and not name[0].isdigit()


def _normalize_command(command: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(command, str):
        return _split_shell_words(command)
    return tuple(str(part) for part in command)


def _normalize_role(role: str) -> str:
    return role.strip().lower().replace("-", "_").replace(" ", "_")


def _program_name(token: str) -> str:
    return PurePath(token).name.lower()


def _is_loopplane_program(program: str) -> bool:
    return program == "loopplane"
