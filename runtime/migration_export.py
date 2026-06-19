from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime.exit_codes import EXIT_GENERIC_FAILURE, EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.path_resolution import WorkflowPathError, load_workflow_config
from runtime.workspace_identity import workspace_boundary_root


EXPORT_SCHEMA_VERSION = "loopplane-migration-export-1"
EXPORT_MANIFEST_NAME = "loopplane_export_manifest.json"
SUPPORTED_EXPORT_PROFILES = frozenset({"source", "stateful", "archive"})
HISTORY_EXPORT_PROFILES = frozenset({"stateful", "archive"})
SOURCE_RUNTIME_FILES = frozenset(
    {
        "git_checkpoints.jsonl",
        "evidence_manifest.json",
        "final_verification_report.json",
    }
)
STATEFUL_RUNTIME_FILES = SOURCE_RUNTIME_FILES | frozenset({"failure_registry.json", "expansion_registry.json"})
STATEFUL_RUNTIME_DIRECTORIES = frozenset({"events", "snapshots"})
SOURCE_RESULT_FILES = frozenset({"report.md", "validation.json", "node_summary.json", "latest.json"})
PROCESS_STATE_FILENAMES = frozenset(
    {
        "background_jobs.json",
        "dashboard_server.json",
        "dashboard_token",
        "supervisor.json",
    }
)
PROCESS_STATE_RUNTIME_DIRECTORIES = frozenset(
    {
        "active_run_leases",
        "lock",
        "locks",
        "runs",
        "supervisor",
    }
)
SECRET_FILENAME_PREFIXES = (".env",)
SECRET_FILENAME_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", "~")
SECRET_KEY_FRAGMENTS = ("api_key", "apikey", "secret", "password", "credential", "private_key", "token")
COMMAND_SECRET_FLAGS = frozenset(
    {
        "--api-key",
        "--api_key",
        "--credential",
        "--key",
        "--password",
        "--private-key",
        "--private_key",
        "--secret",
        "--token",
    }
)
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


@dataclass(frozen=True)
class ExportEntry:
    path: str
    data: bytes
    source: str
    category: str

    def manifest_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "category": self.category,
            "source": self.source,
            "size": len(self.data),
            "sha256": sha256(self.data).hexdigest(),
        }


def export_project(project_root: Path, *, profile: str, output: Path) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    requested_profile = str(profile or "").strip()
    created_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    if requested_profile not in SUPPORTED_EXPORT_PROFILES:
        return _failure(
            project,
            requested_profile,
            output,
            created_at,
            "unsupported_profile",
            [
                "Unsupported export profile "
                f"{requested_profile!r}. Implemented profiles: {', '.join(sorted(SUPPORTED_EXPORT_PROFILES))}."
            ],
        )
    if not (project / ".loopplane").is_dir():
        return _failure(
            project,
            requested_profile,
            output,
            created_at,
            "project_not_initialized",
            [f"{project}: project-local .loopplane instance is missing."],
        )

    try:
        workflow_config = load_workflow_config(project)
        workspace = _load_json_object(project / ".loopplane" / "workspace.json")
        boundary = workspace_boundary_root(project, workspace)
    except (OSError, json.JSONDecodeError, ValueError, WorkflowPathError) as error:
        return _failure(
            project,
            requested_profile,
            output,
            created_at,
            "invalid_project_metadata",
            [f"Unable to resolve project-local workflow metadata: {error}"],
        )

    output_path = output.expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    entries, excluded = _collect_export_entries(
        project,
        boundary,
        workflow_config=workflow_config,
        profile=requested_profile,
        output_path=output_path,
        warnings=warnings,
    )
    manifest = _build_manifest(
        profile=requested_profile,
        created_at=created_at,
        workspace=workspace,
        workflow_config=workflow_config,
        boundary=boundary,
        entries=entries,
        excluded=excluded,
    )
    compression = _write_archive(output_path, entries, manifest, warnings=warnings)

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "ok": True,
        "status": "exported",
        "profile": requested_profile,
        "project_root": project.as_posix(),
        "output": output_path.as_posix(),
        "archive": {
            "path": output_path.as_posix(),
            "format": "tar",
            **compression,
        },
        "manifest": manifest,
        "included_count": len(entries),
        "excluded_count": len(excluded),
        "warnings": warnings,
        "errors": errors,
    }


