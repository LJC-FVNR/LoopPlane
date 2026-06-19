from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.exit_codes import (
    EXIT_GENERIC_FAILURE,
    EXIT_INVALID_CONFIG,
    EXIT_SECURITY_POLICY_VIOLATION,
    EXIT_SUCCESS,
)
from runtime.migration_export import EXPORT_MANIFEST_NAME, EXPORT_SCHEMA_VERSION, _open_archive_for_read
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.schema_validation import validate_project_schemas
from runtime.workspace_identity import repository_root_value


IMPORT_SCHEMA_VERSION = "loopplane-migration-import-1"
SUPPORTED_IMPORT_PROFILES = frozenset({"stateful", "archive"})
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
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
TOOL_OR_VCS_DIRECTORIES = frozenset(
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
SECRET_FILENAME_PREFIXES = (".env",)
SECRET_FILENAME_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def import_project_archive(archive_path: Path, *, target: Path, read_only: bool = False) -> dict[str, Any]:
    archive = archive_path.expanduser()
    if not archive.is_absolute():
        archive = (Path.cwd() / archive).resolve()
    target_project = target.expanduser()
    if not target_project.is_absolute():
        target_project = (Path.cwd() / target_project).resolve()
    else:
        target_project = target_project.resolve()

    warnings: list[str] = []
    if not archive.is_file():
        return _failure(
            status="archive_missing",
            archive=archive,
            target=target_project,
            errors=[f"{archive}: migration archive does not exist."],
        )
    target_error = _target_preflight_error(target_project)
    if target_error is not None:
        return _failure(status=target_error[0], archive=archive, target=target_project, errors=[target_error[1]])

    try:
        with _open_archive_for_read(archive) as opened:
            materialized = _materialize_archive(
                opened,
                archive_path=archive,
                target=target_project,
                read_only=read_only,
            )
    except (OSError, tarfile.TarError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return _failure(
            status="archive_open_failed",
            archive=archive,
            target=target_project,
            errors=[f"Unable to inspect migration archive: {error}"],
        )

    if not materialized.get("ok"):
        return {**materialized, "archive": archive.as_posix(), "target": target_project.as_posix()}

    profile = str(materialized["profile"])
    if read_only and profile != "archive":
        return _failure(
            status="read_only_import_requires_archive",
            archive=archive,
            target=target_project,
            profile=profile,
            errors=["--read-only is only valid for archive-profile migration imports."],
            recovery_actions=["Use a stateful export without --read-only for resumable migration."],
        )
    if profile == "archive" and not read_only:
        return _failure(
            status="archive_import_requires_read_only",
            archive=archive,
            target=target_project,
            profile=profile,
            errors=["Archive-profile imports must use --read-only and are not part of this stateful import path."],
            recovery_actions=["Use a stateful export for resumable migration."],
        )
    if profile not in SUPPORTED_IMPORT_PROFILES:
        return _failure(
            status="unsupported_profile",
            archive=archive,
            target=target_project,
            profile=profile,
            errors=[
                f"Unsupported import profile {profile!r}. Supported profile for normal import: stateful."
            ],
        )

    parent = target_project.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_path = Path(tempfile.mkdtemp(prefix=".loopplane-import-", dir=parent))
    moved_staging = False
    try:
        _write_staging_files(staging_path, materialized["files"])
        _localize_workspace_identity(staging_path, final_target=target_project, warnings=warnings)
        if profile == "archive":
            _mark_archive_read_only_import(staging_path, manifest=materialized["manifest"], warnings=warnings)
        workflow_config = load_workflow_config(staging_path)
        paths = WorkflowPaths.from_config(staging_path, workflow_config)
        _regenerate_local_runtime_scaffolding(paths, profile=profile, warnings=warnings)
        schema_validation = validate_project_schemas(staging_path)
        if not schema_validation.get("ok"):
            return _failure(
                status="schema_validation_failed",
                archive=archive,
                target=target_project,
                profile=profile,
                errors=[
                    "Imported project files failed schema validation before they were installed.",
                    *[str(error) for error in schema_validation.get("errors", [])],
                ],
                extra={"schema_validation": schema_validation},
            )
        _install_staging(staging_path, target_project)
        moved_staging = True
    except (OSError, ValueError, WorkflowPathError, json.JSONDecodeError) as error:
        return _failure(
            status="import_failed",
            archive=archive,
            target=target_project,
            profile=profile,
            errors=[f"Unable to install imported project: {error}"],
        )
    finally:
        if not moved_staging:
            shutil.rmtree(staging_path, ignore_errors=True)

    manifest = materialized["manifest"]
    workflow_id = str(manifest.get("current_workflow_id") or workflow_config.get("workflow_id") or "")
    imported_paths = [str(record["path"]) for record in materialized["files"]]
    read_models_dir = target_project / paths.value("read_models_dir")
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "ok": True,
        "status": "imported",
        "profile": profile,
        "archive": archive.as_posix(),
        "target": target_project.as_posix(),
        "workspace_id": manifest.get("workspace_id"),
        "workflow_id": workflow_id,
        "imported_count": len(imported_paths),
        "imported_paths": imported_paths,
        "read_models": {
            "status": "not_imported",
            "rebuild_required": True,
            "read_models_dir": paths.value("read_models_dir"),
            "directory_exists": read_models_dir.exists(),
        },
        "post_import_actions": (
            post_read_only_import_actions(target_project) if profile == "archive" else post_import_actions(target_project)
        ),
        "warnings": warnings,
        "errors": [],
    }


def format_import_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane import: {result.get('status', 'unknown')}",
        f"profile: {result.get('profile') or 'unknown'}",
        f"archive: {result.get('archive') or 'unknown'}",
        f"target: {result.get('target') or 'unknown'}",
    ]
    if result.get("workflow_id"):
        lines.append(f"workflow_id: {result.get('workflow_id')}")
    if result.get("workspace_id"):
        lines.append(f"workspace_id: {result.get('workspace_id')}")
    if result.get("imported_count") is not None:
        lines.append(f"imported_files: {result.get('imported_count')}")
    read_models = result.get("read_models")
    if isinstance(read_models, Mapping):
        lines.append(f"read_models: {read_models.get('status') or 'unknown'}")
        if read_models.get("rebuild_required"):
            lines.append("read_models_rebuild_required: true")
    for key in ("warnings", "errors", "post_import_actions", "recovery_actions"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def import_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    status = str(result.get("status") or "")
    if status in {"unsafe_archive_member", "manifest_hash_mismatch", "manifest_size_mismatch"}:
        return EXIT_SECURITY_POLICY_VIOLATION
    if status in {
        "archive_missing",
        "target_exists_not_directory",
        "target_not_empty",
        "malformed_manifest",
        "manifest_missing",
        "unsupported_profile",
        "archive_import_requires_read_only",
        "schema_validation_failed",
        "read_only_import_requires_archive",
    }:
        return EXIT_INVALID_CONFIG
    return EXIT_GENERIC_FAILURE


def post_import_actions(target: Path) -> list[str]:
    project = target.as_posix()
    return [
        f"loopplane doctor-agent --project {project} --all",
        f"loopplane configure-agent --project {project} --role worker --adapter codex_cli --command codex",
        f"loopplane configure-agent --project {project} --role planner --adapter codex_cli --command codex",
        f"loopplane rebuild-read-models --project {project}",
        f"loopplane health --project {project}",
        f"loopplane resume --project {project} only after runner configuration and health checks are acceptable",
    ]


def post_read_only_import_actions(target: Path) -> list[str]:
    project = target.as_posix()
    return [
        f"loopplane rebuild-read-models --project {project}",
        f"loopplane health --project {project}",
        f"loopplane dashboard --project {project}",
        "Fork the imported workflow before attempting any mutation or resume operation.",
    ]


def _target_preflight_error(target: Path) -> tuple[str, str] | None:
    if target.exists() and not target.is_dir():
        return ("target_exists_not_directory", f"{target}: target exists and is not a directory.")
    if target.is_dir() and any(target.iterdir()):
        return ("target_not_empty", f"{target}: refusing to import over an existing non-empty target directory.")
    return None


def _materialize_archive(
    archive: tarfile.TarFile,
    *,
    archive_path: Path,
    target: Path,
    read_only: bool,
) -> dict[str, Any]:
    members = archive.getmembers()
    member_map: dict[str, tarfile.TarInfo] = {}
    seen: set[str] = set()
    for member in members:
        path_error = _safe_member_path_error(member.name)
        if path_error is not None:
            return _failure(
                status="unsafe_archive_member",
                archive=archive_path,
                target=target,
                errors=[f"{member.name!r}: {path_error}"],
            )
        if member.name in seen:
            return _failure(
                status="unsafe_archive_member",
                archive=archive_path,
                target=target,
                errors=[f"{member.name!r}: duplicate archive member path."],
            )
        seen.add(member.name)
        member_map[member.name] = member

    manifest_member = member_map.get(EXPORT_MANIFEST_NAME)
    if manifest_member is None:
        return _failure(
            status="manifest_missing",
            archive=archive_path,
            target=target,
            errors=[f"{EXPORT_MANIFEST_NAME} is missing from migration archive."],
        )
    if not manifest_member.isfile():
        return _failure(
            status="unsafe_archive_member",
            archive=archive_path,
            target=target,
            errors=[f"{EXPORT_MANIFEST_NAME}: manifest must be a regular file."],
        )
    manifest_file = archive.extractfile(manifest_member)
    if manifest_file is None:
        return _failure(
            status="manifest_missing",
            archive=archive_path,
            target=target,
            errors=[f"{EXPORT_MANIFEST_NAME}: unable to read manifest."],
        )
    manifest = json.loads(manifest_file.read().decode("utf-8"))
    manifest_error = _manifest_error(manifest)
    if manifest_error is not None:
        return _failure(
            status="malformed_manifest",
            archive=archive_path,
            target=target,
            errors=[manifest_error],
        )
    profile = str(manifest["profile"])
    expected_paths = {EXPORT_MANIFEST_NAME}
    records: list[Mapping[str, Any]] = []
    for record in manifest["files"]:
        if not isinstance(record, Mapping):
            return _failure(
                status="malformed_manifest",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=["Manifest files must be JSON objects."],
            )
        path = str(record.get("path") or "")
        path_error = _safe_member_path_error(path) or _portable_import_path_error(path)
        if path_error is not None:
            return _failure(
                status="unsafe_archive_member",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path!r}: {path_error}"],
            )
        records.append(record)
        expected_paths.add(path)

    unexpected = sorted(set(member_map) - expected_paths)
    if unexpected:
        return _failure(
            status="unsafe_archive_member",
            archive=archive_path,
            target=target,
            profile=profile,
            errors=[f"Archive contains members not declared in the manifest: {', '.join(unexpected[:5])}"],
        )

    files: list[dict[str, Any]] = []
    for record in records:
        path = str(record["path"])
        member = member_map.get(path)
        if member is None:
            return _failure(
                status="malformed_manifest",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"Manifest declares {path!r}, but the archive member is missing."],
            )
        if not member.isfile():
            return _failure(
                status="unsafe_archive_member",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: expected a regular file; refusing special archive member."],
            )
        extracted = archive.extractfile(member)
        if extracted is None:
            return _failure(
                status="malformed_manifest",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: unable to read archive member."],
            )
        data = extracted.read()
        expected_size = record.get("size")
        if not isinstance(expected_size, int) or expected_size < 0:
            return _failure(
                status="malformed_manifest",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: manifest size must be a non-negative integer."],
            )
        if len(data) != expected_size:
            return _failure(
                status="manifest_size_mismatch",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: archive member size does not match manifest."],
            )
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            return _failure(
                status="malformed_manifest",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: manifest sha256 must be a hex digest."],
            )
        actual_hash = sha256(data).hexdigest()
        if actual_hash != expected_hash:
            return _failure(
                status="manifest_hash_mismatch",
                archive=archive_path,
                target=target,
                profile=profile,
                errors=[f"{path}: archive member sha256 does not match manifest."],
            )
        files.append({"path": path, "data": data, "category": record.get("category"), "source": record.get("source")})

    required = {
        "PROJECT_BRIEF.md",
        "PLAN.md",
        ".loopplane/workspace.json",
        ".loopplane/workflow_registry.json",
        ".loopplane/current_workflow.json",
    }
    imported_paths = {str(record["path"]) for record in files}
    missing_required = sorted(required - imported_paths)
    if profile in {"stateful", "archive"} and missing_required:
        return _failure(
            status="malformed_manifest",
            archive=archive_path,
            target=target,
            profile=profile,
            errors=[f"Stateful archive is missing required project files: {', '.join(missing_required)}"],
        )

    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "ok": True,
        "status": "validated",
        "profile": profile,
        "read_only": read_only,
        "manifest": manifest,
        "files": files,
    }


