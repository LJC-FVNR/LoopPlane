from __future__ import annotations

import os
import signal
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import (
    DOCTOR_STATUS_OK,
    DOCTOR_STATUS_WAITING_CONFIG,
    AdapterContractError,
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
    AgentAdapter,
    discover_adapter_produced_files,
    snapshot_adapter_files,
    utc_timestamp,
    write_adapter_input,
    write_adapter_result,
)
from runtime.adapters.boundary import (
    AdapterBoundarySnapshot,
    evaluate_adapter_workspace_boundary,
    observed_boundary_change_paths,
    snapshot_adapter_workspace_boundary,
)
from runtime.adapters.policy import CommandPolicyDecision, enforce_command_policy
from runtime.adapters.runner_availability import classify_runner_availability
from runtime.exit_codes import (
    ADAPTER_COMMAND_UNAVAILABLE_EXIT_CODE,
    ADAPTER_POLICY_BLOCKED_EXIT_CODE,
    ADAPTER_TIMEOUT_EXIT_CODE,
)
from runtime.runner_locks import acquire_runner_resource_lock, with_runner_resource_lock_metadata


TIMEOUT_EXIT_CODE = ADAPTER_TIMEOUT_EXIT_CODE
POLICY_BLOCKED_EXIT_CODE = ADAPTER_POLICY_BLOCKED_EXIT_CODE
COMMAND_UNAVAILABLE_EXIT_CODE = ADAPTER_COMMAND_UNAVAILABLE_EXIT_CODE
DOCTOR_COMMAND_TIMEOUT_SECONDS = 10
DOCTOR_OUTPUT_EXCERPT_LIMIT = 500
PROCESS_POLL_SECONDS = 0.2
PROCESS_TERMINATE_GRACE_SECONDS = 5.0
DEFAULT_COMPLETION_MARKER_GRACE_SECONDS_BY_ROLE = {
    "expansion_planner": 5.0,
    "final_reviewer": 5.0,
    "objective_verifier": 5.0,
    "summary": 5.0,
}
SUPPORTED_PROMPT_DELIVERY_MODES = frozenset(
    {
        "stdin",
        "file_argument",
        "stdin_or_prompt_flag",
    }
)
UNREPRESENTABLE_PROMPT_DELIVERY_MESSAGES = {
    "interactive_terminal": (
        "Prompt delivery mode 'interactive_terminal' requires a terminal-capable "
        "custom adapter. The shell/Codex/Claude subprocess adapters cannot "
        "represent a live interactive terminal, so this runner must remain in "
        "waiting_config until it is reconfigured to stdin, file_argument, "
        "stdin_or_prompt_flag, or a registered terminal adapter."
    ),
    "custom_adapter": (
        "Prompt delivery mode 'custom_adapter' requires a registered custom "
        "adapter implementation. The shell/Codex/Claude subprocess adapters "
        "cannot infer custom delivery behavior from configuration alone."
    ),
}


