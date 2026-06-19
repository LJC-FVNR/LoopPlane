from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from runtime.agent_runners import SCHEMA_VERSION, RunnerConfig


ADAPTER_INPUT_FILENAME = "adapter_input.json"
ADAPTER_RESULT_FILENAME = "adapter_result.json"
DEFAULT_STDOUT_FILENAME = "stdout.log"
DEFAULT_STDERR_FILENAME = "stderr.log"
DEFAULT_FINAL_OUTPUT_FILENAME = "final.md"
DOCTOR_STATUS_OK = "ok"
DOCTOR_STATUS_WAITING_CONFIG = "waiting_config"


class AdapterContractError(ValueError):
    def __init__(self, message: str, *, errors: Sequence[str] | None = None) -> None:
        self.errors = tuple(errors or [message])
        super().__init__(message)


@dataclass(frozen=True)
class AdapterOutputPaths:
    stdout_path: Path
    stderr_path: Path
    final_output_path: Path
    adapter_result_path: Path

    @classmethod
    def for_scheduler_run_dir(cls, scheduler_run_dir: Path | str) -> "AdapterOutputPaths":
        run_dir = Path(scheduler_run_dir)
        return cls(
            stdout_path=run_dir / DEFAULT_STDOUT_FILENAME,
            stderr_path=run_dir / DEFAULT_STDERR_FILENAME,
            final_output_path=run_dir / DEFAULT_FINAL_OUTPUT_FILENAME,
            adapter_result_path=run_dir / ADAPTER_RESULT_FILENAME,
        )