def format_export_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane export: {result.get('status', 'unknown')}",
        f"profile: {result.get('profile') or 'unknown'}",
        f"project: {result.get('project_root') or 'unknown'}",
    ]
    archive = result.get("archive")
    if isinstance(archive, Mapping):
        lines.append(f"output: {archive.get('path') or result.get('output') or 'unknown'}")
        lines.append(f"archive_format: {archive.get('format') or 'unknown'}")
        lines.append(f"compression: {archive.get('compression') or 'unknown'}")
        if archive.get("fallback"):
            lines.append(f"compression_fallback: {archive.get('fallback_reason') or 'unknown'}")
    lines.append(f"included_files: {result.get('included_count', 0)}")
    lines.append(f"excluded_files: {result.get('excluded_count', 0)}")
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def export_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if result.get("status") in {"unsupported_profile", "project_not_initialized", "invalid_project_metadata"}:
        return EXIT_INVALID_CONFIG
    return EXIT_GENERIC_FAILURE


def list_export_archive_members(path: Path) -> list[str]:
    with _open_archive_for_read(path) as archive:
        return sorted(member.name for member in archive.getmembers())


def read_export_archive_manifest(path: Path) -> dict[str, Any]:
    with _open_archive_for_read(path) as archive:
        extracted = archive.extractfile(EXPORT_MANIFEST_NAME)
        if extracted is None:
            raise KeyError(f"{EXPORT_MANIFEST_NAME} is missing from export archive")
        data = json.loads(extracted.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{EXPORT_MANIFEST_NAME} must contain a JSON object")
    return data


def _collect_export_entries(
    project: Path,
    boundary: Path,
    *,
    workflow_config: Mapping[str, Any],
    profile: str,
    output_path: Path,
    warnings: list[str],
) -> tuple[list[ExportEntry], list[dict[str, Any]]]:
    entries: list[ExportEntry] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    resolved_output = output_path.resolve()

    for path in sorted(boundary.rglob("*"), key=lambda item: _project_relative(project, item)):
        if path.is_dir():
            continue
        relative = _project_relative(project, path)
        reason = _export_exclusion_reason(profile, relative, path, resolved_output)
        if reason is not None:
            excluded.append({"path": relative, "reason": reason})
            continue
        if relative in seen:
            continue
        seen.add(relative)
        data, source = _export_file_bytes(profile, project, relative, path, warnings)
        entries.append(ExportEntry(relative, data, source, _export_category(profile, relative)))

    entries.extend(
        _synthetic_active_projection_entries(
            project,
            workflow_config=workflow_config,
            seen=seen,
            profile=profile,
            warnings=warnings,
        )
    )
    return sorted(entries, key=lambda entry: entry.path), sorted(excluded, key=lambda item: str(item["path"]))


def _synthetic_active_projection_entries(
    project: Path,
    *,
    workflow_config: Mapping[str, Any],
    seen: set[str],
    profile: str,
    warnings: list[str],
) -> list[ExportEntry]:
    entries: list[ExportEntry] = []
    for field, target in (("plan_file", "PLAN.md"),):
        if target in seen:
            continue
        value = workflow_config.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        source_path = (project / value).resolve()
        try:
            source_path.relative_to(project.resolve())
        except ValueError:
            warnings.append(f"{target}: active projection source {value!r} is outside the project and was not exported.")
            continue
        if not source_path.is_file():
            continue
        try:
            data = source_path.read_bytes()
        except OSError as error:
            warnings.append(f"{target}: unable to export active projection from {value}: {error}")
            continue
        entries.append(ExportEntry(target, data, "active_workflow_projection", _export_category(profile, target)))
        seen.add(target)
    return entries


def _export_exclusion_reason(profile: str, relative: str, path: Path, output_path: Path) -> str | None:
    if profile in HISTORY_EXPORT_PROFILES:
        return _stateful_exclusion_reason(relative, path, output_path)
    return _source_exclusion_reason(relative, path, output_path)


def _source_exclusion_reason(relative: str, path: Path, output_path: Path) -> str | None:
    base_reason = _base_exclusion_reason(relative, path, output_path)
    if base_reason is not None:
        return base_reason
    parts = PurePosixPath(relative).parts
    name = parts[-1] if parts else ""
    if _is_agent_skill_projection(parts):
        return "agent_skill_projection"
    if parts[0] == ".loopplane" and "results" in parts and name not in SOURCE_RESULT_FILES:
        return "non_portable_result_artifact"
    runtime_index = _first_part_index(parts, "runtime")
    if parts[0] == ".loopplane" and runtime_index is not None:
        runtime_tail = parts[runtime_index + 1 :]
        if runtime_tail and runtime_tail[0] == "events":
            return None
        if len(runtime_tail) == 1 and runtime_tail[0] in SOURCE_RUNTIME_FILES:
            return None
        return "runtime_process_state"
    return None


def _stateful_exclusion_reason(relative: str, path: Path, output_path: Path) -> str | None:
    base_reason = _base_exclusion_reason(relative, path, output_path)
    if base_reason is not None:
        return base_reason
    parts = PurePosixPath(relative).parts
    name = parts[-1] if parts else ""
    if parts[0] == ".loopplane" and "results" in parts:
        return None
    runtime_index = _first_part_index(parts, "runtime")
    if parts[0] == ".loopplane" and runtime_index is not None:
        runtime_tail = parts[runtime_index + 1 :]
        if not runtime_tail:
            return "runtime_process_state"
        if runtime_tail[0] in PROCESS_STATE_RUNTIME_DIRECTORIES:
            return "runtime_process_state"
        if runtime_tail[0] in STATEFUL_RUNTIME_DIRECTORIES:
            return None
        if len(runtime_tail) == 1 and runtime_tail[0] in STATEFUL_RUNTIME_FILES:
            return None
        if name in PROCESS_STATE_FILENAMES or "pid" in name.lower() or "process" in name.lower():
            return "runtime_process_state"
        return "runtime_process_state"
    return None


def _is_agent_skill_projection(parts: Sequence[str]) -> bool:
    return len(parts) >= 3 and parts[0] in {".codex", ".claude"} and parts[1] == "skills"


def _base_exclusion_reason(relative: str, path: Path, output_path: Path) -> str | None:
    parts = PurePosixPath(relative).parts
    name = parts[-1] if parts else ""
    if not parts:
        return "empty_path"
    if parts[0] == ".loopplane_home" or _path_is_under_loopplane_home(path):
        return "loopplane_home_files"
    try:
        if path.resolve() == output_path:
            return "requested_output_archive"
    except OSError:
        pass
    if path.is_symlink():
        return "symlink_not_portable"
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in parts):
        return "tool_or_vcs_directory"
    if name.startswith(SECRET_FILENAME_PREFIXES) or name.endswith(SECRET_FILENAME_SUFFIXES):
        return "machine_local_secret_file"
    if name.endswith(EXCLUDED_SUFFIXES):
        return "temporary_or_compiled_file"
    config_index = _first_part_index(parts, "config")
    if config_index is not None and len(parts) > config_index + 1 and parts[config_index + 1] == "local":
        return "machine_local_config"
    if parts[:2] == (".loopplane", "dashboard_static"):
        return "derived_dashboard_output"
    if parts[0] == ".loopplane" and "read_models" in parts:
        return "derived_read_model"
    return None