def _manifest_error(manifest: Any) -> str | None:
    if not isinstance(manifest, Mapping):
        return f"{EXPORT_MANIFEST_NAME} must contain a JSON object."
    if manifest.get("schema_version") != EXPORT_SCHEMA_VERSION:
        return f"Unsupported export manifest schema_version {manifest.get('schema_version')!r}."
    profile = manifest.get("profile")
    if not isinstance(profile, str) or not profile.strip():
        return "Export manifest profile must be a non-empty string."
    files = manifest.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return "Export manifest files must be an array."
    seen: set[str] = set()
    for index, record in enumerate(files):
        if not isinstance(record, Mapping):
            return f"Export manifest files[{index}] must be an object."
        path = record.get("path")
        if not isinstance(path, str) or not path.strip():
            return f"Export manifest files[{index}].path must be a non-empty string."
        if path in seen:
            return f"Export manifest files contains duplicate path {path!r}."
        seen.add(path)
    return None


def _safe_member_path_error(path: str) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return "archive member path must be non-empty"
    text = path.strip()
    if "\\" in text or WINDOWS_ABSOLUTE_RE.match(text):
        return "archive member path must be a relative POSIX path"
    if text.startswith("/") or text.startswith("//"):
        return "archive member path must not be absolute"
    raw_parts = text.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return "archive member path must not contain empty, current, or parent path segments"
    parsed = PurePosixPath(text)
    if parsed.is_absolute() or any(part == ".." for part in parsed.parts):
        return "archive member path must stay inside the target project"
    return None