@dataclass(frozen=True)
class AdapterInput:
    schema_version: str
    run_id: str
    workflow_id: str
    runner_id: str
    role: str
    task_id: str | None
    prompt_path: Path
    prompt_content: str
    scheduler_run_dir: Path
    role_output_dir: Path
    task_evidence_run_dir: Path | None
    cwd: str
    adapter: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    timeout_seconds: int
    permission_policy: Mapping[str, Any]
    prompt_delivery: Mapping[str, Any]
    runner_config: Mapping[str, Any]

    @classmethod
    def from_runner_config(
        cls,
        *,
        run_id: str,
        workflow_id: str,
        runner_config: RunnerConfig,
        prompt_path: Path | str,
        scheduler_run_dir: Path | str,
        role_output_dir: Path | str,
        task_id: str | None = None,
        task_evidence_run_dir: Path | str | None = None,
        prompt_content: str | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: int | None = None,
        role: str | None = None,
    ) -> "AdapterInput":
        prompt = Path(prompt_path)
        content = prompt.read_text(encoding="utf-8") if prompt_content is None else prompt_content
        selected_role = role or runner_config.role
        merged_env = dict(runner_config.env)
        adapter_options = runner_config.adapter_options
        model = _adapter_option_text(adapter_options, ("model", "codex_model", "claude_model"))
        reasoning_effort = _adapter_option_text(
            adapter_options,
            ("reasoning_effort", "model_reasoning_effort", "codex_reasoning_effort", "effort", "thinking_effort"),
        )
        if model:
            merged_env.setdefault("LOOPPLANE_AGENT_MODEL", model)
        if reasoning_effort:
            merged_env.setdefault("LOOPPLANE_AGENT_REASONING_EFFORT", reasoning_effort)
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        merged_env["LOOPPLANE_ROLE"] = selected_role
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            workflow_id=workflow_id,
            runner_id=runner_config.runner_id,
            role=selected_role,
            task_id=task_id,
            prompt_path=prompt,
            prompt_content=content,
            scheduler_run_dir=Path(scheduler_run_dir),
            role_output_dir=Path(role_output_dir),
            task_evidence_run_dir=Path(task_evidence_run_dir) if task_evidence_run_dir is not None else None,
            cwd=cwd if cwd is not None else runner_config.cwd,
            adapter=runner_config.adapter,
            command=runner_config.command,
            args=tuple(runner_config.args),
            env=MappingProxyType(merged_env),
            timeout_seconds=timeout_seconds if timeout_seconds is not None else runner_config.timeout_seconds,
            permission_policy=_freeze_mapping(runner_config.permission_policy),
            prompt_delivery=_freeze_mapping(runner_config.prompt_delivery),
            runner_config=_freeze_mapping(runner_config.as_dict()),
        ).validate()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterInput":
        errors = _base_input_errors(data)
        if errors:
            _raise_contract(errors)

        prompt_path = Path(str(data["prompt_path"]))
        prompt_content = data.get("prompt_content")
        if prompt_content is None:
            try:
                prompt_content = prompt_path.read_text(encoding="utf-8")
            except OSError as error:
                _raise_contract([f"prompt_content missing and prompt_path cannot be read: {error}"])

        task_evidence_run_dir = data.get("task_evidence_run_dir")
        return cls(
            schema_version=str(data["schema_version"]),
            run_id=str(data["run_id"]),
            workflow_id=str(data["workflow_id"]),
            runner_id=str(data["runner_id"]),
            role=str(data["role"]),
            task_id=str(data["task_id"]) if data.get("task_id") is not None else None,
            prompt_path=prompt_path,
            prompt_content=str(prompt_content),
            scheduler_run_dir=Path(str(data["scheduler_run_dir"])),
            role_output_dir=Path(str(data["role_output_dir"])),
            task_evidence_run_dir=Path(str(task_evidence_run_dir)) if task_evidence_run_dir is not None else None,
            cwd=str(data["cwd"]),
            adapter=str(data["adapter"]),
            command=str(data["command"]),
            args=tuple(str(value) for value in data["args"]),
            env=MappingProxyType({str(key): str(value) for key, value in data["env"].items()}),
            timeout_seconds=int(data["timeout_seconds"]),
            permission_policy=_freeze_mapping(data["permission_policy"]),
            prompt_delivery=_freeze_mapping(data["prompt_delivery"]),
            runner_config=_freeze_mapping(data["runner_config"]),
        ).validate()

    @classmethod
    def read_json(cls, path: Path | str) -> "AdapterInput":
        data = _read_json_object(Path(path))
        return cls.from_dict(data)

    def validate(self) -> "AdapterInput":
        errors = _base_input_errors(self.to_dict())
        if errors:
            _raise_contract(errors)
        if self.task_id is None and self.task_evidence_run_dir is not None:
            _raise_contract(["task_evidence_run_dir must be null when task_id is null"])
        return self

    def output_paths(self) -> AdapterOutputPaths:
        return AdapterOutputPaths.for_scheduler_run_dir(self.scheduler_run_dir)

    def ensure_run_dirs(self) -> None:
        ensure_run_dirs(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "runner_id": self.runner_id,
            "role": self.role,
            "task_id": self.task_id,
            "prompt_path": _path_to_posix(self.prompt_path),
            "prompt_content": self.prompt_content,
            "scheduler_run_dir": _path_to_posix(self.scheduler_run_dir),
            "role_output_dir": _path_to_posix(self.role_output_dir),
            "task_evidence_run_dir": (
                _path_to_posix(self.task_evidence_run_dir)
                if self.task_evidence_run_dir is not None
                else None
            ),
            "cwd": self.cwd,
            "adapter": self.adapter,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "permission_policy": _thaw(self.permission_policy),
            "prompt_delivery": _thaw(self.prompt_delivery),
            "runner_config": _thaw(self.runner_config),
        }

    def write_json(self, path: Path | str) -> Path:
        return _write_json(Path(path), self.to_dict())