def _path_is_under_loopplane_home(path: Path) -> bool:
    value = os.environ.get("LOOPPLANE_HOME")
    if not value:
        return False
    try:
        resolved_path = path.resolve()
        home = Path(value).expanduser().resolve()
    except OSError:
        return False
    return resolved_path == home or home in resolved_path.parents


def _export_file_bytes(
    profile: str,
    project: Path,
    relative: str,
    path: Path,
    warnings: list[str],
) -> tuple[bytes, str]:
    parts = PurePosixPath(relative).parts
    if parts and parts[-1] == "agent_runners.json" and "config" in parts:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            warnings.append(f"{relative}: unable to sanitize agent runner JSON; exporting raw file: {error}")
            return path.read_bytes(), "filesystem"
        sanitized = _sanitize_agent_runners_config(data, profile=profile)
        return _json_bytes(sanitized), "sanitized_config"
    if _should_sanitize_migration_metadata(profile, relative) and path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            warnings.append(f"{relative}: unable to sanitize {profile} JSON metadata; exporting raw file: {error}")
            return path.read_bytes(), "filesystem"
        sanitized = _sanitize_stateful_metadata(data, project=project, profile=profile)
        return _json_bytes(sanitized), f"sanitized_{profile}_metadata"
    if _should_sanitize_migration_metadata(profile, relative) and path.suffix == ".jsonl":
        try:
            return (
                _sanitize_stateful_jsonl(path.read_text(encoding="utf-8"), project=project, profile=profile),
                f"sanitized_{profile}_metadata",
            )
        except OSError as error:
            warnings.append(f"{relative}: unable to sanitize {profile} JSONL metadata; exporting raw file: {error}")
            return path.read_bytes(), "filesystem"
    if _should_sanitize_migration_metadata(profile, relative) and parts and parts[-1] == "commands.sh":
        try:
            return _sanitize_stateful_commands_text(path.read_text(encoding="utf-8")), f"sanitized_{profile}_metadata"
        except OSError as error:
            warnings.append(f"{relative}: unable to sanitize command evidence; exporting raw file: {error}")
            return path.read_bytes(), "filesystem"
    return path.read_bytes(), "filesystem"