def _portable_import_path_error(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if not parts:
        return "empty project path"
    name = parts[-1]
    if any(part in TOOL_OR_VCS_DIRECTORIES for part in parts):
        return "tool or VCS directories are not valid migration import members"
    if name.startswith(SECRET_FILENAME_PREFIXES) or name.endswith(SECRET_FILENAME_SUFFIXES):
        return "machine-local secret files are not valid migration import members"
    if parts[:3] == (".loopplane", "config", "local"):
        return "machine-local config must not be imported"
    if parts[0] == ".loopplane" and "read_models" in parts:
        return "derived read models must be rebuilt after import"
    runtime_index = _first_part_index(parts, "runtime")
    if parts[0] == ".loopplane" and runtime_index is not None:
        runtime_tail = parts[runtime_index + 1 :]
        if runtime_tail and runtime_tail[0] in PROCESS_STATE_RUNTIME_DIRECTORIES:
            return "stale runtime process state must not be imported"
        if name in PROCESS_STATE_FILENAMES or "pid" in name.lower() or "process" in name.lower():
            return "stale runtime process state must not be imported"
    return None


def _write_staging_files(staging: Path, files: Sequence[Mapping[str, Any]]) -> None:
    for record in files:
        relative = str(record["path"])
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(record["data"])


def _localize_workspace_identity(staging: Path, *, final_target: Path, warnings: list[str]) -> None:
    workspace_path = staging / ".loopplane" / "workspace.json"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    if not isinstance(workspace, dict):
        raise ValueError(".loopplane/workspace.json must contain a JSON object")
    workspace["project_root"] = "."
    workspace["loopplane_dir"] = ".loopplane"
    workspace["repo_root"] = repository_root_value(final_target)
    workspace["workspace_boundary"] = "project_root"
    workspace["allow_out_of_boundary_writes"] = bool(workspace.get("allow_out_of_boundary_writes", False))
    workspace["single_active_running_workflow"] = bool(workspace.get("single_active_running_workflow", True))
    workspace_path.write_text(json.dumps(workspace, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    warnings.append("Localized workspace.json repo_root for the import target; workflow identity was preserved.")


def _mark_archive_read_only_import(staging: Path, *, manifest: Mapping[str, Any], warnings: list[str]) -> None:
    registry_path = staging / ".loopplane" / "workflow_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise ValueError(".loopplane/workflow_registry.json must contain a JSON object")
    workflows = registry.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise ValueError(".loopplane/workflow_registry.json must contain a non-empty workflows array")
    now = _utc_now()
    for index, record in enumerate(workflows):
        if not isinstance(record, dict):
            raise ValueError(f".loopplane/workflow_registry.json workflows[{index}] must contain a JSON object")
        previous_status = str(record.get("status") or "")
        previous_archived = bool(record.get("archived"))
        record["status"] = "read_only_imported"
        record["read_only"] = True
        record["archived"] = False
        record["imported_at"] = now
        record["imported_by"] = "loopplane import --read-only"
        record["migration_profile"] = "archive"
        record["resume_allowed"] = False
        record["restore_or_fork_required_for_mutation"] = True
        if previous_status and previous_status != "read_only_imported":
            record["imported_source_status"] = previous_status
        if previous_archived:
            record["imported_source_archived"] = True
    registry["generated_at"] = now
    registry["updated_at"] = now
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    warnings.append("Marked imported archive workflow registry records as read_only_imported.")

    current_path = staging / ".loopplane" / "current_workflow.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    if isinstance(current, dict):
        current["selection_reason"] = "read_only_archive_import"
        current["selected_at"] = now
        current["read_only"] = True
        current["migration_profile"] = "archive"
        current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    intent = manifest.get("migration_intent") if isinstance(manifest.get("migration_intent"), Mapping) else {}
    expected_status = str(intent.get("workflow_status_on_import") or "read_only_imported")
    if expected_status != "read_only_imported":
        warnings.append(
            "Archive manifest did not declare read_only_imported import status; import enforced read-only state."
        )


def _regenerate_local_runtime_scaffolding(paths: WorkflowPaths, *, profile: str, warnings: list[str]) -> None:
    now = _utc_now()
    workflow_id = str(paths.workflow_id or "unknown_workflow")
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    state_path = paths.runtime_dir / "state.json"
    read_only_archive = profile == "archive"
    state = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "status": "read_only_imported" if read_only_archive else "waiting_config",
        "initialized_at": now,
        "configuration_problems": [] if read_only_archive else ["agent runners must be reconfigured after migration"],
        "blocked_reasons": (
            ["archive import is read-only; fork the workflow before mutation or resume"]
            if read_only_archive
            else ["stateful import does not restore machine-local process state"]
        ),
        "scheduler": {
            "last_action": "archive_read_only_import" if read_only_archive else "stateful_import",
            "heartbeat_at": now,
            "migration_profile": profile,
            "resume_allowed": not read_only_archive,
        },
    }
    if read_only_archive:
        state["read_only"] = True
        state["resume_allowed"] = False
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    background_jobs = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "jobs": [],
    }
    (paths.runtime_dir / "background_jobs.json").write_text(
        json.dumps(background_jobs, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    expansion_registry_path = paths.runtime_dir / "expansion_registry.json"
    if not expansion_registry_path.exists():
        expansion_registry = {
            "schema_version": "1.5",
            "workflow_id": workflow_id,
            "cycle": 0,
            "events": [],
            "proposals": [],
        }
        expansion_registry_path.write_text(
            json.dumps(expansion_registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if read_only_archive:
        warnings.append("Regenerated read-only runtime state with no resumable process handles.")
    else:
        warnings.append("Regenerated local runtime state with empty background-job records; no process handles were restored.")


def _install_staging(staging: Path, target: Path) -> None:
    if target.exists():
        for child in staging.iterdir():
            child.rename(target / child.name)
        staging.rmdir()
        return
    staging.rename(target)


def _first_part_index(parts: Sequence[str], needle: str) -> int | None:
    for index, part in enumerate(parts):
        if part == needle:
            return index
    return None


def _failure(
    *,
    status: str,
    archive: Path,
    target: Path,
    errors: Sequence[str],
    profile: str | None = None,
    recovery_actions: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "profile": profile,
        "archive": archive.as_posix(),
        "target": target.as_posix(),
        "imported_count": 0,
        "warnings": [],
        "errors": list(errors),
    }
    if recovery_actions:
        result["recovery_actions"] = list(recovery_actions)
    if extra:
        result.update(dict(extra))
    return result


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