@dataclass(frozen=True)
class AdapterOutput:
    schema_version: str
    run_id: str
    runner_id: str
    role: str
    adapter: str
    command: str
    cwd: str
    started_at: str
    ended_at: str
    exit_code: int
    timed_out: bool
    stdout_path: Path
    stderr_path: Path
    final_output_path: Path
    adapter_result_path: Path
    produced_files: tuple[Path, ...]
    adapter_metadata: Mapping[str, Any]

    @classmethod
    def from_input(
        cls,
        adapter_input: AdapterInput,
        *,
        started_at: str,
        ended_at: str,
        exit_code: int,
        timed_out: bool = False,
        output_paths: AdapterOutputPaths | None = None,
        produced_files: Sequence[Path | str] = (),
        adapter_metadata: Mapping[str, Any] | None = None,
    ) -> "AdapterOutput":
        paths = output_paths or adapter_input.output_paths()
        return cls(
            schema_version=adapter_input.schema_version,
            run_id=adapter_input.run_id,
            runner_id=adapter_input.runner_id,
            role=adapter_input.role,
            adapter=adapter_input.adapter,
            command=adapter_input.command,
            cwd=adapter_input.cwd,
            started_at=started_at,
            ended_at=ended_at,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            final_output_path=paths.final_output_path,
            adapter_result_path=paths.adapter_result_path,
            produced_files=tuple(Path(path) for path in produced_files),
            adapter_metadata=_freeze_mapping(adapter_metadata or {}),
        ).validate()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterOutput":
        errors = _base_output_errors(data)
        if errors:
            _raise_contract(errors)
        return cls(
            schema_version=str(data["schema_version"]),
            run_id=str(data["run_id"]),
            runner_id=str(data["runner_id"]),
            role=str(data["role"]),
            adapter=str(data["adapter"]),
            command=str(data["command"]),
            cwd=str(data["cwd"]),
            started_at=str(data["started_at"]),
            ended_at=str(data["ended_at"]),
            exit_code=int(data["exit_code"]),
            timed_out=bool(data["timed_out"]),
            stdout_path=Path(str(data["stdout_path"])),
            stderr_path=Path(str(data["stderr_path"])),
            final_output_path=Path(str(data["final_output_path"])),
            adapter_result_path=Path(str(data["adapter_result_path"])),
            produced_files=tuple(Path(str(path)) for path in data["produced_files"]),
            adapter_metadata=_freeze_mapping(data["adapter_metadata"]),
        ).validate()

    @classmethod
    def read_json(cls, path: Path | str) -> "AdapterOutput":
        data = _read_json_object(Path(path))
        return cls.from_dict(data)

    def validate(self) -> "AdapterOutput":
        errors = _base_output_errors(self.to_dict())
        if errors:
            _raise_contract(errors)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "runner_id": self.runner_id,
            "role": self.role,
            "adapter": self.adapter,
            "command": self.command,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_path": _path_to_posix(self.stdout_path),
            "stderr_path": _path_to_posix(self.stderr_path),
            "final_output_path": _path_to_posix(self.final_output_path),
            "adapter_result_path": _path_to_posix(self.adapter_result_path),
            "produced_files": [_path_to_posix(path) for path in self.produced_files],
            "adapter_metadata": _thaw(self.adapter_metadata),
        }

    def write_json(self, path: Path | str | None = None) -> Path:
        return _write_json(Path(path) if path is not None else self.adapter_result_path, self.to_dict())