def _should_sanitize_migration_metadata(profile: str, relative: str) -> bool:
    if profile in HISTORY_EXPORT_PROFILES:
        return True
    parts = PurePosixPath(relative).parts
    return profile == "source" and bool(parts) and parts[0] == ".loopplane"


def _sanitize_agent_runners_config(data: Any, *, profile: str) -> Any:
    if not isinstance(data, Mapping):
        return data
    sanitized = dict(data)
    runners = sanitized.get("runners")
    if not isinstance(runners, Mapping):
        return _redact_secret_values(sanitized, placeholder=f"<redacted-for-{profile}-migration>")
    sanitized_runners: dict[str, Any] = {}
    for runner_id, runner in runners.items():
        if not isinstance(runner, Mapping):
            sanitized_runners[str(runner_id)] = runner
            continue
        runner_data = _redact_secret_values(dict(runner), placeholder=f"<redacted-for-{profile}-migration>")
        command = runner_data.get("command")
        if isinstance(command, str):
            runner_data["command"] = _portable_command(command)
        cwd = runner_data.get("cwd")
        if isinstance(cwd, str) and _is_absolute_or_local_machine_path(cwd):
            runner_data["cwd"] = "{{project_root}}"
        if "env" in runner_data:
            runner_data["env"] = {}
        doctor = runner_data.get("doctor")
        if isinstance(doctor, Mapping):
            doctor_data = _redact_secret_values(dict(doctor), placeholder=f"<redacted-for-{profile}-migration>")
            for field in ("check_command", "auth_check_command", "check_auth_command"):
                value = doctor_data.get(field)
                if isinstance(value, str) and value.strip():
                    doctor_data[field] = _portable_command(value)
            runner_data["doctor"] = doctor_data
        sanitized_runners[str(runner_id)] = runner_data
    sanitized["runners"] = sanitized_runners
    notes = list(sanitized.get("migration_notes") or []) if isinstance(sanitized.get("migration_notes"), list) else []
    note = f"{profile} export removed machine-local runner environment overrides and sanitized absolute command paths"
    if note not in notes:
        notes.append(note)
    sanitized["migration_notes"] = notes
    return _redact_secret_values(sanitized, placeholder=f"<redacted-for-{profile}-migration>")