class ShellAdapter(AgentAdapter):
    adapter_name = "shell"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        adapter_input.ensure_run_dirs()
        paths = adapter_input.output_paths()
        adapter_input_path = write_adapter_input(adapter_input)
        pre_run_files = snapshot_adapter_files(adapter_input)
        boundary_snapshot = snapshot_adapter_workspace_boundary(adapter_input)
        started_at = utc_timestamp()
        argv, stdin_text, invocation_metadata = self.prepare_invocation(adapter_input)
        policy_decision = enforce_command_policy(
            role=adapter_input.role,
            command=argv,
            permission_policy=adapter_input.permission_policy,
        )
        if not policy_decision.allowed:
            paths.stdout_path.write_text("", encoding="utf-8")
            paths.stderr_path.write_text(policy_decision.reason + "\n", encoding="utf-8")
            paths.final_output_path.write_text("Command blocked by permission policy.\n", encoding="utf-8")
            return _write_process_result(
                adapter_input,
                started_at=started_at,
                exit_code=POLICY_BLOCKED_EXIT_CODE,
                timed_out=False,
                pre_run_files=pre_run_files,
                boundary_snapshot=boundary_snapshot,
                adapter_input_path=adapter_input_path,
                adapter_metadata={
                    **invocation_metadata,
                    "argv": argv,
                    "delivery_mode": adapter_input.prompt_delivery.get("mode"),
                    "policy_decision": _policy_decision_dict(policy_decision),
                    "external_execution": False,
                },
            )

        env = _process_env(adapter_input, paths)
        # A stdout transform (e.g. CLI adapter JSON renderers)
        # rewrites each line before it lands in stdout.log; absent one, stdout is
        # redirected straight to the file with zero overhead (plain shell path).
        stdout_transform = self.make_stdout_transform(adapter_input)
        with acquire_runner_resource_lock(adapter_input) as runner_lock:
            process: subprocess.Popen[str] | None = None
            process_result: Mapping[str, Any] = {
                "exit_code": COMMAND_UNAVAILABLE_EXIT_CODE,
                "timed_out": False,
            }
            try:
                paths.stdout_path.parent.mkdir(parents=True, exist_ok=True)
                paths.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                with paths.stdout_path.open("w", encoding="utf-8") as stdout_file, paths.stderr_path.open(
                    "w",
                    encoding="utf-8",
                ) as stderr_file:
                    process = subprocess.Popen(
                        argv,
                        cwd=adapter_input.cwd,
                        env=env,
                        stdin=subprocess.PIPE if stdin_text is not None else None,
                        stdout=subprocess.PIPE if stdout_transform is not None else stdout_file,
                        stderr=stderr_file,
                        text=True,
                        start_new_session=True,
                    )
                    _write_adapter_child_pid_file(env.get("LOOPPLANE_ADAPTER_CHILD_PID_FILE"), process.pid)
                    pump_thread: threading.Thread | None = None
                    if stdout_transform is not None and process.stdout is not None:
                        pump_thread = threading.Thread(
                            target=_pump_transformed_stdout,
                            args=(process.stdout, stdout_file, stdout_transform),
                            daemon=True,
                        )
                        pump_thread.start()
                    _write_process_stdin(process, stdin_text)
                    process_result = _wait_for_process(
                        process,
                        timeout_seconds=adapter_input.timeout_seconds,
                        completion_marker_path=_completion_marker_path(adapter_input),
                        completion_marker_grace_seconds=_completion_marker_grace_seconds(adapter_input),
                    )
                    if pump_thread is not None:
                        # The pipe reaches EOF once the child (and its group) exit,
                        # so the pump drains and returns; bound the wait defensively.
                        pump_thread.join(timeout=30)
            except OSError as error:
                if process is not None and process.poll() is None:
                    _terminate_process_group(process)
                paths.stdout_path.write_text("", encoding="utf-8")
                paths.stderr_path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
                _write_default_final_output(
                    paths.final_output_path,
                    stdout="",
                    stderr=str(error),
                    exit_code=COMMAND_UNAVAILABLE_EXIT_CODE,
                    timed_out=False,
                )
                return _write_process_result(
                    adapter_input,
                    started_at=started_at,
                    exit_code=COMMAND_UNAVAILABLE_EXIT_CODE,
                    timed_out=False,
                    pre_run_files=pre_run_files,
                    boundary_snapshot=boundary_snapshot,
                    adapter_input_path=adapter_input_path,
                    adapter_metadata=with_runner_resource_lock_metadata(
                        {
                            **invocation_metadata,
                            "argv": argv,
                            "delivery_mode": adapter_input.prompt_delivery.get("mode"),
                            "policy_decision": _policy_decision_dict(policy_decision),
                            "external_execution": False,
                            "error_type": type(error).__name__,
                        },
                        runner_lock,
                    ),
                )

            if process_result.get("timed_out"):
                completed_stdout = _read_output_text(paths.stdout_path)
                completed_stderr = _read_output_text(paths.stderr_path)
                _write_default_final_output(
                    paths.final_output_path,
                    stdout=_final_output_text(stdout_transform, completed_stdout),
                    stderr=completed_stderr,
                    exit_code=TIMEOUT_EXIT_CODE,
                    timed_out=True,
                )
                return _write_process_result(
                    adapter_input,
                    started_at=started_at,
                    exit_code=TIMEOUT_EXIT_CODE,
                    timed_out=True,
                    pre_run_files=pre_run_files,
                    boundary_snapshot=boundary_snapshot,
                    adapter_input_path=adapter_input_path,
                    adapter_metadata=with_runner_resource_lock_metadata(
                        {
                            **invocation_metadata,
                            "argv": argv,
                            "delivery_mode": adapter_input.prompt_delivery.get("mode"),
                            "policy_decision": _policy_decision_dict(policy_decision),
                            "external_execution": True,
                            "child_pid": process.pid if process is not None else None,
                            **_process_result_metadata(process_result),
                        },
                        runner_lock,
                    ),
                )

            exit_code = int(
                process_result.get(
                    "exit_code",
                    process.returncode if process is not None else COMMAND_UNAVAILABLE_EXIT_CODE,
                )
            )
            completed_stdout = _read_output_text(paths.stdout_path)
            completed_stderr = _read_output_text(paths.stderr_path)
            if not _has_text(paths.final_output_path):
                _write_default_final_output(
                    paths.final_output_path,
                    stdout=_final_output_text(stdout_transform, completed_stdout),
                    stderr=completed_stderr,
                    exit_code=exit_code,
                    timed_out=False,
                )
            return _write_process_result(
                adapter_input,
                started_at=started_at,
                exit_code=exit_code,
                timed_out=False,
                pre_run_files=pre_run_files,
                boundary_snapshot=boundary_snapshot,
                adapter_input_path=adapter_input_path,
                adapter_metadata=with_runner_resource_lock_metadata(
                    {
                        **invocation_metadata,
                        "argv": argv,
                        "delivery_mode": adapter_input.prompt_delivery.get("mode"),
                        "policy_decision": _policy_decision_dict(policy_decision),
                        "external_execution": True,
                        "child_pid": process.pid if process is not None else None,
                        **_process_result_metadata(process_result),
                    },
                    runner_lock,
                ),
            )

    def doctor(self, adapter_input: AdapterInput) -> AdapterDoctorResult:
        checks = shell_doctor_checks(adapter_input, invocation_builder=self.build_invocation)
        status = _aggregate_doctor_status(checks)
        message = "Shell adapter is available." if status == DOCTOR_STATUS_OK else "Shell adapter requires configuration."
        if status == DOCTOR_STATUS_OK:
            return AdapterDoctorResult.ok(
                adapter_input,
                checks=checks,
                message=message,
                adapter_metadata={"external_execution": False},
            )
        return AdapterDoctorResult.waiting_config(
            adapter_input,
            checks=checks,
            message=message,
            adapter_metadata={"external_execution": False},
        )

    def build_invocation(self, adapter_input: AdapterInput) -> tuple[list[str], str | None]:
        return build_shell_invocation(adapter_input)

    def prepare_invocation(
        self,
        adapter_input: AdapterInput,
    ) -> tuple[list[str], str | None, Mapping[str, Any]]:
        argv, stdin_text = self.build_invocation(adapter_input)
        return argv, stdin_text, {}

    def make_stdout_transform(self, adapter_input: AdapterInput) -> "StdoutTransform | None":
        """Return a line transform for the child's stdout, or None for raw passthrough.

        The base shell adapter (and the codex adapter) return None, so stdout is
        redirected straight to stdout.log with no extra threads or copies.
        Subclasses whose stdout is a machine format (e.g. the Claude adapter's
        stream-json) return a transform that renders each line to a compact,
        human-readable form before it is written, and exposes the final answer
        for final.md via ``final_output()``.
        """
        return None