@dataclass(frozen=True)
class AdapterDoctorResult:
    schema_version: str
    runner_id: str
    role: str
    adapter: str
    status: str
    checks: tuple[Mapping[str, Any], ...]
    message: str
    adapter_metadata: Mapping[str, Any]

    @classmethod
    def ok(
        cls,
        adapter_input: AdapterInput,
        *,
        checks: Sequence[Mapping[str, Any]] = (),
        message: str = "",
        adapter_metadata: Mapping[str, Any] | None = None,
    ) -> "AdapterDoctorResult":
        return cls(
            schema_version=adapter_input.schema_version,
            runner_id=adapter_input.runner_id,
            role=adapter_input.role,
            adapter=adapter_input.adapter,
            status=DOCTOR_STATUS_OK,
            checks=tuple(_freeze_mapping(check) for check in checks),
            message=message,
            adapter_metadata=_freeze_mapping(adapter_metadata or {}),
        ).validate()

    @classmethod
    def waiting_config(
        cls,
        adapter_input: AdapterInput,
        *,
        checks: Sequence[Mapping[str, Any]] = (),
        message: str,
        adapter_metadata: Mapping[str, Any] | None = None,
    ) -> "AdapterDoctorResult":
        return cls(
            schema_version=adapter_input.schema_version,
            runner_id=adapter_input.runner_id,
            role=adapter_input.role,
            adapter=adapter_input.adapter,
            status=DOCTOR_STATUS_WAITING_CONFIG,
            checks=tuple(_freeze_mapping(check) for check in checks),
            message=message,
            adapter_metadata=_freeze_mapping(adapter_metadata or {}),
        ).validate()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterDoctorResult":
        errors = _doctor_result_errors(data)
        if errors:
            _raise_contract(errors)
        return cls(
            schema_version=str(data["schema_version"]),
            runner_id=str(data["runner_id"]),
            role=str(data["role"]),
            adapter=str(data["adapter"]),
            status=str(data["status"]),
            checks=tuple(_freeze_mapping(check) for check in data["checks"]),
            message=str(data["message"]),
            adapter_metadata=_freeze_mapping(data["adapter_metadata"]),
        ).validate()

    def validate(self) -> "AdapterDoctorResult":
        errors = _doctor_result_errors(self.to_dict())
        if errors:
            _raise_contract(errors)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runner_id": self.runner_id,
            "role": self.role,
            "adapter": self.adapter,
            "status": self.status,
            "checks": [_thaw(check) for check in self.checks],
            "message": self.message,
            "adapter_metadata": _thaw(self.adapter_metadata),
        }


class AgentAdapter(ABC):
    adapter_name: str

    @abstractmethod
    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        raise NotImplementedError

    def doctor(self, adapter_input: AdapterInput) -> AdapterDoctorResult:
        return AdapterDoctorResult.waiting_config(
            adapter_input,
            message=f"adapter {self.adapter_name!r} has not implemented doctor checks",
            checks=(
                {
                    "name": "adapter_doctor",
                    "status": DOCTOR_STATUS_WAITING_CONFIG,
                    "message": "Doctor checks are not implemented for this adapter.",
                },
            ),
        )


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_run_dirs(adapter_input: AdapterInput) -> None:
    adapter_input.scheduler_run_dir.mkdir(parents=True, exist_ok=True)
    adapter_input.role_output_dir.mkdir(parents=True, exist_ok=True)
    if adapter_input.task_evidence_run_dir is not None:
        adapter_input.task_evidence_run_dir.mkdir(parents=True, exist_ok=True)


def write_adapter_result(adapter_output: AdapterOutput, path: Path | str | None = None) -> Path:
    return adapter_output.write_json(path)


def write_adapter_input(adapter_input: AdapterInput, path: Path | str | None = None) -> Path:
    return adapter_input.write_json(Path(path) if path is not None else adapter_input.scheduler_run_dir / ADAPTER_INPUT_FILENAME)


def snapshot_adapter_files(adapter_input: AdapterInput) -> frozenset[Path]:
    return frozenset(_iter_adapter_files(adapter_input))


def discover_adapter_produced_files(
    adapter_input: AdapterInput,
    *,
    before: Sequence[Path | str] = (),
    explicit: Sequence[Path | str] = (),
) -> tuple[Path, ...]:
    before_keys = {_path_key(path) for path in before}
    produced: dict[str, Path] = {}
    for path in _iter_adapter_files(adapter_input):
        if _path_key(path) not in before_keys:
            produced[_path_key(path)] = path
    for path in explicit:
        item = Path(path)
        produced[_path_key(item)] = item
    return tuple(produced[key] for key in sorted(produced))