def _redact_secret_values(
    value: Any,
    *,
    key: str = "",
    placeholder: str = "<redacted-for-source-migration>",
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_secret_values(child_value, key=str(child_key), placeholder=placeholder)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_values(item, key=key, placeholder=placeholder) for item in value]
    normalized_key = key.lower().replace("-", "_")
    if isinstance(value, str) and any(fragment in normalized_key for fragment in SECRET_KEY_FRAGMENTS):
        return placeholder
    return value


STALE_PROCESS_KEYS = frozenset(
    {
        "pid",
        "pids",
        "adapter_pid",
        "background",
        "background_jobs",
        "background_job_records",
        "background_pids",
        "background_commands",
        "background_logs",
        "background_registry_update",
        "background_registry_path",
        "process_handle",
        "process_handles",
        "runner_resource_lock",
        "runner_resource_locks",
        "lock_path",
        "lock_paths",
        "supervisor_pid",
        "wake_next_agent_when",
    }
)
COMMAND_KEYS = frozenset({"command", "cmd", "check_command", "auth_check_command", "check_auth_command"})
PATH_KEY_FRAGMENTS = ("path", "dir", "file", "log", "output", "cwd")


def _sanitize_stateful_metadata(value: Any, *, project: Path, key: str = "", profile: str = "stateful") -> Any:
    redaction = f"<redacted-for-{profile}-migration>"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            text_key = str(child_key)
            normalized_key = text_key.lower().replace("-", "_")
            if _is_stale_process_key(normalized_key):
                continue
            if (
                isinstance(child_value, str)
                and (any(fragment in normalized_key for fragment in SECRET_KEY_FRAGMENTS) or "token" in normalized_key)
            ):
                sanitized[text_key] = redaction
                continue
            if normalized_key in COMMAND_KEYS and isinstance(child_value, str):
                sanitized[text_key] = _portable_command(child_value)
                continue
            if normalized_key == "commands" and isinstance(child_value, Sequence) and not isinstance(child_value, (str, bytes)):
                sanitized[text_key] = [
                    _portable_command(item)
                    if isinstance(item, str)
                    else _sanitize_stateful_metadata(item, project=project, key=text_key, profile=profile)
                    for item in child_value
                ]
                continue
            if isinstance(child_value, str) and _is_path_metadata_key(normalized_key):
                sanitized[text_key] = _portable_path_value(project, child_value)
                continue
            sanitized[text_key] = _sanitize_stateful_metadata(child_value, project=project, key=text_key, profile=profile)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_stateful_metadata(item, project=project, key=key, profile=profile) for item in value]
    normalized_key = key.lower().replace("-", "_")
    if isinstance(value, str) and any(fragment in normalized_key for fragment in SECRET_KEY_FRAGMENTS):
        return redaction
    return value


def _sanitize_stateful_jsonl(text: str, *, project: Path, profile: str = "stateful") -> bytes:
    lines: list[str] = []
    previous_event_id: str | None = None
    previous_event_hash: str | None = None
    for line in text.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        sanitized = _sanitize_stateful_metadata(data, project=project, profile=profile)
        if _looks_like_event_record(sanitized):
            event = dict(sanitized)
            if previous_event_id is not None:
                event["prev_event_id"] = previous_event_id
                event["prev_event_hash"] = previous_event_hash
            event["event_hash"] = _event_record_hash(event)
            previous_event_id = str(event.get("event_id") or "")
            previous_event_hash = str(event.get("event_hash") or "")
            sanitized = event
        lines.append(json.dumps(sanitized, sort_keys=True))
    return ("\n".join(lines) + ("\n" if text.endswith("\n") or lines else "")).encode("utf-8")