class StdoutTransform:
    """Renders a child process's stdout line-by-line as it streams.

    ``render_line`` is called for each raw stdout line and returns the text to
    write to stdout.log (without a trailing newline), or None to drop the line.
    ``final_output`` returns the human-readable final answer accumulated across
    the stream, used as the fallback contents of final.md.
    """

    def render_line(self, line: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def final_output(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


def _pump_transformed_stdout(source: Any, sink: Any, transform: StdoutTransform) -> None:
    """Read raw stdout lines, render each via the transform, and flush to the log.

    Runs on a dedicated thread so rendered lines reach stdout.log as the child
    emits them, keeping the dashboard's live log tail current. Flushing per line
    is what makes the output observable mid-run.
    """
    try:
        for raw_line in source:
            rendered = transform.render_line(raw_line.rstrip("\n"))
            if rendered is None:
                continue
            sink.write(rendered if rendered.endswith("\n") else rendered + "\n")
            sink.flush()
    except (OSError, ValueError):
        # Pipe closed underneath us (process killed / log file closed on timeout);
        # nothing more to drain.
        return
    finally:
        try:
            source.close()
        except OSError:
            pass


def _final_output_text(transform: "StdoutTransform | None", completed_stdout: str) -> str:
    if transform is not None:
        return transform.final_output()
    return completed_stdout


def build_shell_invocation(adapter_input: AdapterInput) -> tuple[list[str], str | None]:
    try:
        argv = shlex.split(adapter_input.command)
    except ValueError as error:
        raise AdapterContractError(f"command cannot be parsed: {error}") from error
    if not argv:
        raise AdapterContractError("command cannot be empty")

    argv.extend(_expand_template(value, adapter_input) for value in adapter_input.args)
    mode = str(adapter_input.prompt_delivery.get("mode", "stdin"))
    stdin_text: str | None = None
    if mode == "stdin":
        stdin_text = adapter_input.prompt_content
    elif mode == "file_argument":
        template = adapter_input.prompt_delivery.get("argument_template")
        if template is None:
            argv.append(adapter_input.prompt_path.as_posix())
        else:
            argv.append(_expand_template(str(template), adapter_input))
    elif mode == "stdin_or_prompt_flag":
        prompt_flag = adapter_input.prompt_delivery.get("prompt_flag")
        prompt_file = adapter_input.prompt_delivery.get("prompt_file")
        if prompt_flag is not None and prompt_file is not None:
            argv.extend(
                (
                    _expand_template(str(prompt_flag), adapter_input),
                    _expand_template(str(prompt_file), adapter_input),
                )
            )
        elif prompt_file is not None:
            argv.append(_expand_template(str(prompt_file), adapter_input))
        else:
            stdin_text = adapter_input.prompt_content
    else:
        raise AdapterContractError(_unsupported_prompt_delivery_message(mode))
    return argv, stdin_text


def shell_doctor_checks(
    adapter_input: AdapterInput,
    *,
    invocation_builder: Any = build_shell_invocation,
    executable_resolver: Any = None,
) -> tuple[Mapping[str, Any], ...]:
    checks: list[Mapping[str, Any]] = []
    doctor_config = _doctor_config(adapter_input)
    process_env = _process_env(adapter_input, adapter_input.output_paths())
    mode = str(adapter_input.prompt_delivery.get("mode", ""))
    checks.append(
        _doctor_check(
            "prompt_delivery",
            DOCTOR_STATUS_OK if mode in SUPPORTED_PROMPT_DELIVERY_MODES else DOCTOR_STATUS_WAITING_CONFIG,
            (
                f"Prompt delivery mode {mode!r} is supported by the shell adapter."
                if mode in SUPPORTED_PROMPT_DELIVERY_MODES
                else _unsupported_prompt_delivery_message(mode)
            ),
            "prompt_delivery_supported" if mode in SUPPORTED_PROMPT_DELIVERY_MODES else "unsupported_prompt_delivery",
        )
    )

    cwd = Path(adapter_input.cwd)
    cwd_is_dir = cwd.is_dir()
    checks.append(
        _doctor_check(
            "cwd",
            DOCTOR_STATUS_OK if cwd_is_dir else DOCTOR_STATUS_WAITING_CONFIG,
            f"Working directory exists: {cwd}" if cwd_is_dir else f"Working directory is missing: {cwd}",
            "cwd_ok" if cwd_is_dir else "cwd_missing",
            path=cwd.as_posix(),
        )
    )

    command_available = False
    try:
        command_parts = shlex.split(adapter_input.command)
    except ValueError as error:
        command_parts = []
        checks.append(
            _doctor_check(
                "command_parse",
                DOCTOR_STATUS_WAITING_CONFIG,
                f"Command cannot be parsed: {error}",
                "command_parse_failed",
            )
        )
    if command_parts:
        program = command_parts[0]
        resolution = _resolve_doctor_executable(
            program,
            process_env=process_env,
            cwd=adapter_input.cwd,
            executable_resolver=executable_resolver,
        )
        resolved_program = resolution.get("resolved_path")
        command_available = resolved_program is not None
        recovered = command_available and resolution.get("recovered") is True
        if recovered:
            command_status = "warning"
            command_message = f"Recovered unavailable command {program} as {resolved_program}."
            command_code = "command_recovered"
        elif command_available:
            command_status = DOCTOR_STATUS_OK
            command_message = f"Command program is available: {program}"
            command_code = "command_available"
        else:
            command_status = DOCTOR_STATUS_WAITING_CONFIG
            command_message = f"Command program was not found on PATH: {program}"
            command_code = "command_missing"
        checks.append(
            _doctor_check(
                "command_exists",
                command_status,
                command_message,
                command_code,
                program=program,
                resolved_path=resolved_program,
                resolution_source=resolution.get("source"),
                recovered=recovered,
            )
        )

    checks.extend(_output_directory_checks(adapter_input))

    argv_for_policy: tuple[str, ...] | list[str] = tuple(command_parts or (adapter_input.command,))
    try:
        argv_for_policy, _stdin_text = invocation_builder(adapter_input)
    except AdapterContractError as error:
        checks.append(
            _doctor_check(
                "invocation",
                DOCTOR_STATUS_WAITING_CONFIG,
                str(error),
                "invocation_invalid",
            )
        )

    policy_decision = enforce_command_policy(
        role=adapter_input.role,
        command=argv_for_policy,
        permission_policy=adapter_input.permission_policy,
    )
    checks.append(
        _doctor_check(
            "permission_policy",
            DOCTOR_STATUS_OK if policy_decision.allowed else DOCTOR_STATUS_WAITING_CONFIG,
            policy_decision.reason,
            "policy_ok" if policy_decision.allowed else "policy_mismatch",
            decision=policy_decision.decision,
        )
    )

    if command_available and cwd_is_dir:
        version_command = _doctor_string(doctor_config.get("check_command"))
        if version_command is not None:
            check_kind = str(doctor_config.get("check_kind") or "").strip()
            explicit_doctor_check = check_kind == "doctor_check" or (
                adapter_input.adapter == "shell" and check_kind != "version_command"
            )
            check_name = "doctor_check" if explicit_doctor_check else "version_command"
            success_code = "doctor_check_ok" if explicit_doctor_check else "version_command_ok"
            failure_code = "doctor_check_failed" if explicit_doctor_check else "version_command_failed"
            success_message = "Doctor check command succeeded." if explicit_doctor_check else "Version command succeeded."
            failure_message = "Doctor check command failed." if explicit_doctor_check else "Version command failed."
            checks.append(
                _run_doctor_command_check(
                    adapter_input,
                    version_command,
                    check_name=check_name,
                    success_code=success_code,
                    failure_code=failure_code,
                    success_message=success_message,
                    failure_message=failure_message,
                    executable_resolver=executable_resolver,
                )
            )

        checks.append(
            _authentication_check(
                adapter_input,
                doctor_config,
                process_env,
                executable_resolver=executable_resolver,
            )
        )
    return tuple(checks)


def _resolve_doctor_executable(
    program: str,
    *,
    process_env: Mapping[str, str],
    cwd: str,
    executable_resolver: Any,
) -> dict[str, Any]:
    if executable_resolver is not None:
        resolution = executable_resolver(program, env=process_env, cwd=cwd)
        if isinstance(resolution, Mapping):
            return dict(resolution)
        to_dict = getattr(resolution, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, Mapping):
                return dict(data)

    resolved_path = shutil.which(program, path=process_env.get("PATH"))
    return {
        "configured_program": program,
        "invocation_program": program,
        "resolved_path": resolved_path,
        "source": "path" if resolved_path is not None else "unresolved",
        "recovered": False,
    }


def _aggregate_doctor_status(checks: tuple[Mapping[str, Any], ...]) -> str:
    if any(check.get("status") == DOCTOR_STATUS_WAITING_CONFIG for check in checks):
        return DOCTOR_STATUS_WAITING_CONFIG
    return DOCTOR_STATUS_OK


def _unsupported_prompt_delivery_message(mode: str) -> str:
    return UNREPRESENTABLE_PROMPT_DELIVERY_MESSAGES.get(
        mode,
        f"Prompt delivery mode {mode!r} is not supported by the shell adapter.",
    )


def _doctor_check(
    name: str,
    status: str,
    message: str,
    code: str,
    **extra: Any,
) -> dict[str, Any]:
    check = {
        "name": name,
        "status": status,
        "message": message,
        "code": code,
    }
    check.update(extra)
    return check


def _doctor_config(adapter_input: AdapterInput) -> Mapping[str, Any]:
    raw = adapter_input.runner_config.get("doctor")
    return raw if isinstance(raw, Mapping) else {}


def _doctor_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _doctor_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _doctor_command_timeout_seconds(adapter_input: AdapterInput) -> int:
    raw = _doctor_config(adapter_input).get("timeout_seconds", DOCTOR_COMMAND_TIMEOUT_SECONDS)
    if isinstance(raw, bool):
        return DOCTOR_COMMAND_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        return DOCTOR_COMMAND_TIMEOUT_SECONDS
    return max(1, timeout)


def _output_directory_checks(adapter_input: AdapterInput) -> tuple[Mapping[str, Any], ...]:
    candidates: list[tuple[str, Path]] = [
        ("scheduler_run_dir", adapter_input.scheduler_run_dir),
        ("role_output_dir", adapter_input.role_output_dir),
    ]
    if adapter_input.task_evidence_run_dir is not None:
        candidates.append(("task_evidence_run_dir", adapter_input.task_evidence_run_dir))
    return tuple(_output_directory_check(kind, path) for kind, path in candidates)


def _output_directory_check(kind: str, path: Path) -> Mapping[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(path.as_posix())
        probe = path / f".loopplane_doctor_write_test_{os.getpid()}"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as error:
        return _doctor_check(
            "output_directory",
            DOCTOR_STATUS_WAITING_CONFIG,
            f"{kind} is not writable: {path}: {error}",
            "output_directory_unwritable",
            path_kind=kind,
            path=path.as_posix(),
            error_type=type(error).__name__,
        )
    return _doctor_check(
        "output_directory",
        DOCTOR_STATUS_OK,
        f"{kind} is writable: {path}",
        "output_directory_writable",
        path_kind=kind,
        path=path.as_posix(),
    )


def _authentication_check(
    adapter_input: AdapterInput,
    doctor_config: Mapping[str, Any],
    process_env: Mapping[str, str],
    *,
    executable_resolver: Any = None,
) -> Mapping[str, Any]:
    requires_auth = doctor_config.get("requires_auth") is True
    if not requires_auth:
        return _doctor_check(
            "authentication",
            DOCTOR_STATUS_OK,
            "Authentication is not required for this runner.",
            "authentication_not_required",
        )

    auth_env_vars = _doctor_string_list(doctor_config.get("auth_env_vars"))
    missing_env_vars = [name for name in auth_env_vars if not process_env.get(name)]
    if missing_env_vars:
        return _doctor_check(
            "authentication",
            DOCTOR_STATUS_WAITING_CONFIG,
            "Authentication is unavailable; required auth environment variables are missing.",
            "authentication_unavailable",
            missing_env_vars=missing_env_vars,
        )

    auth_check_command = _doctor_string(
        doctor_config.get("auth_check_command", doctor_config.get("check_auth_command"))
    )
    if auth_check_command is not None:
        return _run_doctor_command_check(
            adapter_input,
            auth_check_command,
            check_name="authentication",
            success_code="authentication_available",
            failure_code="authentication_unavailable",
            success_message="Authentication check succeeded.",
            failure_message="Authentication check failed.",
            executable_resolver=executable_resolver,
        )

    if auth_env_vars:
        return _doctor_check(
            "authentication",
            DOCTOR_STATUS_OK,
            "Authentication environment variables are present.",
            "authentication_available",
            auth_env_vars=auth_env_vars,
        )

    return _doctor_check(
        "authentication",
        DOCTOR_STATUS_OK,
        "Authentication is required, but no auth check command or auth_env_vars are configured.",
        "authentication_check_not_configured",
    )


def _run_doctor_command_check(
    adapter_input: AdapterInput,
    command: str,
    *,
    check_name: str,
    success_code: str,
    failure_code: str,
    success_message: str,
    failure_message: str,
    executable_resolver: Any = None,
) -> Mapping[str, Any]:
    expanded_command = _expand_template(command, adapter_input)
    try:
        argv = shlex.split(expanded_command)
    except ValueError as error:
        return _doctor_check(
            check_name,
            DOCTOR_STATUS_WAITING_CONFIG,
            f"{failure_message} Command cannot be parsed: {error}",
            failure_code,
            command=expanded_command,
            error_type=type(error).__name__,
        )
    if not argv:
        return _doctor_check(
            check_name,
            DOCTOR_STATUS_WAITING_CONFIG,
            f"{failure_message} Command is empty.",
            failure_code,
            command=expanded_command,
        )

    configured_argv = list(argv)
    process_env = _process_env(adapter_input, adapter_input.output_paths())
    resolution = _resolve_doctor_executable(
        argv[0],
        process_env=process_env,
        cwd=adapter_input.cwd,
        executable_resolver=executable_resolver,
    )
    invocation_program = resolution.get("invocation_program")
    if isinstance(invocation_program, str) and invocation_program:
        argv[0] = invocation_program

    try:
        completed = subprocess.run(
            argv,
            cwd=adapter_input.cwd,
            env=process_env,
            text=True,
            capture_output=True,
            timeout=_doctor_command_timeout_seconds(adapter_input),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return _doctor_check(
            check_name,
            DOCTOR_STATUS_WAITING_CONFIG,
            f"{failure_message} Command timed out.",
            failure_code,
            command=expanded_command,
            configured_argv=configured_argv,
            argv=argv,
            executable_resolution=resolution,
            timed_out=True,
            stdout_excerpt=_excerpt(_coerce_text(error.stdout)),
            stderr_excerpt=_excerpt(_coerce_text(error.stderr)),
            error_type=type(error).__name__,
        )
    except OSError as error:
        return _doctor_check(
            check_name,
            DOCTOR_STATUS_WAITING_CONFIG,
            f"{failure_message} {type(error).__name__}: {error}",
            failure_code,
            command=expanded_command,
            configured_argv=configured_argv,
            argv=argv,
            executable_resolution=resolution,
            error_type=type(error).__name__,
        )

    if completed.returncode == 0:
        return _doctor_check(
            check_name,
            DOCTOR_STATUS_OK,
            success_message,
            success_code,
            command=expanded_command,
            configured_argv=configured_argv,
            argv=argv,
            executable_resolution=resolution,
            exit_code=completed.returncode,
            stdout_excerpt=_excerpt(completed.stdout),
            stderr_excerpt=_excerpt(completed.stderr),
        )
    return _doctor_check(
        check_name,
        DOCTOR_STATUS_WAITING_CONFIG,
        f"{failure_message} Exit code {completed.returncode}.",
        failure_code,
        command=expanded_command,
        configured_argv=configured_argv,
        argv=argv,
        executable_resolution=resolution,
        exit_code=completed.returncode,
        stdout_excerpt=_excerpt(completed.stdout),
        stderr_excerpt=_excerpt(completed.stderr),
    )


def _write_process_result(
    adapter_input: AdapterInput,
    *,
    started_at: str,
    exit_code: int,
    timed_out: bool,
    pre_run_files: Sequence[Path | str],
    boundary_snapshot: AdapterBoundarySnapshot,
    adapter_input_path: Path,
    adapter_metadata: Mapping[str, Any],
) -> AdapterOutput:
    paths = adapter_input.output_paths()
    # Runner availability describes the external runner process, not a local
    # post-run policy verdict.  Preserve the real process exit code before a
    # workspace-boundary violation is translated to POLICY_BLOCKED_EXIT_CODE;
    # otherwise an unrelated warning in a successful runner's stderr can be
    # misclassified as a persistent runner configuration failure.
    process_exit_code = exit_code
    produced_files = discover_adapter_produced_files(
        adapter_input,
        before=pre_run_files,
        explicit=(
            adapter_input_path,
            paths.stdout_path,
            paths.stderr_path,
            paths.final_output_path,
            paths.adapter_result_path,
        ),
    )
    metadata = dict(adapter_metadata)
    boundary_policy = evaluate_adapter_workspace_boundary(adapter_input, boundary_snapshot)
    if boundary_policy.get("enforced"):
        produced_files = _merge_produced_files(produced_files, observed_boundary_change_paths(boundary_policy))
        metadata["workspace_boundary_policy"] = boundary_policy
    if boundary_policy.get("enforced") and not boundary_policy.get("ok", True):
        metadata["process_exit_code"] = process_exit_code
        exit_code = POLICY_BLOCKED_EXIT_CODE
        _append_boundary_violation(paths.stderr_path, boundary_policy)
        _write_boundary_final_output(paths.final_output_path, boundary_policy)
    if "runner_availability" not in metadata:
        availability = classify_runner_availability(
            adapter_input,
            exit_code=process_exit_code,
            timed_out=timed_out,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            final_output_path=paths.final_output_path,
        )
        if availability is not None:
            metadata["runner_availability"] = availability
    output = AdapterOutput.from_input(
        adapter_input,
        started_at=started_at,
        ended_at=utc_timestamp(),
        exit_code=exit_code,
        timed_out=timed_out,
        produced_files=produced_files,
        adapter_metadata=metadata,
    )
    write_adapter_result(output)
    return output


def _write_process_stdin(process: subprocess.Popen[str], stdin_text: str | None) -> None:
    if stdin_text is None or process.stdin is None:
        return
    try:
        process.stdin.write(stdin_text)
        process.stdin.flush()
    except BrokenPipeError:
        return
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def _wait_for_process(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: int,
    completion_marker_path: Path | None,
    completion_marker_grace_seconds: float | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    marker_seen_at: float | None = None
    while True:
        return_code = process.poll()
        if return_code is not None:
            return {
                "exit_code": return_code,
                "timed_out": False,
                "process_exit_code": return_code,
                "process_group_id": process.pid,
            }

        now = time.monotonic()
        if now >= deadline:
            process_exit_code = _terminate_process_group(process)
            return {
                "exit_code": TIMEOUT_EXIT_CODE,
                "timed_out": True,
                "process_exit_code": process_exit_code,
                "process_group_id": process.pid,
                "termination_reason": "timeout",
            }

        if (
            completion_marker_path is not None
            and completion_marker_grace_seconds is not None
            and completion_marker_path.is_file()
        ):
            marker_seen_at = marker_seen_at if marker_seen_at is not None else now
            if now - marker_seen_at >= completion_marker_grace_seconds:
                process_exit_code = _terminate_process_group(process)
                return {
                    "exit_code": 0,
                    "timed_out": False,
                    "process_exit_code": process_exit_code,
                    "process_group_id": process.pid,
                    "termination_reason": "completion_marker",
                    "terminated_after_completion_marker": True,
                    "completion_marker_path": completion_marker_path.as_posix(),
                    "completion_marker_grace_seconds": completion_marker_grace_seconds,
                }

        time.sleep(PROCESS_POLL_SECONDS)


def _terminate_process_group(process: subprocess.Popen[str]) -> int | None:
    if process.poll() is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.poll()
    except OSError:
        process.terminate()
    try:
        return process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return process.poll()
        except OSError:
            process.kill()
        try:
            return process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return process.poll()


def _completion_marker_path(adapter_input: AdapterInput) -> Path | None:
    if _completion_marker_grace_seconds(adapter_input) is None:
        return None
    return adapter_input.role_output_dir / "agent_status.json"


def _completion_marker_grace_seconds(adapter_input: AdapterInput) -> float | None:
    options = _adapter_options(adapter_input)
    explicit = _float_option(
        options,
        (
            "completion_marker_grace_seconds",
            "agent_status_completion_grace_seconds",
            "complete_after_agent_status_seconds",
        ),
    )
    if explicit is not None:
        return explicit
    if _explicit_false_option(options, ("complete_after_agent_status", "completion_marker_enabled")):
        return None
    return DEFAULT_COMPLETION_MARKER_GRACE_SECONDS_BY_ROLE.get(adapter_input.role)


def _process_result_metadata(process_result: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "process_group_id",
        "process_exit_code",
        "termination_reason",
        "terminated_after_completion_marker",
        "completion_marker_path",
        "completion_marker_grace_seconds",
    ):
        if key in process_result:
            metadata[key] = process_result[key]
    return metadata


def _adapter_options(adapter_input: AdapterInput) -> Mapping[str, Any]:
    runner_config = adapter_input.runner_config
    raw = runner_config.get("adapter_options") if isinstance(runner_config, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def _float_option(options: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = options.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _explicit_false_option(options: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(options.get(key) is False for key in keys)


def _merge_produced_files(
    produced_files: Sequence[Path | str],
    additional_paths: Sequence[Path | str],
) -> tuple[Path, ...]:
    merged: dict[str, Path] = {}
    for path in (*produced_files, *additional_paths):
        item = Path(path)
        merged[item.as_posix()] = item
    return tuple(merged[key] for key in sorted(merged))


def _append_boundary_violation(path: Path, boundary_policy: Mapping[str, Any]) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    message = _boundary_violation_message(boundary_policy)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{message}\n", encoding="utf-8")


def _write_boundary_final_output(path: Path, boundary_policy: Mapping[str, Any]) -> None:
    path.write_text(
        f"Adapter blocked by workspace boundary policy: {_boundary_violation_message(boundary_policy)}\n",
        encoding="utf-8",
    )


def _boundary_violation_message(boundary_policy: Mapping[str, Any]) -> str:
    changes = boundary_policy.get("observed_changes")
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)) or not changes:
        return "out-of-boundary adapter change detected"
    paths = [
        str(change.get("path"))
        for change in changes
        if isinstance(change, Mapping) and isinstance(change.get("path"), str)
    ]
    if not paths:
        return "out-of-boundary adapter change detected"
    return "out-of-boundary adapter change(s): " + ", ".join(paths)


def _write_default_final_output(
    path: Path,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    timed_out: bool,
) -> None:
    if stdout:
        path.write_text(stdout, encoding="utf-8")
        return
    if stderr:
        path.write_text(stderr, encoding="utf-8")
        return
    status = "timed out" if timed_out else f"exited {exit_code}"
    path.write_text(f"Shell adapter command {status} with no output.\n", encoding="utf-8")


def _process_env(adapter_input: AdapterInput, paths: Any) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_project_dotenv_env(adapter_input, env))
    env.update(dict(adapter_input.env))
    _configure_isolated_codex_home(adapter_input, env)
    env.update(
        {
            "LOOPPLANE_RUN_ID": adapter_input.run_id,
            "LOOPPLANE_WORKFLOW_ID": adapter_input.workflow_id,
            "LOOPPLANE_RUNNER_ID": adapter_input.runner_id,
            "LOOPPLANE_ROLE": adapter_input.role,
            "LOOPPLANE_PROMPT_PATH": adapter_input.prompt_path.as_posix(),
            "LOOPPLANE_STDOUT_LOG": paths.stdout_path.as_posix(),
            "LOOPPLANE_STDERR_LOG": paths.stderr_path.as_posix(),
            "LOOPPLANE_FINAL_OUTPUT": paths.final_output_path.as_posix(),
            "LOOPPLANE_ADAPTER_RESULT": paths.adapter_result_path.as_posix(),
        }
    )
    if adapter_input.task_id is not None:
        env["LOOPPLANE_TASK_ID"] = adapter_input.task_id
    if adapter_input.task_evidence_run_dir is not None:
        env["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"] = adapter_input.task_evidence_run_dir.as_posix()
    return env


def adapter_process_env(adapter_input: AdapterInput) -> dict[str, str]:
    """Return the exact environment used by shell-family child processes."""

    return _process_env(adapter_input, adapter_input.output_paths())


def _configure_isolated_codex_home(adapter_input: AdapterInput, env: dict[str, str]) -> None:
    isolate_value = env.get("LOOPPLANE_ISOLATE_CODEX_STATE", "1").strip().lower()
    if (
        adapter_input.adapter != "codex_cli"
        or isolate_value in {"0", "false", "no", "off"}
        or adapter_input.env.get("CODEX_HOME")
    ):
        return
    state_root = env.get("LOOPPLANE_AGENT_STATE_ROOT") or env.get("TMPDIR") or "/tmp"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    codex_home = (
        Path(state_root)
        / f"loopplane-agent-state-{uid}"
        / adapter_input.workflow_id
        / adapter_input.runner_id
    )
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        codex_home.chmod(0o700)
    except OSError:
        pass

    auth_source_value = env.get("LOOPPLANE_CODEX_AUTH_FILE")
    inherited_codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    auth_source = Path(auth_source_value).expanduser() if auth_source_value else inherited_codex_home / "auth.json"
    auth_target = codex_home / "auth.json"
    if auth_source.is_file() and not auth_target.exists():
        try:
            auth_target.symlink_to(auth_source.resolve())
        except OSError:
            pass

    env["CODEX_HOME"] = codex_home.as_posix()
    env["LOOPPLANE_CODEX_STATE_ISOLATED"] = "1"



def _project_dotenv_env(adapter_input: AdapterInput, base_env: Mapping[str, str]) -> dict[str, str]:
    """Load project-local .env values into the child process only.

    The values returned here are not serialized into adapter_input.json, runner
    config, or LoopPlane logs by this adapter. Existing process environment
    values take precedence over .env values.
    """
    dotenv = _project_dotenv_path(adapter_input)
    if dotenv is None or not dotenv.is_file():
        return {}
    loaded: dict[str, str] = {}
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for raw_line in lines:
        key_value = _parse_dotenv_assignment(raw_line)
        if key_value is None:
            continue
        key, value = key_value
        if key in base_env or key in loaded:
            continue
        loaded[key] = value
    return loaded


def _project_dotenv_path(adapter_input: AdapterInput) -> Path | None:
    cwd = adapter_input.cwd.strip()
    if not cwd or "{{" in cwd:
        return None
    try:
        project = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    return project / ".env"


def _parse_dotenv_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def _write_adapter_child_pid_file(path_value: str | None, pid: int) -> None:
    if not path_value:
        return
    try:
        path = Path(path_value).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        return


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


def _policy_decision_dict(decision: CommandPolicyDecision) -> dict[str, Any]:
    data = asdict(decision)
    data["command"] = list(decision.command)
    data["matched_command"] = list(decision.matched_command)
    return data


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _read_output_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _excerpt(value: str) -> str:
    if len(value) <= DOCTOR_OUTPUT_EXCERPT_LIMIT:
        return value
    return value[:DOCTOR_OUTPUT_EXCERPT_LIMIT] + "...[truncated]"


def _has_text(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