def _base_input_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _require_schema(data, errors)
    for field in (
        "run_id",
        "workflow_id",
        "runner_id",
        "role",
        "prompt_path",
        "scheduler_run_dir",
        "role_output_dir",
        "cwd",
        "adapter",
        "command",
    ):
        _require_non_empty_string(data, field, errors)
    if "prompt_content" in data and not isinstance(data["prompt_content"], str):
        errors.append("prompt_content must be a string")
    if data.get("task_id") is not None and not _non_empty_string(data["task_id"]):
        errors.append("task_id must be null or a non-empty string")
    if data.get("task_evidence_run_dir") is not None and not _non_empty_string(data["task_evidence_run_dir"]):
        errors.append("task_evidence_run_dir must be null or a non-empty string")
    _require_string_sequence(data, "args", errors)
    _require_string_mapping(data, "env", errors)
    _require_positive_int(data, "timeout_seconds", errors)
    _require_mapping(data, "permission_policy", errors)
    _require_mapping(data, "prompt_delivery", errors)
    _require_mapping(data, "runner_config", errors)
    return errors


def _base_output_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _require_schema(data, errors)
    for field in (
        "run_id",
        "runner_id",
        "role",
        "adapter",
        "command",
        "cwd",
        "started_at",
        "ended_at",
        "stdout_path",
        "stderr_path",
        "final_output_path",
        "adapter_result_path",
    ):
        _require_non_empty_string(data, field, errors)
    _require_int(data, "exit_code", errors)
    if "timed_out" not in data or not isinstance(data.get("timed_out"), bool):
        errors.append("timed_out must be boolean")
    _require_string_sequence(data, "produced_files", errors)
    _require_mapping(data, "adapter_metadata", errors)
    return errors


def _doctor_result_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _require_schema(data, errors)
    for field in ("runner_id", "role", "adapter", "status", "message"):
        _require_non_empty_string(data, field, errors)
    if data.get("status") not in {DOCTOR_STATUS_OK, DOCTOR_STATUS_WAITING_CONFIG, "warning"}:
        errors.append("status must be ok, warning, or waiting_config")
    checks = data.get("checks")
    if not isinstance(checks, (list, tuple)) or not all(isinstance(check, Mapping) for check in checks):
        errors.append("checks must be a list of objects")
    _require_mapping(data, "adapter_metadata", errors)
    return errors


def _require_schema(data: Mapping[str, Any], errors: list[str]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")


def _require_non_empty_string(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    if not _non_empty_string(data.get(field)):
        errors.append(f"{field} must be a non-empty string")


def _require_positive_int(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        errors.append(f"{field} must be a positive integer")


def _require_int(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{field} must be an integer")


def _require_mapping(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    if not isinstance(data.get(field), Mapping):
        errors.append(f"{field} must be an object")


def _require_string_mapping(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    value = data.get(field)
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return
    for key, item in value.items():
        if not _non_empty_string(key) or not isinstance(item, str):
            errors.append(f"{field} must map strings to strings")
            return


def _require_string_sequence(data: Mapping[str, Any], field: str, errors: list[str]) -> None:
    value = data.get(field)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        errors.append(f"{field} must be a list of strings")


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise AdapterContractError(f"{path}: unable to read JSON: {error}") from error
    except json.JSONDecodeError as error:
        raise AdapterContractError(f"{path}: invalid JSON: {error.msg}") from error
    if not isinstance(data, Mapping):
        raise AdapterContractError(f"{path}: JSON must be an object")
    return data


def _write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _adapter_option_text(options: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = options.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _path_to_posix(path: Path) -> str:
    return path.as_posix()


def _iter_adapter_files(adapter_input: AdapterInput) -> tuple[Path, ...]:
    roots: list[Path] = [adapter_input.scheduler_run_dir, adapter_input.role_output_dir]
    if adapter_input.task_evidence_run_dir is not None:
        roots.append(adapter_input.task_evidence_run_dir)
    files: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        try:
            candidates = root.rglob("*")
        except OSError:
            continue
        for candidate in candidates:
            try:
                if candidate.is_file():
                    files.setdefault(_path_key(candidate), candidate)
            except OSError:
                continue
    return tuple(files[key] for key in sorted(files))


def _path_key(path: Path | str) -> str:
    return Path(path).as_posix()


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _raise_contract(errors: Sequence[str]) -> None:
    raise AdapterContractError("; ".join(errors), errors=errors)