def _sanitize_stateful_commands_text(text: str) -> bytes:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines.append(f"{indent}{_portable_command(stripped)}")
    return ("\n".join(lines) + ("\n" if text.endswith("\n") or lines else "")).encode("utf-8")


def _looks_like_event_record(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return "event_id" in value and ("sequence" in value or "seq" in value or "event_hash" in value)


def _event_record_hash(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("event_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _is_stale_process_key(normalized_key: str) -> bool:
    if normalized_key in STALE_PROCESS_KEYS:
        return True
    return normalized_key.endswith("_pid") or normalized_key.endswith("_pids")


def _is_path_metadata_key(normalized_key: str) -> bool:
    return any(fragment in normalized_key for fragment in PATH_KEY_FRAGMENTS)


def _portable_path_value(project: Path, value: str) -> str:
    text = value.strip()
    if not _is_absolute_or_local_machine_path(text):
        return value
    if text.startswith("./"):
        return text[2:] or "."
    if text.startswith("../"):
        return "<redacted-local-path>"
    try:
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            relative = candidate.resolve(strict=False).relative_to(project)
            return relative.as_posix()
    except (OSError, ValueError):
        pass
    return "<redacted-local-path>"


def _portable_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return "reconfigure-after-migration"
    executable = parts[0]
    if _is_absolute_or_local_machine_path(executable):
        parts[0] = Path(executable).name or "reconfigure-after-migration"
    for index, part in enumerate(parts[1:], start=1):
        if _is_absolute_or_local_machine_path(part):
            parts[index] = "<redacted-local-path>"
    parts = _redact_command_secret_args(parts)
    return " ".join(shlex.quote(part) for part in parts)


def _redact_command_secret_args(parts: list[str]) -> list[str]:
    redacted = list(parts)
    redact_next = False
    for index, part in enumerate(redacted):
        normalized = part.lower().replace("_", "-")
        if redact_next:
            redacted[index] = "<redacted-local-secret>"
            redact_next = False
            continue
        if normalized in COMMAND_SECRET_FLAGS:
            redact_next = True
            continue
        flag, separator, _value = normalized.partition("=")
        if separator and flag in COMMAND_SECRET_FLAGS:
            original_flag = part.partition("=")[0]
            redacted[index] = f"{original_flag}=<redacted-local-secret>"
            continue
        if index > 0 and any(fragment in normalized for fragment in ("secret", "password", "token")):
            redacted[index] = "<redacted-local-secret>"
    return redacted


def _is_absolute_or_local_machine_path(value: str) -> bool:
    text = value.strip().replace("\\", "/")
    if not text:
        return False
    return text.startswith("/") or text.startswith("~") or text.startswith("../") or text.startswith("./")


def _build_manifest(
    *,
    profile: str,
    created_at: str,
    workspace: Mapping[str, Any],
    workflow_config: Mapping[str, Any],
    boundary: Path,
    entries: Sequence[ExportEntry],
    excluded: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    workflow_id = workflow_config.get("workflow_id")
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "profile": profile,
        "created_at": created_at,
        "project_root": ".",
        "workspace_boundary": workspace.get("workspace_boundary", "project_root"),
        "resolved_workspace_boundary": _manifest_boundary(boundary),
        "workspace_id": workspace.get("workspace_id"),
        "current_workflow_id": workflow_id,
        "archive_format": "tar",
        "files": [entry.manifest_record() for entry in entries],
        "excluded_paths": [dict(item) for item in excluded],
    }
    if profile == "stateful":
        manifest["stateful_profile"] = {
            "preserves": [
                "project brief and plan",
                "workflow registry and current pointer",
                "sanitized workflow config",
                "planning records",
                "request records",
                "historical result and run evidence",
                "runtime events and snapshots",
                "failure registry",
                "git checkpoint metadata",
                "evidence manifest and final verification report",
            ],
            "excludes": [
                "locks",
                "active run leases",
                "PIDs and stale process handles",
                "dashboard tokens",
                "dashboard server state",
                "derived read models",
                "machine-local runner overrides",
                "machine-local runner secrets",
                "unsanitized absolute local command paths",
                "LOOPPLANE_HOME files",
                "sibling or out-of-boundary files",
            ],
        }
    elif profile == "archive":
        manifest["migration_intent"] = {
            "mode": "read_only_archive",
            "import_requires_read_only": True,
            "workflow_status_on_import": "read_only_imported",
            "resume_allowed_after_import": False,
            "resume_escape_paths": ["restore", "fork"],
        }
        manifest["archive_profile"] = {
            "intent": "view workflow history elsewhere without resuming it",
            "intended_import_mode": "read_only",
            "intended_dashboard_mode": "read_only",
            "preserves": [
                "project brief and plan",
                "workflow registry and current pointer",
                "sanitized workflow config",
                "planning records",
                "request records",
                "historical result and run evidence",
                "runtime events and snapshots",
                "failure registry",
                "git checkpoint metadata",
                "evidence manifest and final verification report",
            ],
            "excludes": [
                "locks",
                "active run leases",
                "PIDs and stale process handles",
                "dashboard tokens",
                "dashboard server state",
                "derived read models",
                "machine-local runner overrides",
                "machine-local runner secrets",
                "unsanitized absolute local command paths",
                "LOOPPLANE_HOME files",
                "sibling or out-of-boundary files",
            ],
        }
    else:
        manifest["source_profile"] = {
            "preserves": [
                "project files inside the workspace boundary",
                "workflow registry and current pointer",
                "plans and project brief files",
                "source migration evidence files",
                "runtime events",
                "git checkpoint metadata",
            ],
            "excludes": [
                "locks",
                "active run leases",
                "PIDs and process state",
                "dashboard tokens",
                "dashboard server state",
                "derived read models",
                "machine-local runner overrides",
                "machine-local runner secrets",
                "project-local Codex/Claude skill projections",
                "LOOPPLANE_HOME files",
                "sibling or out-of-boundary files",
            ],
        }
    return manifest


def _write_archive(
    output: Path,
    entries: Sequence[ExportEntry],
    manifest: Mapping[str, Any],
    *,
    warnings: list[str],
) -> dict[str, Any]:
    requested = _requested_compression(output)
    if requested == "zstd":
        zstd = shutil.which("zstd")
        if zstd:
            with tempfile.NamedTemporaryFile(prefix=".loopplane-export-", suffix=".tar", dir=output.parent, delete=False) as temp:
                temp_tar = Path(temp.name)
            try:
                _write_tar(temp_tar, entries, manifest, mode="w")
                completed = subprocess.run(
                    [zstd, "-q", "-f", "-o", str(output), str(temp_tar)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if completed.returncode == 0:
                    return {
                        "requested_compression": requested,
                        "compression": "zstd",
                        "fallback": False,
                        "zstd_path": zstd,
                    }
                warnings.append(f"zstd compression failed; wrote uncompressed tar fallback: {completed.stderr.strip()}")
                _write_tar(output, entries, manifest, mode="w")
                return _compression_fallback(requested, "zstd_command_failed")
            finally:
                temp_tar.unlink(missing_ok=True)
        warnings.append("zstd executable is unavailable; wrote an uncompressed tar archive to the requested output path.")
        _write_tar(output, entries, manifest, mode="w")
        return _compression_fallback(requested, "zstd_unavailable")
    if requested == "gzip":
        _write_tar(output, entries, manifest, mode="w:gz")
    elif requested == "xz":
        _write_tar(output, entries, manifest, mode="w:xz")
    elif requested == "bzip2":
        _write_tar(output, entries, manifest, mode="w:bz2")
    else:
        _write_tar(output, entries, manifest, mode="w")
    return {
        "requested_compression": requested,
        "compression": requested,
        "fallback": False,
    }


def _write_tar(output: Path, entries: Sequence[ExportEntry], manifest: Mapping[str, Any], *, mode: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = _json_bytes(manifest)
    with tarfile.open(output, mode) as archive:
        _add_bytes(archive, EXPORT_MANIFEST_NAME, manifest_bytes)
        for entry in entries:
            _add_bytes(archive, entry.path, entry.data)


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def _open_archive_for_read(path: Path) -> tarfile.TarFile:
    archive = path.expanduser().resolve()
    with archive.open("rb") as handle:
        magic = handle.read(4)
    if magic == ZSTD_MAGIC:
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError(f"{archive}: zstd-compressed archive requires the zstd executable for inspection")
        completed = subprocess.run(
            [zstd, "-q", "-d", "-c", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"{archive}: unable to decompress zstd archive: {completed.stderr.decode('utf-8', 'ignore')}")
        return tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:")
    return tarfile.open(archive, "r:*")


def _requested_compression(output: Path) -> str:
    name = output.name.lower()
    if name.endswith((".tar.zst", ".tzst")):
        return "zstd"
    if name.endswith((".tar.gz", ".tgz")):
        return "gzip"
    if name.endswith(".tar.xz"):
        return "xz"
    if name.endswith(".tar.bz2"):
        return "bzip2"
    return "tar"


def _compression_fallback(requested: str, reason: str) -> dict[str, Any]:
    return {
        "requested_compression": requested,
        "compression": "tar",
        "fallback": True,
        "fallback_reason": reason,
    }


def _export_category(profile: str, relative: str) -> str:
    parts = PurePosixPath(relative).parts
    name = parts[-1] if parts else relative
    if relative in {"PROJECT_BRIEF.md", "PLAN.md"}:
        return "root_project_file"
    if relative in {".loopplane/workspace.json", ".loopplane/workflow_registry.json", ".loopplane/current_workflow.json"}:
        return "workspace_metadata"
    if "config" in parts:
        return "sanitized_config"
    if "planning" in parts:
        return "planning"
    if "requests" in parts:
        return "requests"
    if "results" in parts or name in {"evidence_manifest.json", "final_verification_report.json"}:
        return "evidence"
    if profile in HISTORY_EXPORT_PROFILES and "runtime" in parts and "snapshots" in parts:
        return "runtime_snapshots"
    if profile in HISTORY_EXPORT_PROFILES and name == "failure_registry.json":
        return "failure_registry"
    if "runtime" in parts and "events" in parts:
        return "runtime_events"
    if name == "git_checkpoints.jsonl":
        return "git_checkpoint_metadata"
    return "project_source"


def _manifest_boundary(boundary: Path) -> str:
    name = boundary.name
    return "." if not name else "."


def _first_part_index(parts: Sequence[str], needle: str) -> int | None:
    for index, part in enumerate(parts):
        if part == needle:
            return index
    return None


def _project_relative(project: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else project / path
    return candidate.absolute().relative_to(project).as_posix()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected JSON object")
    return data


def _json_bytes(data: Mapping[str, Any] | Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _failure(
    project: Path,
    profile: str,
    output: Path,
    created_at: str,
    status: str,
    errors: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "profile": profile,
        "project_root": project.as_posix(),
        "output": output.expanduser().as_posix(),
        "archive": None,
        "manifest": None,
        "included_count": 0,
        "excluded_count": 0,
        "created_at": created_at,
        "warnings": [],
        "errors": list(errors),
    }
