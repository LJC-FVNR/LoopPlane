from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from runtime.approval import (
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
    approval_record_status,
    default_expires_at,
    load_approval_policy,
    new_approval_id,
    read_approval_requests,
    read_approval_responses,
)
from runtime.exit_codes import (
    EXIT_INVALID_CONFIG,
    EXIT_SUCCESS,
    EXIT_VERSION_CONTROL_UNAVAILABLE,
    EXIT_WAITING_APPROVAL,
    has_text,
)
from runtime.path_resolution import (
    PathSerializationError,
    WorkflowPathError,
    WorkflowPaths,
    load_workflow_config,
    serialize_project_path,
)
from runtime.workspace_identity import relative_root_value, workspace_boundary_root


SCHEMA_VERSION = "1.5"
GIT_REF_BUNDLE_SCHEMA_VERSION = "loopplane-git-ref-bundle-export-1"
GIT_REF_BUNDLE_IMPORT_SCHEMA_VERSION = "loopplane-git-ref-bundle-import-1"
SUPPORTED_PROVIDER = "git"
SUPPORTED_REPOSITORY_MODE = "existing_or_local_init"
SUPPORTED_CHECKPOINT_BACKEND = "managed_refs"
DEFAULT_REFS_PREFIX = "refs/loopplane/"
DEFAULT_GENERATED_PATH_EXCLUDES = (
    "**/__pycache__/",
    "**/*.pyc",
    "**/*.pyo",
    ".pytest_cache/",
    ".coverage",
    "coverage.xml",
    "htmlcov/",
    "LOOPPLANE_DASHBOARD.url",
)
PROJECT_LOCAL_MACHINE_STATE_EXCLUDES = (
    ".loopplane/config/local/",
)
ROLLBACK_REQUESTS_FILENAME = "version_control_rollback_requests.jsonl"
TASK_LINE_RE = re.compile(r"^- \[(?P<status>[ x~!\-])\]\s+(?P<task_id>[A-Za-z0-9_.-]+):\s+(?P<title>.+?)\s*$")
FIELD_LINE_RE = re.compile(r"^  - (?P<field>[A-Za-z0-9_ -]+):(?P<value>.*)$")


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class GitCommandRunner(Protocol):
    def git_path(self) -> str | None:
        ...

    def run(self, project_root: Path, args: Sequence[str]) -> GitCommandResult:
        ...


class SubprocessGitCommandRunner:
    def git_path(self) -> str | None:
        return shutil.which("git")

    def run(
        self,
        project_root: Path,
        args: Sequence[str],
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> GitCommandResult:
        executable = self.git_path()
        if executable is None:
            return GitCommandResult(127, "", "git executable not found")

        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            [executable, "-C", str(project_root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
        return GitCommandResult(completed.returncode, completed.stdout, completed.stderr)


CHECKPOINT_REASONS = {
    "before_plan_activation",
    "after_plan_activation",
    "before_worker_run",
    "after_validation_pass",
    "before_change_request_apply",
    "after_change_request_apply",
    "before_final_completion",
    "after_final_completion",
    "brief_created",
    "planner_draft_created",
    "audit_passed",
    "worker_project_changes_detected",
    "plan_reconciled",
    "workflow_completed",
    "manual_checkpoint",
    "brief_updated",
}


def create_git_checkpoint(
    project_root: Path,
    *,
    reason: str = "manual_checkpoint",
    task_id: str | None = None,
    run_id: str | None = None,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    created_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config_path = paths.version_control_config_file
    config = _load_version_control_config(config_path, errors)

    git = _detect_git(git_runner)
    if not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot create default local checkpoints.")
    if workflow_id is None:
        errors.append("workflow_id is missing; managed checkpoint refs cannot be resolved.")
    if reason not in CHECKPOINT_REASONS:
        warnings.append(f"Checkpoint reason {reason!r} is not a standard LoopPlane checkpoint boundary.")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))
        if config.get("enabled") is False:
            errors.append("Version control is disabled; checkpoint creation is not available.")
    completion_warning = _post_completion_checkpoint_warning(paths, reason=reason)
    if completion_warning:
        warnings.append(completion_warning)

    if errors:
        return _checkpoint_failure(project, workflow_id, git, warnings, errors, created_at)

    init_status = initialize_local_repository_if_missing(project, runner=git_runner)
    if not init_status["ok"]:
        problem = init_status.get("problem")
        message = problem.get("message") if isinstance(problem, dict) else "Git repository is unavailable."
        return _checkpoint_failure(project, workflow_id, git, warnings, [str(message)], created_at)

    repository = _detect_repository(project, git_runner, git["available"])
    if not repository["inside_work_tree"]:
        return _checkpoint_failure(
            project,
            workflow_id,
            git,
            warnings,
            ["Project is not in a Git work tree after initialization."],
            created_at,
        )

    refs_namespace = _resolve_refs_namespace(str(config["refs_namespace"]), workflow_id)
    checkpoint_id = _new_checkpoint_id(created_at)
    checkpoint_ref = f"{refs_namespace.rstrip('/')}/checkpoints/{checkpoint_id}"
    check_ref = _run_git(git_runner, project, ("check-ref-format", checkpoint_ref))
    if check_ref.returncode != 0:
        return _checkpoint_failure(
            project,
            workflow_id,
            git,
            warnings,
            [f"Managed checkpoint ref is invalid: {_compact_command_error(check_ref)}"],
            created_at,
        )

    active_branch_before = _active_branch(project, git_runner)
    head_before = _rev_parse_optional(project, git_runner, "HEAD")
    index_before = _index_fingerprint(project, git_runner)
    path_policy = _workspace_path_policy(
        project,
        config,
        paths,
        extra_excludes=_checkpoint_metadata_excludes(paths),
    )
    all_status_before = _status_entries(project, git_runner, repository=repository)
    status_before = _status_entries(
        project,
        git_runner,
        pathspecs=path_policy["pathspecs"],
        repository=repository,
    )

    checkpoint = _create_checkpoint_commit(
        project,
        git_runner,
        checkpoint_ref,
        checkpoint_id,
        workflow_id,
        reason,
        task_id,
        run_id,
        head_before,
        created_at,
        config,
        path_policy,
    )
    if not checkpoint["ok"]:
        return _checkpoint_failure(project, workflow_id, git, warnings, checkpoint["errors"], created_at)

    active_branch_after = _active_branch(project, git_runner)
    head_after = _rev_parse_optional(project, git_runner, "HEAD")
    index_after = _index_fingerprint(project, git_runner)
    all_status_after = _status_entries(project, git_runner, repository=repository)
    status_after = _status_entries(
        project,
        git_runner,
        pathspecs=path_policy["pathspecs"],
        repository=repository,
    )
    included_paths = _checkpoint_included_paths(project, git_runner, checkpoint["commit"], repository)
    excluded_paths = _dedupe_text(
        [
            *_excluded_status_paths(all_status_before, status_before),
            *_excluded_status_paths(all_status_after, status_after),
        ]
    )

    record = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "workflow_id": workflow_id,
        "checkpoint_id": checkpoint_id,
        "reason": reason,
        "task_id": task_id,
        "run_id": run_id,
        "status": "created",
        "provider": "git",
        "backend": SUPPORTED_CHECKPOINT_BACKEND,
        "repository_root": _repository_root_for_record(project, repository),
        "ref": checkpoint_ref,
        "commit": checkpoint["commit"],
        "tree": checkpoint["tree"],
        "parent": head_before,
        "active_branch_before": active_branch_before,
        "active_branch_after": active_branch_after,
        "head_before": head_before,
        "head_after": head_after,
        "active_branch_unchanged": active_branch_before == active_branch_after,
        "head_unchanged": head_before == head_after,
        "user_index_unchanged": index_before == index_after,
        "status_entries_before": len(status_before),
        "status_entries_after": len(status_after),
        "included_paths": included_paths,
        "excluded_paths": excluded_paths,
        "path_policy": {
            "scope": "workspace_boundary",
            "workspace_boundary": path_policy["workspace_boundary"],
            "resolved_workspace_boundary": path_policy["resolved_workspace_boundary"],
            "allow_out_of_boundary_writes": path_policy["allow_out_of_boundary_writes"],
            "pathspecs": list(path_policy["pathspecs"]),
            "configured_includes": list(path_policy["configured_includes"]),
            "configured_excludes": [
                item for item in path_policy["configured_excludes"] if not _is_git_internal_path(item)
            ],
        },
        "warnings": warnings,
    }
    _append_jsonl(paths.runtime_dir / "git_checkpoints.jsonl", _checkpoint_record_for_log(record))
    _write_version_control_status(
        paths.read_models_dir / "version_control_status.json",
        _checkpoint_record_for_log(record),
        repository,
        project=project,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "ok",
        "ok": True,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "checkpoint": record,
        "warnings": warnings,
        "errors": [],
    }


def _post_completion_checkpoint_warning(paths: WorkflowPaths, *, reason: str) -> str | None:
    if reason in {"before_final_completion", "after_final_completion", "workflow_completed"}:
        return None
    for marker_name in ("plan_loop_complete.json", "completion_marker.json"):
        marker_path = paths.runtime_dir / marker_name
        if marker_path.is_file():
            return (
                "A completion marker already exists; creating a post-completion checkpoint may make status/health "
                "report the marker as stale. Run `loopplane final-verify` again after post-completion notes or packaging changes."
            )
    return None


def _checkpoint_record_for_log(record: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(record)
    for field in ("included_paths", "excluded_paths"):
        values = compact.pop(field, [])
        summary = _path_list_summary(values)
        compact[f"{field}_count"] = summary["count"]
        compact[f"{field}_sample"] = summary["sample"]
        compact[f"{field}_omitted"] = summary["omitted"]
    return compact


def _path_list_summary(values: Any, *, sample_size: int = 20) -> dict[str, Any]:
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        items = [str(value) for value in values]
    else:
        items = []
    return {
        "count": len(items),
        "sample": items[:sample_size],
        "omitted": max(0, len(items) - sample_size),
    }


def capture_run_git_metadata(
    project_root: Path,
    run_dir: Path,
    *,
    stage: str,
    task_id: str,
    run_id: str,
    run_kind: str = "worker",
    detail_level: str = "full",
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    resolved_run_dir = _resolve_run_dir(project, run_dir)
    git_dir = resolved_run_dir / "git"
    git_runner = runner or SubprocessGitCommandRunner()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    paths = workflow["paths"]
    config_path = paths.version_control_config_file
    config = _load_version_control_config(config_path, errors)

    git = _detect_git(git_runner)
    if not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot capture run metadata.")
    if workflow["workflow_id"] is None:
        errors.append("workflow_id is missing; run Git metadata cannot be associated with a workflow.")
    if stage not in {"pre", "post"}:
        errors.append(f"Unsupported run metadata stage: {stage!r}; expected 'pre' or 'post'.")
    if run_kind not in {"worker", "recovery"}:
        errors.append(f"Unsupported run kind: {run_kind!r}; expected 'worker' or 'recovery'.")
    if detail_level not in {"full", "status"}:
        errors.append(f"Unsupported run metadata detail_level: {detail_level!r}; expected 'full' or 'status'.")
    if not task_id:
        errors.append("task_id is required for run Git metadata.")
    if not run_id:
        errors.append("run_id is required for run Git metadata.")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))
        if config.get("enabled") is False:
            errors.append("Version control is disabled; run Git metadata capture is not available.")

    if errors:
        return _run_metadata_failure(
            project,
            resolved_run_dir,
            workflow["workflow_id"],
            git,
            warnings,
            errors,
            generated_at,
            task_id,
            run_id,
            run_kind,
            stage,
        )

    init_status = initialize_local_repository_if_missing(project, runner=git_runner)
    if not init_status["ok"]:
        problem = init_status.get("problem")
        message = problem.get("message") if isinstance(problem, dict) else "Git repository is unavailable."
        return _run_metadata_failure(
            project,
            resolved_run_dir,
            workflow["workflow_id"],
            git,
            warnings,
            [str(message)],
            generated_at,
            task_id,
            run_id,
            run_kind,
            stage,
        )

    repository = _detect_repository(project, git_runner, git["available"])
    if not repository["inside_work_tree"]:
        return _run_metadata_failure(
            project,
            resolved_run_dir,
            workflow["workflow_id"],
            git,
            warnings,
            ["Project is not in a Git work tree after initialization."],
            generated_at,
            task_id,
            run_id,
            run_kind,
            stage,
        )

    assert config is not None
    assert workflow["workflow_id"] is not None
    if detail_level == "status":
        if stage == "pre":
            return _capture_pre_run_git_status_metadata(
                project,
                resolved_run_dir,
                git_dir,
                workflow["workflow_id"],
                paths,
                config,
                git_runner,
                git,
                repository,
                warnings,
                task_id,
                run_id,
                run_kind,
                generated_at,
            )
        return _capture_post_run_git_status_metadata(
            project,
            resolved_run_dir,
            git_dir,
            workflow["workflow_id"],
            paths,
            config,
            git_runner,
            git,
            repository,
            warnings,
            task_id,
            run_id,
            run_kind,
            generated_at,
        )
    if stage == "pre":
        return _capture_pre_run_git_metadata(
            project,
            resolved_run_dir,
            git_dir,
            workflow["workflow_id"],
            paths,
            config,
            git_runner,
            git,
            repository,
            warnings,
            task_id,
            run_id,
            run_kind,
            generated_at,
        )
    return _capture_post_run_git_metadata(
        project,
        resolved_run_dir,
        git_dir,
        workflow["workflow_id"],
        paths,
        config,
        git_runner,
        git,
        repository,
        warnings,
        task_id,
        run_id,
        run_kind,
        generated_at,
    )


def load_version_control_status(
    project_root: Path,
    *,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config_path = paths.version_control_config_file
    config = _load_version_control_config(config_path, errors)
    read_model = _load_json_mapping(paths.read_models_dir / "version_control_status.json")
    latest_checkpoint = _latest_checkpoint_record(paths.runtime_dir / "git_checkpoints.jsonl")

    git = _detect_git(git_runner)
    repository = _detect_repository(project, git_runner, git["available"])
    repository_source = "git" if git["available"] else "read_model"
    if not repository["inside_work_tree"]:
        repository = _repository_from_read_model(read_model, fallback=repository)
        if repository["inside_work_tree"]:
            repository_source = "read_model"
    elif config is not None:
        repository = _repository_with_scoped_dirty_status(
            project,
            git_runner,
            repository,
            _workspace_path_policy(
                project,
                config,
                paths,
                extra_excludes=_checkpoint_metadata_excludes(paths),
            ),
        )

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))

    read_model_warnings = read_model.get("warnings") if isinstance(read_model, Mapping) else None
    if isinstance(read_model_warnings, Sequence) and not isinstance(read_model_warnings, (str, bytes)):
        warnings.extend(str(warning) for warning in read_model_warnings)

    enabled = _config_enabled(config, read_model)
    if enabled and not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot report live repository status.")
    if enabled and git["available"] and not repository["inside_work_tree"]:
        errors.append("Git repository is unavailable; LoopPlane init should initialize a local repository.")

    if latest_checkpoint is None:
        latest_checkpoint = _checkpoint_from_read_model(read_model)

    rollback = _rollback_status(
        config=config,
        enabled=enabled,
        git_available=git["available"],
        repo_detected=bool(repository.get("inside_work_tree")),
        latest_checkpoint=latest_checkpoint,
        errors=errors,
    )

    dirty = bool(repository.get("dirty"))
    changed_files_count = int(repository.get("dirty_files_count") or 0)
    if repository_source == "read_model":
        dirty = _read_model_dirty(read_model, default=dirty)
        changed_files_count = _read_model_dirty_files_count(read_model, default=changed_files_count)

    status = _status_label(errors=errors, warnings=warnings, enabled=enabled)
    configuration = _configuration_summary(config_path, config, workflow_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": not errors,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "provider": _provider(config, read_model),
        "enabled": enabled,
        "backend": _config_value(config, read_model, "checkpoint_backend", "backend"),
        "repository_mode": _config_value(config, read_model, "repository_mode"),
        "git": {
            "available": bool(git.get("available")),
        },
        "git_available": bool(git.get("available")),
        "repo_detected": bool(repository.get("inside_work_tree")),
        "repository": {
            "detected": bool(repository.get("inside_work_tree")),
            "inside_work_tree": bool(repository.get("inside_work_tree")),
            "root": repository.get("root"),
            "head_commit": repository.get("head_commit"),
            "dirty": dirty,
            "dirty_files_count": changed_files_count,
        },
        "dirty": dirty,
        "changed_files_count": changed_files_count,
        "last_checkpoint": latest_checkpoint,
        "configuration": configuration,
        "rollback": rollback,
        "rollback_available": bool(rollback.get("available")),
        "checkpoint_model": {
            "backend": _config_value(config, read_model, "checkpoint_backend", "backend"),
            "branch_cleanliness_required": False,
            "note": (
                "LoopPlane checkpoints are stored in managed refs and do not clean or commit the user's current branch. "
                "A dirty worktree can still be covered by the latest LoopPlane checkpoint."
            ),
            "latest_checkpoint_covers_dirty_worktree": latest_checkpoint is not None,
        },
        "sources": {
            "read_model_loaded": isinstance(read_model, Mapping),
            "checkpoint_log_loaded": latest_checkpoint is not None,
            "repository_source": repository_source,
            "git_fallback_used": repository_source == "git",
        },
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def load_task_diff_metadata(project_root: Path, *, task_id: str) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    if not task_id or not task_id.strip():
        errors.append("task_id is required for task diff metadata.")
        return _task_diff_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            task_id=task_id,
            status="invalid_request",
            ok=False,
            warnings=warnings,
            errors=errors,
        )
    task_id = task_id.strip()

    task_metadata = _plan_task_metadata(paths.plan_file)
    if task_metadata is None:
        errors.append("PLAN.md is unavailable; task diff metadata cannot be resolved.")
        return _task_diff_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            task_id=task_id,
            status="waiting_config",
            ok=False,
            warnings=warnings,
            errors=errors,
        )
    if task_id not in task_metadata:
        errors.append(f"Task {task_id!r} was not found in PLAN.md.")
        return _task_diff_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            task_id=task_id,
            status="task_not_found",
            ok=False,
            warnings=warnings,
            errors=errors,
        )
    if errors:
        return _task_diff_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            task_id=task_id,
            status="waiting_config",
            ok=False,
            warnings=warnings,
            errors=errors,
        )

    candidates = _task_diff_candidates(project, paths, task_id, task_metadata[task_id])
    selected: Mapping[str, Any] | None = None
    selected_payload: Mapping[str, Any] | None = None
    invalid_artifact_errors: list[str] = []
    for candidate in candidates:
        run_dir = candidate["run_dir"]
        changed_files_path = run_dir / "git" / "changed_files.json"
        if not changed_files_path.is_file():
            continue
        payload = _load_json_mapping(changed_files_path)
        if not isinstance(payload, Mapping):
            invalid_artifact_errors.append(
                f"{_path_for_project_record(project, changed_files_path)} is not valid changed-file metadata."
            )
            continue
        selected = candidate
        selected_payload = payload
        break

    if selected is None or selected_payload is None:
        if invalid_artifact_errors:
            return _task_diff_result(
                project=project,
                workflow_id=workflow_id,
                generated_at=generated_at,
                task_id=task_id,
                status="invalid_metadata",
                ok=False,
                warnings=warnings,
                errors=invalid_artifact_errors,
                runs_considered=_candidate_records(project, candidates),
            )
        return _task_diff_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            task_id=task_id,
            status="unavailable",
            ok=True,
            warnings=warnings,
            errors=[],
            message=(
                f"No captured diff metadata is available for task {task_id}. "
                "Expected git/changed_files.json and git/project_diff.patch under a task run directory."
            ),
            runs_considered=_candidate_records(project, candidates),
        )

    run_dir = selected["run_dir"]
    git_dir = run_dir / "git"
    state = _load_json_mapping(git_dir / "run_metadata_state.json")
    changed_files, sanitization_warnings = _sanitize_changed_files(selected_payload.get("changed_files"))
    warnings.extend(sanitization_warnings)
    run_id = str(selected_payload.get("run_id") or selected.get("run_id") or run_dir.name)
    checkpoints = _task_run_checkpoints(paths.runtime_dir / "git_checkpoints.jsonl", task_id=task_id, run_id=run_id)
    before_checkpoint = _before_run_checkpoint(state, checkpoints)
    after_checkpoint = _after_run_checkpoint(checkpoints)
    patch_path = git_dir / "project_diff.patch"
    patch = _patch_artifact(project, patch_path)
    artifacts = [
        _path_for_project_record(project, path)
        for path in (
            git_dir / "pre_run_head.txt",
            git_dir / "pre_run_status.json",
            git_dir / "post_run_status.json",
            git_dir / "changed_files.json",
            git_dir / "project_diff.patch",
            git_dir / "run_metadata_state.json",
        )
        if path.exists()
    ]
    diff = {
        "available": True,
        "base_commit": _string_or_none(selected_payload.get("base_commit") or _mapping_value(state, "base_commit")),
        "current_tree": _string_or_none(selected_payload.get("current_tree")),
        "changed_files_count": len(changed_files),
        "changed_files": changed_files,
        "summary": _changed_files_summary(changed_files),
        "patch": patch,
    }
    return _task_diff_result(
        project=project,
        workflow_id=workflow_id,
        generated_at=generated_at,
        task_id=task_id,
        status="ok",
        ok=True,
        warnings=warnings,
        errors=[],
        run_id=run_id,
        run_dir=run_dir,
        source=str(selected.get("source") or "run_artifact"),
        diff=diff,
        checkpoints={
            "before": before_checkpoint,
            "after": after_checkpoint,
        },
        artifacts=artifacts,
        runs_considered=_candidate_records(project, candidates),
    )


def load_checkpoint_log(project_root: Path, *, limit: int | None = None) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    invalid_request = limit is not None and limit <= 0
    if invalid_request:
        errors.append("limit must be a positive integer.")

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config = _load_version_control_config(paths.version_control_config_file, errors)
    read_model = _load_json_mapping(paths.read_models_dir / "version_control_status.json")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))

    read_model_warnings = read_model.get("warnings") if isinstance(read_model, Mapping) else None
    if isinstance(read_model_warnings, Sequence) and not isinstance(read_model_warnings, (str, bytes)):
        warnings.extend(_safe_log_text(warning) or "Ignoring unsafe version-control read-model warning." for warning in read_model_warnings)

    enabled = _config_enabled(config, read_model)
    git_available = _read_model_git_available(read_model)
    read_model_problem = _read_model_problem(read_model)
    if enabled and git_available is False:
        errors.append("Git is unavailable according to version_control_status.json; checkpoint log may be stale.")
    if read_model_problem and enabled:
        errors.append(read_model_problem)

    checkpoint_path = paths.runtime_dir / "git_checkpoints.jsonl"
    records, record_problems, missing_log = _checkpoint_log_records(checkpoint_path)
    if missing_log:
        warnings.append(f"Checkpoint log is missing: {_path_for_project_record(project, checkpoint_path)}.")

    total_count = len(records)
    checkpoints = list(reversed(records))
    if limit is not None and limit > 0:
        checkpoints = checkpoints[:limit]

    last_checkpoint = checkpoints[0] if checkpoints else None
    if not last_checkpoint:
        read_model_checkpoint = _checkpoint_from_read_model(read_model)
        if read_model_checkpoint is not None:
            warnings.append("version_control_status.json reports a last checkpoint that is absent from git_checkpoints.jsonl.")

    metadata_invalid = bool(record_problems)
    warnings.extend(record_problems)
    status = _checkpoint_log_status(
        errors=errors,
        invalid_request=invalid_request,
        metadata_invalid=metadata_invalid,
        enabled=enabled,
        checkpoint_count=total_count,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": not errors and not metadata_invalid,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "provider": _provider(config, read_model),
        "enabled": enabled,
        "git_available": git_available,
        "backend": _config_value(config, read_model, "checkpoint_backend", "backend"),
        "repository_mode": _config_value(config, read_model, "repository_mode"),
        "checkpoint_count": total_count,
        "returned_count": len(checkpoints),
        "limit": limit,
        "order": "newest_first",
        "last_checkpoint": last_checkpoint,
        "checkpoints": checkpoints,
        "sources": {
            "checkpoint_log": _path_for_project_record(project, checkpoint_path),
            "checkpoint_log_loaded": not missing_log,
            "read_model_loaded": isinstance(read_model, Mapping),
            "read_model_enrichment_used": isinstance(read_model, Mapping),
            "direct_git_reads": False,
        },
        "message": "No checkpoints recorded." if total_count == 0 and not errors and not metadata_invalid else None,
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def request_checkpoint_rollback(
    project_root: Path,
    *,
    checkpoint_id: str,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    safe_checkpoint_id = _safe_log_text(checkpoint_id)
    if safe_checkpoint_id is None:
        errors.append("checkpoint id is required and must not reference unsafe paths.")

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config = _load_version_control_config(paths.version_control_config_file, errors)
    read_model = _load_json_mapping(paths.read_models_dir / "version_control_status.json")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))

    enabled = _config_enabled(config, read_model)
    git = _detect_git(git_runner)
    repository = _detect_repository(project, git_runner, git["available"])
    repository_source = "git" if git["available"] else "read_model"
    if not repository["inside_work_tree"]:
        repository = _repository_from_read_model(read_model, fallback=repository)
        if repository["inside_work_tree"]:
            repository_source = "read_model"
    elif config is not None:
        repository = _repository_with_scoped_dirty_status(
            project,
            git_runner,
            repository,
            _workspace_path_policy(
                project,
                config,
                paths,
                extra_excludes=_checkpoint_metadata_excludes(paths),
            ),
        )

    if not enabled:
        errors.append("Version control is disabled; rollback is unavailable.")
    if enabled and not git["available"]:
        errors.append("Git is unavailable; rollback requires managed checkpoint metadata and a local repository.")
    if enabled and git["available"] and not repository["inside_work_tree"]:
        errors.append("Git repository is unavailable; rollback cannot be prepared.")

    policy = _rollback_policy(config)
    if not policy["allow_rollback"]:
        errors.append("Rollback is disabled by version-control rollback_policy.")

    checkpoint_path = paths.runtime_dir / "git_checkpoints.jsonl"
    checkpoints, record_problems, missing_log = _checkpoint_log_records(checkpoint_path)
    if missing_log:
        warnings.append(f"Checkpoint log is missing: {_path_for_project_record(project, checkpoint_path)}.")
    if record_problems:
        warnings.extend(record_problems)
        errors.append("Checkpoint metadata is invalid; rollback request was not recorded.")

    target_checkpoint: dict[str, Any] | None = None
    if safe_checkpoint_id is not None and not record_problems:
        for checkpoint in checkpoints:
            if str(checkpoint.get("checkpoint_id") or "") == safe_checkpoint_id:
                target_checkpoint = dict(checkpoint)
                break
        if target_checkpoint is None:
            if checkpoints:
                errors.append(f"Checkpoint {safe_checkpoint_id} was not found in LoopPlane checkpoint metadata.")
            else:
                errors.append("No checkpoints are recorded; rollback cannot be prepared.")

    if target_checkpoint is not None:
        if target_checkpoint.get("backend") != SUPPORTED_CHECKPOINT_BACKEND:
            errors.append("Checkpoint metadata does not use the managed_refs backend.")
        if not _safe_log_text(target_checkpoint.get("commit")):
            errors.append("Checkpoint metadata is missing a safe checkpoint commit.")

    affected_paths: list[dict[str, Any]] = []
    affected_warnings: list[str] = []
    if target_checkpoint is not None and not errors and git["available"] and repository["inside_work_tree"]:
        affected_paths, affected_warnings = _rollback_affected_paths(project, paths, git_runner, config, target_checkpoint)
        warnings.extend(affected_warnings)

    dirty = bool(repository.get("dirty"))
    changed_files_count = int(repository.get("dirty_files_count") or 0)
    if repository_source == "read_model":
        dirty = _read_model_dirty(read_model, default=dirty)
        changed_files_count = _read_model_dirty_files_count(read_model, default=changed_files_count)
    risk_summary = _rollback_risk_summary(
        dirty=dirty,
        dirty_files_count=changed_files_count,
        affected_paths=affected_paths,
        policy=policy,
    )

    sources = _rollback_sources(
        project,
        paths,
        checkpoint_log_loaded=not missing_log,
        read_model_loaded=isinstance(read_model, Mapping),
    )
    status = _rollback_request_status(
        errors=errors,
        record_problems=record_problems,
        enabled=enabled,
        checkpoint_count=len(checkpoints),
    )
    if errors:
        return _rollback_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            status=status,
            ok=False,
            checkpoint_id=safe_checkpoint_id,
            target_checkpoint=target_checkpoint,
            affected_paths=affected_paths,
            risk_summary=risk_summary,
            rollback_request=None,
            approval_request=None,
            approval_policy=None,
            approval_required=False,
            execution=None,
            sources=sources,
            warnings=warnings,
            errors=errors,
        )

    approval_policy = load_approval_policy(paths)
    approval_required = bool(policy["requires_approval"] and approval_policy.get("enabled") is True)
    risk_summary = _effective_rollback_risk_summary(risk_summary, approval_required=approval_required)
    rollback_request_id = _new_rollback_request_id()

    if not approval_required:
        execution = _execute_checkpoint_rollback(
            project=project,
            runner=git_runner,
            checkpoint=target_checkpoint or {},
            affected_paths=affected_paths,
        )
        execution_sources = {**dict(sources), "mutation_performed": bool(execution.get("worktree_mutated"))}
        rollback_request = _new_rollback_request(
            workflow_id=workflow_id,
            rollback_request_id=rollback_request_id,
            checkpoint=target_checkpoint or {},
            affected_paths=affected_paths,
            risk_summary=risk_summary,
            approval_request=None,
            approval_policy=approval_policy,
            sources=execution_sources,
            status="executed" if execution.get("ok") else "failed",
            approval_required=False,
            execution=execution,
        )
        _append_jsonl(paths.requests_dir / ROLLBACK_REQUESTS_FILENAME, rollback_request)
        return _rollback_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            status="executed" if execution.get("ok") else "failed",
            ok=bool(execution.get("ok")),
            checkpoint_id=safe_checkpoint_id,
            target_checkpoint=target_checkpoint,
            affected_paths=affected_paths,
            risk_summary=risk_summary,
            rollback_request=rollback_request,
            approval_request=None,
            approval_policy=approval_policy,
            approval_required=False,
            execution=execution,
            sources=execution_sources,
            warnings=warnings,
            errors=list(execution.get("errors") or []),
        )

    existing_request, existing_approval = _existing_pending_rollback_request(paths, safe_checkpoint_id or "")
    if existing_request is not None and existing_approval is not None:
        rollback_request = existing_request
        approval_request = existing_approval
    else:
        approval_request = _new_rollback_approval_request(
            workflow_id=workflow_id,
            rollback_request_id=rollback_request_id,
            checkpoint=target_checkpoint or {},
            affected_paths=affected_paths,
            risk_summary=risk_summary,
            sources=sources,
        )
        rollback_request = _new_rollback_request(
            workflow_id=workflow_id,
            rollback_request_id=rollback_request_id,
            checkpoint=target_checkpoint or {},
            affected_paths=affected_paths,
            risk_summary=risk_summary,
            approval_request=approval_request,
            approval_policy=approval_policy,
            sources=sources,
            status="approval_required",
            approval_required=True,
            execution=None,
        )
        _append_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME, approval_request)
        _append_jsonl(paths.requests_dir / ROLLBACK_REQUESTS_FILENAME, rollback_request)

    return _rollback_result(
        project=project,
        workflow_id=workflow_id,
        generated_at=generated_at,
        status="approval_required",
        ok=True,
        checkpoint_id=safe_checkpoint_id,
        target_checkpoint=target_checkpoint,
        affected_paths=affected_paths,
        risk_summary=risk_summary,
        rollback_request=rollback_request,
        approval_request=approval_request,
        approval_policy=approval_policy,
        approval_required=True,
        execution=None,
        sources=sources,
        warnings=warnings,
        errors=[],
    )


def export_git_checkpoint_bundle(
    project_root: Path,
    *,
    output: Path,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    output_path = output.expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config = _load_version_control_config(paths.version_control_config_file, errors)

    git = _detect_git(git_runner)
    if not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot export checkpoint refs.")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))
        if config.get("enabled") is False:
            errors.append("Version control is disabled; checkpoint bundle export is unavailable.")

    repository = _detect_repository(project, git_runner, git["available"])
    if git["available"] and not repository["inside_work_tree"]:
        errors.append("Git repository is unavailable; LoopPlane checkpoint refs cannot be exported.")

    output_error = _validate_bundle_output_path(project, output_path)
    if output_error:
        errors.append(output_error)

    checkpoint_path = paths.runtime_dir / "git_checkpoints.jsonl"
    checkpoint_records, checkpoint_warnings, missing_checkpoint_log = _checkpoint_log_records(checkpoint_path)
    if missing_checkpoint_log:
        warnings.append(f"Checkpoint log is missing: {_path_for_project_record(project, checkpoint_path)}.")
    warnings.extend(checkpoint_warnings)

    refs_namespace = None
    if config is not None and workflow_id is not None:
        refs_namespace = _resolve_refs_namespace(str(config.get("refs_namespace") or ""), workflow_id)

    refs: list[str] = []
    refs_error: str | None = None
    if not errors and refs_namespace:
        refs, refs_error = _managed_checkpoint_refs(project, git_runner, refs_namespace)
        if refs_error:
            errors.append(refs_error)
        elif not refs:
            errors.append("No LoopPlane-managed checkpoint refs were found to export.")

    active_branch_before = _active_branch(project, git_runner) if git["available"] and repository["inside_work_tree"] else None
    head_before = _rev_parse_optional(project, git_runner, "HEAD") if git["available"] and repository["inside_work_tree"] else None
    index_before = _index_fingerprint(project, git_runner) if git["available"] and repository["inside_work_tree"] else ""

    if errors:
        return _bundle_export_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            status=_bundle_export_status(errors=errors, no_refs=not refs),
            ok=False,
            output=output_path,
            git=git,
            repository=repository,
            refs_namespace=refs_namespace,
            refs=refs,
            checkpoint_records=checkpoint_records,
            checkpoint_log_path=checkpoint_path,
            bundle_heads=[],
            active_branch_before=active_branch_before,
            active_branch_after=active_branch_before,
            head_before=head_before,
            head_after=head_before,
            index_unchanged=True,
            warnings=warnings,
            errors=errors,
        )

    assert refs_namespace is not None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        errors.append(f"Unable to create bundle output directory: {error}")
        return _bundle_export_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            status="invalid_request",
            ok=False,
            output=output_path,
            git=git,
            repository=repository,
            refs_namespace=refs_namespace,
            refs=refs,
            checkpoint_records=checkpoint_records,
            checkpoint_log_path=checkpoint_path,
            bundle_heads=[],
            active_branch_before=active_branch_before,
            active_branch_after=active_branch_before,
            head_before=head_before,
            head_after=head_before,
            index_unchanged=True,
            warnings=warnings,
            errors=errors,
        )
    create = _run_git(git_runner, project, ("bundle", "create", str(output_path), *refs))
    if create.returncode != 0:
        errors.append(f"git bundle create failed: {_compact_command_error(create)}")
        return _bundle_export_result(
            project=project,
            workflow_id=workflow_id,
            generated_at=generated_at,
            status="bundle_create_failed",
            ok=False,
            output=output_path,
            git=git,
            repository=repository,
            refs_namespace=refs_namespace,
            refs=refs,
            checkpoint_records=checkpoint_records,
            checkpoint_log_path=checkpoint_path,
            bundle_heads=[],
            active_branch_before=active_branch_before,
            active_branch_after=_active_branch(project, git_runner),
            head_before=head_before,
            head_after=_rev_parse_optional(project, git_runner, "HEAD"),
            index_unchanged=index_before == _index_fingerprint(project, git_runner),
            warnings=warnings,
            errors=errors,
        )

    verify = _run_git(git_runner, project, ("bundle", "verify", str(output_path)))
    if verify.returncode != 0:
        errors.append(f"git bundle verify failed: {_compact_command_error(verify)}")

    bundle_heads, head_warnings = _bundle_heads(project, git_runner, output_path)
    warnings.extend(head_warnings)
    unexpected_heads = [head["ref"] for head in bundle_heads if not _is_managed_checkpoint_ref(str(head.get("ref")), refs_namespace)]
    if unexpected_heads:
        errors.append("Git bundle contains non-LoopPlane managed refs: " + ", ".join(sorted(unexpected_heads)))

    active_branch_after = _active_branch(project, git_runner)
    head_after = _rev_parse_optional(project, git_runner, "HEAD")
    index_after = _index_fingerprint(project, git_runner)
    return _bundle_export_result(
        project=project,
        workflow_id=workflow_id,
        generated_at=generated_at,
        status="exported" if not errors else "invalid_bundle",
        ok=not errors,
        output=output_path,
        git=git,
        repository=repository,
        refs_namespace=refs_namespace,
        refs=refs,
        checkpoint_records=checkpoint_records,
        checkpoint_log_path=checkpoint_path,
        bundle_heads=bundle_heads,
        active_branch_before=active_branch_before,
        active_branch_after=active_branch_after,
        head_before=head_before,
        head_after=head_after,
        index_unchanged=index_before == index_after,
        warnings=warnings,
        errors=errors,
    )


def import_git_checkpoint_bundle(
    project_root: Path,
    *,
    bundle: Path,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    bundle_path = bundle.expanduser()
    if not bundle_path.is_absolute():
        bundle_path = (Path.cwd() / bundle_path).resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    generated_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    workflow_id = workflow["workflow_id"]
    paths = workflow["paths"]
    config = _load_version_control_config(paths.version_control_config_file, errors)

    git = _detect_git(git_runner)
    if not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot import checkpoint refs.")

    if config is not None:
        warnings.extend(_configuration_warnings(config))
        errors.extend(_configuration_errors(config))
        if config.get("enabled") is False:
            errors.append("Version control is disabled; checkpoint bundle import is unavailable.")

    input_error = _validate_bundle_input_path(bundle_path)
    if input_error:
        errors.append(input_error)

    refs_namespace = None
    if config is not None and workflow_id is not None:
        refs_namespace = _resolve_refs_namespace(str(config.get("refs_namespace") or ""), workflow_id)

    init_status: Mapping[str, Any] | None = None
    repository = _detect_repository(project, git_runner, git["available"])
    if not errors:
        init_status = initialize_local_repository_if_missing(project, runner=git_runner)
        if not init_status["ok"]:
            problem = init_status.get("problem")
            message = problem.get("message") if isinstance(problem, Mapping) else "Git repository is unavailable."
            errors.append(str(message))
        repository = _detect_repository(project, git_runner, git["available"])
        if not repository["inside_work_tree"]:
            errors.append("Git repository is unavailable; LoopPlane checkpoint refs cannot be imported.")

    active_branch_before = _active_branch(project, git_runner) if git["available"] and repository["inside_work_tree"] else None
    head_before = _rev_parse_optional(project, git_runner, "HEAD") if git["available"] and repository["inside_work_tree"] else None
    index_before = _index_fingerprint(project, git_runner) if git["available"] and repository["inside_work_tree"] else ""

    checkpoint_path = paths.runtime_dir / "git_checkpoints.jsonl"
    checkpoint_records, checkpoint_warnings, missing_checkpoint_log = _checkpoint_log_records(checkpoint_path)
    if missing_checkpoint_log:
        warnings.append(f"Checkpoint log is missing: {_path_for_project_record(project, checkpoint_path)}.")
    warnings.extend(checkpoint_warnings)

    bundle_heads: list[dict[str, str]] = []
    managed_heads: list[dict[str, str]] = []
    ignored_heads: list[dict[str, str]] = []
    imported_refs: list[dict[str, Any]] = []

    if not errors:
        verify = _run_git(git_runner, project, ("bundle", "verify", str(bundle_path)))
        if verify.returncode != 0:
            errors.append(f"git bundle verify failed: {_compact_command_error(verify)}")

    if not errors:
        bundle_heads, head_warnings = _bundle_heads(project, git_runner, bundle_path)
        warnings.extend(head_warnings)
        managed_heads = [
            dict(head)
            for head in bundle_heads
            if _is_managed_checkpoint_ref(str(head.get("ref") or ""), refs_namespace)
        ]
        ignored_heads = [
            dict(head)
            for head in bundle_heads
            if not _is_managed_checkpoint_ref(str(head.get("ref") or ""), refs_namespace)
        ]
        if ignored_heads:
            warnings.append(
                "Ignoring non-LoopPlane-managed refs from bundle: "
                + ", ".join(sorted(str(head.get("ref") or "unknown") for head in ignored_heads))
            )
        if not managed_heads:
            errors.append("No LoopPlane-managed checkpoint refs were found to import from the bundle.")

    if not errors:
        unbundle = _run_git(git_runner, project, ("bundle", "unbundle", str(bundle_path)))
        if unbundle.returncode != 0:
            errors.append(f"git bundle unbundle failed: {_compact_command_error(unbundle)}")

    if not errors:
        for head in managed_heads:
            ref = str(head.get("ref") or "")
            commit = str(head.get("commit") or "")
            check_ref = _run_git(git_runner, project, ("check-ref-format", ref))
            if check_ref.returncode != 0:
                errors.append(f"Managed checkpoint ref is invalid: {_compact_command_error(check_ref)}")
                continue
            cat_file = _run_git(git_runner, project, ("cat-file", "-e", f"{commit}^{{commit}}"))
            if cat_file.returncode != 0:
                errors.append(f"Imported bundle head is not a commit for {ref}: {_compact_command_error(cat_file)}")
                continue
            old_commit = _rev_parse_optional(project, git_runner, ref)
            if old_commit == commit:
                imported_refs.append(
                    {
                        "ref": ref,
                        "commit": commit,
                        "previous_commit": old_commit,
                        "action": "unchanged",
                    }
                )
                continue
            update_ref = _run_git(git_runner, project, ("update-ref", ref, commit))
            if update_ref.returncode != 0:
                errors.append(f"git update-ref failed for managed checkpoint ref {ref}: {_compact_command_error(update_ref)}")
                continue
            imported_refs.append(
                {
                    "ref": ref,
                    "commit": commit,
                    "previous_commit": old_commit,
                    "action": "created" if old_commit is None else "updated",
                }
            )

    active_branch_after = _active_branch(project, git_runner) if git["available"] and repository["inside_work_tree"] else active_branch_before
    head_after = _rev_parse_optional(project, git_runner, "HEAD") if git["available"] and repository["inside_work_tree"] else head_before
    index_after = _index_fingerprint(project, git_runner) if git["available"] and repository["inside_work_tree"] else index_before
    return _bundle_import_result(
        project=project,
        workflow_id=workflow_id,
        generated_at=generated_at,
        status=_bundle_import_status(errors=errors, imported_refs=imported_refs),
        ok=not errors,
        bundle=bundle_path,
        git=git,
        repository=_detect_repository(project, git_runner, git["available"]),
        refs_namespace=refs_namespace,
        bundle_heads=bundle_heads,
        managed_heads=managed_heads,
        ignored_heads=ignored_heads,
        imported_refs=imported_refs,
        checkpoint_records=checkpoint_records,
        checkpoint_log_path=checkpoint_path,
        init_status=init_status,
        active_branch_before=active_branch_before,
        active_branch_after=active_branch_after,
        head_before=head_before,
        head_after=head_after,
        index_unchanged=index_before == index_after,
        warnings=warnings,
        errors=errors,
    )


def format_status_text(result: Mapping[str, Any]) -> str:
    repository = result.get("repository") if isinstance(result.get("repository"), Mapping) else {}
    git = result.get("git") if isinstance(result.get("git"), Mapping) else {}
    rollback = result.get("rollback") if isinstance(result.get("rollback"), Mapping) else {}
    checkpoint = result.get("last_checkpoint")
    checkpoint_label = "none"
    if isinstance(checkpoint, Mapping):
        checkpoint_label = str(checkpoint.get("checkpoint_id") or "unknown")
        reason = checkpoint.get("reason")
        created_at = checkpoint.get("created_at")
        details = [str(value) for value in (reason, created_at) if value]
        if details:
            checkpoint_label = f"{checkpoint_label} ({', '.join(details)})"

    changed_files_count = int(result.get("changed_files_count") or 0)
    lines = [
        "LoopPlane version control status",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    lines.extend(
        [
            f"Provider: {result.get('provider') or 'unknown'}",
            f"Git enabled: {_yes_no(bool(result.get('enabled')))}",
            f"Git available: {_yes_no(bool(git.get('available')))}",
            f"Repository detected: {_yes_no(bool(repository.get('detected') or repository.get('inside_work_tree')))}",
        ]
    )
    if repository.get("root"):
        lines.append(f"Repository root: {repository.get('root')}")
    if repository.get("head_commit"):
        lines.append(f"HEAD commit: {repository.get('head_commit')}")
    lines.extend(
        [
            f"Dirty: {_yes_no(bool(result.get('dirty')))}",
            f"Changed files: {changed_files_count}",
            f"Last checkpoint: {checkpoint_label}",
            f"Rollback available: {_yes_no(bool(rollback.get('available')))}",
        ]
    )
    checkpoint_model = result.get("checkpoint_model") if isinstance(result.get("checkpoint_model"), Mapping) else {}
    if checkpoint_model.get("note"):
        lines.append(f"Checkpoint model: {checkpoint_model.get('note')}")
    configuration = result.get("configuration") if isinstance(result.get("configuration"), Mapping) else {}
    effective_worker_checkpointing = (
        configuration.get("effective_worker_checkpointing")
        if isinstance(configuration.get("effective_worker_checkpointing"), Mapping)
        else {}
    )
    if effective_worker_checkpointing:
        lines.append(
            "Worker checkpoints effective: "
            f"{_yes_no(bool(effective_worker_checkpointing.get('enabled')))} "
            f"({effective_worker_checkpointing.get('reason') or 'unknown'})"
        )
    if rollback.get("requires_approval"):
        lines.append("Rollback approval required: yes")
    elif "requires_approval" in rollback:
        lines.append("Rollback approval required: no")
    if rollback.get("reason") and not rollback.get("available"):
        lines.append(f"Rollback unavailable reason: {rollback.get('reason')}")
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_checkpoint_log_text(result: Mapping[str, Any]) -> str:
    lines = [
        "LoopPlane checkpoint log",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    source = result.get("sources") if isinstance(result.get("sources"), Mapping) else {}
    lines.extend(
        [
            f"Provider: {result.get('provider') or 'unknown'}",
            f"Version control enabled: {_yes_no(bool(result.get('enabled')))}",
            f"Git availability source: version_control_status.json",
            f"Checkpoint source: {source.get('checkpoint_log') or 'unknown'}",
            f"Checkpoints: {result.get('checkpoint_count', 0)}",
            f"Returned: {result.get('returned_count', 0)}",
            f"Order: {str(result.get('order') or 'newest_first').replace('_', ' ')}",
            "Rollback action: none; this command is inspection-only.",
        ]
    )
    if result.get("limit") is not None:
        lines.append(f"Limit: {result.get('limit')}")

    checkpoints = result.get("checkpoints")
    if isinstance(checkpoints, Sequence) and not isinstance(checkpoints, (str, bytes)) and checkpoints:
        lines.extend(["", "Checkpoints:"])
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, Mapping):
                continue
            details = [
                str(checkpoint.get("checkpoint_id") or "unknown"),
                str(checkpoint.get("created_at") or "created_at_unknown"),
                str(checkpoint.get("reason") or "reason_unknown"),
            ]
            if checkpoint.get("task_id"):
                details.append(f"task {checkpoint.get('task_id')}")
            if checkpoint.get("run_id"):
                details.append(f"run {checkpoint.get('run_id')}")
            if checkpoint.get("commit"):
                details.append(f"commit {checkpoint.get('commit')}")
            lines.append(f"  - {' | '.join(details)}")
    else:
        lines.append("")
        lines.append(str(result.get("message") or "No checkpoints recorded."))

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_bundle_export_text(result: Mapping[str, Any]) -> str:
    bundle = result.get("bundle") if isinstance(result.get("bundle"), Mapping) else {}
    safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
    lines = [
        "LoopPlane Git checkpoint bundle export",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    lines.extend(
        [
            f"Output: {bundle.get('path') or result.get('output') or 'unknown'}",
            f"Format: {bundle.get('format') or 'git_bundle'}",
            f"Refs namespace: {bundle.get('refs_namespace') or 'unknown'}",
            f"Exported refs: {bundle.get('ref_count', 0)}",
            f"Managed refs only: {_yes_no(bool(bundle.get('managed_refs_only')))}",
            f"Active branch unchanged: {_yes_no(bool(safety.get('active_branch_unchanged')))}",
            f"HEAD unchanged: {_yes_no(bool(safety.get('head_unchanged')))}",
            f"User index unchanged: {_yes_no(bool(safety.get('user_index_unchanged')))}",
            f"Remote operations: {_yes_no(bool(safety.get('remote_operations_performed')))}",
            f"User history rewritten: {_yes_no(bool(safety.get('history_rewritten')))}",
        ]
    )
    refs = bundle.get("refs")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and refs:
        lines.append("Refs:")
        lines.extend(f"  - {ref}" for ref in refs)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_bundle_import_text(result: Mapping[str, Any]) -> str:
    bundle = result.get("bundle") if isinstance(result.get("bundle"), Mapping) else {}
    import_result = result.get("import") if isinstance(result.get("import"), Mapping) else {}
    safety = result.get("safety") if isinstance(result.get("safety"), Mapping) else {}
    lines = [
        "LoopPlane Git checkpoint bundle import",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    lines.extend(
        [
            f"Input: {bundle.get('path') or result.get('input') or 'unknown'}",
            f"Format: {bundle.get('format') or 'git_bundle'}",
            f"Refs namespace: {bundle.get('refs_namespace') or 'unknown'}",
            f"Bundle heads: {bundle.get('head_count', 0)}",
            f"Importable refs: {bundle.get('importable_ref_count', 0)}",
            f"Ignored refs: {bundle.get('ignored_ref_count', 0)}",
            f"Imported refs: {import_result.get('imported_count', 0)}",
            f"Imported refs managed only: {_yes_no(bool(import_result.get('managed_refs_only')))}",
            f"Active branch unchanged: {_yes_no(bool(safety.get('active_branch_unchanged')))}",
            f"HEAD unchanged: {_yes_no(bool(safety.get('head_unchanged')))}",
            f"User index unchanged: {_yes_no(bool(safety.get('user_index_unchanged')))}",
            f"Remote operations: {_yes_no(bool(safety.get('remote_operations_performed')))}",
            f"User history rewritten: {_yes_no(bool(safety.get('history_rewritten')))}",
        ]
    )
    refs = import_result.get("refs")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and refs:
        lines.append("Refs:")
        for ref_record in refs:
            if not isinstance(ref_record, Mapping):
                continue
            lines.append(
                "  - "
                + " | ".join(
                    str(value)
                    for value in (
                        ref_record.get("ref") or "unknown",
                        ref_record.get("action") or "unknown",
                        ref_record.get("commit") or "unknown",
                    )
                )
            )
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_rollback_text(result: Mapping[str, Any]) -> str:
    checkpoint = result.get("target_checkpoint") if isinstance(result.get("target_checkpoint"), Mapping) else {}
    risk = result.get("risk_summary") if isinstance(result.get("risk_summary"), Mapping) else {}
    rollback_request = result.get("rollback_request") if isinstance(result.get("rollback_request"), Mapping) else {}
    approval_request = result.get("approval_request") if isinstance(result.get("approval_request"), Mapping) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), Mapping) else {}
    sources = result.get("sources") if isinstance(result.get("sources"), Mapping) else {}
    lines = [
        "LoopPlane rollback request",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    lines.extend(
        [
            f"Target checkpoint: {checkpoint.get('checkpoint_id') or result.get('checkpoint_id') or 'unknown'}",
            f"Checkpoint reason: {checkpoint.get('reason') or 'unknown'}",
            f"Approval required: {_yes_no(bool(result.get('approval_required', True)))}",
            f"Execution performed: {_yes_no(bool(execution.get('performed')))}",
            f"Execution reason: {execution.get('reason') or 'approval_required'}",
        ]
    )
    if rollback_request.get("rollback_request_id"):
        lines.append(f"Rollback request: {rollback_request.get('rollback_request_id')}")
    if approval_request.get("approval_id"):
        lines.append(f"Approval id: {approval_request.get('approval_id')}")

    affected_paths = result.get("affected_paths")
    lines.append(f"Affected paths: {len(affected_paths) if isinstance(affected_paths, Sequence) and not isinstance(affected_paths, (str, bytes)) else 0}")
    if isinstance(affected_paths, Sequence) and not isinstance(affected_paths, (str, bytes)) and affected_paths:
        for entry in affected_paths:
            if not isinstance(entry, Mapping):
                continue
            details = str(entry.get("change_type") or "changed")
            if entry.get("old_path"):
                details = f"{details}, from {entry.get('old_path')}"
            lines.append(f"  - {entry.get('path')} ({details})")

    lines.extend(
        [
            "",
            "Risk summary:",
            f"  dirty worktree: {_yes_no(bool(risk.get('dirty_worktree')))}",
            f"  dirty files: {risk.get('dirty_files_count', 0)}",
            f"  untracked files: {risk.get('untracked_files_count', 0)}",
            f"  affected paths: {risk.get('affected_paths_count', 0)}",
            f"  history rewrite before approval: {_yes_no(bool(risk.get('history_rewrite_before_approval')))}",
            f"  user worktree mutation before approval: {_yes_no(bool(risk.get('worktree_mutation_before_approval')))}",
            f"  user branch preserved before approval: {_yes_no(bool(risk.get('user_branch_preserved_before_approval')))}",
            f"  user index preserved before approval: {_yes_no(bool(risk.get('user_index_preserved_before_approval')))}",
        ]
    )
    notes = risk.get("notes")
    if isinstance(notes, Sequence) and not isinstance(notes, (str, bytes)) and notes:
        lines.append("  notes:")
        lines.extend(f"    - {note}" for note in notes)

    lines.extend(
        [
            "",
            "Records:",
            f"  checkpoint log: {sources.get('checkpoint_log') or 'unknown'}",
            f"  version-control read model: {sources.get('version_control_status') or 'unknown'}",
            f"  rollback requests: {sources.get('rollback_requests') or 'unknown'}",
            f"  approval requests: {sources.get('approval_requests') or 'unknown'}",
        ]
    )

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_task_diff_text(result: Mapping[str, Any]) -> str:
    diff = result.get("diff") if isinstance(result.get("diff"), Mapping) else {}
    patch = diff.get("patch") if isinstance(diff.get("patch"), Mapping) else {}
    checkpoints = result.get("checkpoints") if isinstance(result.get("checkpoints"), Mapping) else {}
    before_checkpoint = checkpoints.get("before") if isinstance(checkpoints, Mapping) else None
    after_checkpoint = checkpoints.get("after") if isinstance(checkpoints, Mapping) else None
    lines = [
        "LoopPlane task diff",
        f"Status: {result.get('status', 'unknown')}",
        f"Project: {result.get('project_root')}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result.get('workflow_id')}")
    lines.append(f"Task: {result.get('task_id') or 'unknown'}")
    if result.get("run_id"):
        lines.append(f"Run: {result.get('run_id')}")
    if result.get("run_dir"):
        lines.append(f"Run directory: {result.get('run_dir')}")
    if result.get("message"):
        lines.append(f"Message: {result.get('message')}")

    if diff.get("available"):
        lines.append("Diff metadata: available")
        lines.append(f"Changed files: {diff.get('changed_files_count', 0)}")
        changed_files = diff.get("changed_files")
        if isinstance(changed_files, Sequence) and not isinstance(changed_files, (str, bytes)) and changed_files:
            for entry in changed_files:
                if not isinstance(entry, Mapping):
                    continue
                stats = _format_file_stats(entry)
                lines.append(f"  - {entry.get('path')} ({entry.get('change_type', 'changed')}{stats})")
        if patch.get("available"):
            size = patch.get("size_bytes")
            size_label = f", {size} bytes" if isinstance(size, int) else ""
            lines.append(f"Patch artifact: {patch.get('path')}{size_label}")
        else:
            lines.append("Patch artifact: unavailable")
        lines.append(f"Before-run checkpoint: {_checkpoint_label(before_checkpoint)}")
        lines.append(f"After-run checkpoint: {_checkpoint_label(after_checkpoint)}")
    else:
        lines.append("Diff metadata: unavailable")
        considered = result.get("runs_considered")
        if isinstance(considered, Sequence) and not isinstance(considered, (str, bytes)) and considered:
            lines.append("Runs considered:")
            for candidate in considered:
                if not isinstance(candidate, Mapping):
                    continue
                lines.append(
                    f"  - {candidate.get('run_id') or 'unknown'} "
                    f"({candidate.get('source') or 'unknown'}): {candidate.get('run_dir')}"
                )
        else:
            lines.append("Runs considered: none")

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def format_run_metadata_text(result: dict[str, Any]) -> str:
    lines = [
        "LoopPlane run Git metadata",
        f"Status: {result['status']}",
        f"Project: {result['project_root']}",
        f"Run directory: {result['run_dir']}",
        f"Stage: {result['stage']}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result['workflow_id']}")
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("checkpoint"):
            checkpoint = metadata["checkpoint"]
            lines.append(f"Before-run checkpoint: {checkpoint.get('ref')}")
        if "changed_files_count" in metadata:
            lines.append(f"Changed files: {metadata['changed_files_count']}")
    if result["warnings"]:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    if result["errors"]:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in result["errors"])
    return "\n".join(lines) + "\n"


def format_checkpoint_text(result: dict[str, Any]) -> str:
    lines = [
        "LoopPlane Git checkpoint",
        f"Status: {result['status']}",
        f"Project: {result['project_root']}",
    ]
    if result.get("workflow_id"):
        lines.append(f"Workflow: {result['workflow_id']}")
    checkpoint = result.get("checkpoint")
    if isinstance(checkpoint, dict):
        lines.extend(
            [
                f"Checkpoint: {checkpoint['checkpoint_id']}",
                f"Reason: {checkpoint['reason']}",
                f"Ref: {checkpoint['ref']}",
                f"Commit: {checkpoint['commit']}",
                f"Active branch unchanged: {_yes_no(checkpoint['active_branch_unchanged'])}",
                f"HEAD unchanged: {_yes_no(checkpoint['head_unchanged'])}",
                f"User index unchanged: {_yes_no(checkpoint['user_index_unchanged'])}",
            ]
        )
    if result["warnings"]:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    if result["errors"]:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in result["errors"])
    return "\n".join(lines) + "\n"


def initialize_local_repository_if_missing(
    project_root: Path,
    *,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()

    git = _detect_git(git_runner)
    if not git["available"]:
        return _git_init_status(
            "waiting_config",
            git,
            _empty_repository_status(),
            reason="git_unavailable",
            message="Git is unavailable; LoopPlane cannot create default local checkpoints.",
        )

    repository = _detect_repository(project, git_runner, git["available"])
    if repository["inside_work_tree"]:
        return _git_init_status("ok", git, repository, reason="existing_repository")

    local_init = _detect_local_init_capability(project, git["available"], repository["inside_work_tree"])
    if not local_init["possible"]:
        return _git_init_status(
            "waiting_config",
            git,
            repository,
            reason=local_init["reason"],
            message=f"Local git init does not appear possible: {local_init['reason']}.",
        )

    init = git_runner.run(project, ("init",))
    if init.returncode != 0:
        return _git_init_status(
            "waiting_config",
            git,
            repository,
            reason="git_init_failed",
            message=f"git init failed: {_compact_command_error(init)}",
        )

    repository = _detect_repository(project, git_runner, git["available"])
    if not repository["inside_work_tree"]:
        return _git_init_status(
            "waiting_config",
            git,
            repository,
            reason="git_init_unverified",
            message="git init completed but the project is still not detected as a Git work tree.",
        )

    return _git_init_status("ok", git, repository, reason="local_repository_ready")


def plan_local_repository_initialization(
    project_root: Path,
    *,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()

    git = _detect_git(git_runner)
    if not git["available"]:
        return _git_init_status(
            "waiting_config",
            git,
            _empty_repository_status(),
            reason="git_unavailable",
            message="Git is unavailable; LoopPlane cannot create default local checkpoints.",
        )

    repository = _detect_repository(project, git_runner, git["available"])
    if repository["inside_work_tree"]:
        return _git_init_status("ok", git, repository, reason="existing_repository")

    local_init = _detect_local_init_capability(project, git["available"], repository["inside_work_tree"])
    if not local_init["possible"]:
        return _git_init_status(
            "waiting_config",
            git,
            repository,
            reason=local_init["reason"],
            message=f"Local git init does not appear possible: {local_init['reason']}.",
        )

    planned_repository = dict(repository)
    planned_repository["inside_work_tree"] = True
    planned_repository["root"] = str(project)
    return _git_init_status("ok", git, planned_repository, reason="local_repository_ready")


def run_git_doctor(
    project_root: Path,
    *,
    runner: GitCommandRunner | None = None,
) -> dict[str, Any]:
    project = project_root.expanduser().resolve()
    git_runner = runner or SubprocessGitCommandRunner()
    warnings: list[str] = []
    errors: list[str] = []

    workflow = _load_workflow(project, warnings, errors)
    paths = workflow["paths"]
    config_path = paths.version_control_config_file
    config = _load_version_control_config(config_path, errors)

    git = _detect_git(git_runner)
    if not git["available"]:
        errors.append("Git is unavailable; LoopPlane cannot create default local checkpoints.")

    repository = _detect_repository(project, git_runner, git["available"])
    local_init = _detect_local_init_capability(project, git["available"], repository["inside_work_tree"])
    configuration = _configuration_summary(config_path, config, workflow["workflow_id"])

    warnings.extend(_configuration_warnings(config))
    errors.extend(_configuration_errors(config))

    if config and bool(config.get("enabled", True)) and git["available"] and not repository["inside_work_tree"]:
        if bool(config.get("auto_init_if_missing", False)):
            if not local_init["possible"]:
                errors.append(f"Local git init does not appear possible: {local_init['reason']}.")
        else:
            warnings.append("Project is not in a Git work tree and auto_init_if_missing is disabled.")

    status = "waiting_config" if errors else ("warning" if warnings else "ok")
    checkpoint_model = {
        "backend": configuration.get("checkpoint_backend"),
        "branch_cleanliness_required": False,
        "note": (
            "LoopPlane uses managed refs for checkpoints; this intentionally does not create commits on "
            "or clean the user's current branch."
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "ok": not errors,
        "project_root": str(project),
        "workflow_id": workflow["workflow_id"],
        "git": git,
        "repository": repository,
        "local_init": local_init,
        "configuration": configuration,
        "checkpoint_model": checkpoint_model,
        "warnings": warnings,
        "errors": errors,
    }


def format_doctor_text(result: dict[str, Any]) -> str:
    git = result["git"]
    repository = result["repository"]
    local_init = result["local_init"]
    config = result["configuration"]
    lines = [
        "LoopPlane version control doctor",
        f"Project: {result['project_root']}",
        f"Status: {result['status']}",
        "",
        "Git:",
        f"  available: {_yes_no(git['available'])}",
    ]
    if git.get("executable"):
        lines.append(f"  executable: {git['executable']}")
    if git.get("version"):
        lines.append(f"  version: {git['version']}")

    lines.extend(
        [
            "",
            "Repository:",
            f"  detected: {_yes_no(repository['inside_work_tree'])}",
        ]
    )
    if repository.get("root"):
        lines.append(f"  root: {repository['root']}")
    if repository.get("head_commit"):
        lines.append(f"  head_commit: {repository['head_commit']}")
    elif repository["inside_work_tree"]:
        lines.append("  head_commit: unavailable")
    lines.append(f"  dirty: {_yes_no(repository['dirty'])}")
    lines.append(f"  dirty_files_count: {repository['dirty_files_count']}")
    checkpoint_model = result.get("checkpoint_model") if isinstance(result.get("checkpoint_model"), Mapping) else {}
    if checkpoint_model.get("note"):
        lines.append(f"  checkpoint_model: {checkpoint_model.get('note')}")

    lines.extend(
        [
            "",
            "Local Init:",
            f"  checked: {_yes_no(local_init['checked'])}",
            f"  possible: {_yes_no(local_init['possible'])}",
            f"  reason: {local_init['reason']}",
            "",
            "Configuration:",
            f"  config_found: {_yes_no(config['config_found'])}",
            f"  config_path: {config['config_path']}",
            f"  enabled: {config.get('enabled')}",
            f"  provider: {config.get('provider')}",
            f"  repository_mode: {config.get('repository_mode')}",
            f"  checkpoint_backend: {config.get('checkpoint_backend')}",
            f"  refs_namespace: {config.get('refs_namespace')}",
            f"  resolved_refs_namespace: {config.get('resolved_refs_namespace')}",
            f"  worker_checkpointing_effective: "
            f"{config.get('effective_worker_checkpointing', {}).get('enabled')}",
            f"  worker_checkpointing_reason: "
            f"{config.get('effective_worker_checkpointing', {}).get('reason')}",
            "",
            "Safety:",
            f"  no_remote_push: {config.get('no_remote_push')}",
            f"  do_not_switch_user_branch: {config.get('do_not_switch_user_branch')}",
            f"  do_not_modify_user_index: {config.get('do_not_modify_user_index')}",
            f"  commit_policy.write_to_user_branch: {config.get('commit_policy', {}).get('write_to_user_branch')}",
            f"  commit_policy.require_approval_for_user_branch_commit: "
            f"{config.get('commit_policy', {}).get('require_approval_for_user_branch_commit')}",
        ]
    )

    if result["warnings"]:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    if result["errors"]:
        lines.extend(["", "Errors:"])
        lines.extend(f"  - {error}" for error in result["errors"])
    return "\n".join(lines) + "\n"


def doctor_exit_code(result: dict[str, Any]) -> int:
    return _version_control_exit_code(result)


def checkpoint_exit_code(result: dict[str, Any]) -> int:
    return _version_control_exit_code(result)


def status_exit_code(result: Mapping[str, Any]) -> int:
    return _version_control_exit_code(result)


def diff_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    return EXIT_INVALID_CONFIG


def checkpoint_log_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if result.get("status") in {"invalid_metadata", "invalid_request"}:
        return EXIT_INVALID_CONFIG
    return _version_control_exit_code(result)


def rollback_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("status") == "approval_required":
        return EXIT_WAITING_APPROVAL
    if result.get("status") in {"checkpoint_not_found", "no_checkpoints", "invalid_metadata", "invalid_request"}:
        return EXIT_INVALID_CONFIG
    if result.get("status") == "waiting_config" and has_text(
        result,
        (
            "workflow.json",
            "version-control config",
            "invalid workflow path",
        ),
        "errors",
        "warnings",
        "message",
    ):
        return EXIT_INVALID_CONFIG
    return _version_control_exit_code(result)


def bundle_export_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if result.get("status") in {"invalid_request", "no_checkpoints"}:
        return EXIT_INVALID_CONFIG
    return _version_control_exit_code(result)


def bundle_import_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    if result.get("status") in {"invalid_bundle", "invalid_request", "no_checkpoints"}:
        return EXIT_INVALID_CONFIG
    return _version_control_exit_code(result)


def run_metadata_exit_code(result: dict[str, Any]) -> int:
    return _version_control_exit_code(result)


def _version_control_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok"):
        return EXIT_SUCCESS
    git = result.get("git")
    if isinstance(git, Mapping) and git.get("available") is False:
        return EXIT_VERSION_CONTROL_UNAVAILABLE
    if has_text(
        result,
        (
            "git is unavailable",
            "version control is unavailable",
            "version control is disabled",
            "git repository is unavailable",
            "local git init",
        ),
        "errors",
        "warnings",
        "message",
    ):
        return EXIT_VERSION_CONTROL_UNAVAILABLE
    if has_text(
        result,
        (
            "workflow.json",
            "version-control config",
            "invalid json",
            "invalid workflow path",
            "unsupported version-control",
            "unsafe config",
        ),
        "errors",
        "warnings",
        "message",
        ):
        return EXIT_INVALID_CONFIG
    return EXIT_VERSION_CONTROL_UNAVAILABLE


def _task_diff_result(
    *,
    project: Path,
    workflow_id: str | None,
    generated_at: str,
    task_id: str,
    status: str,
    ok: bool,
    warnings: Sequence[str],
    errors: Sequence[str],
    message: str | None = None,
    run_id: str | None = None,
    run_dir: Path | None = None,
    source: str | None = None,
    diff: Mapping[str, Any] | None = None,
    checkpoints: Mapping[str, Any] | None = None,
    artifacts: Sequence[str] | None = None,
    runs_considered: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": ok,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_dir": _path_for_project_record(project, run_dir) if run_dir is not None else None,
        "source": source,
        "message": message,
        "diff": dict(diff) if diff is not None else {"available": False},
        "checkpoints": dict(checkpoints) if checkpoints is not None else {"before": None, "after": None},
        "artifacts": list(artifacts or []),
        "runs_considered": [dict(candidate) for candidate in runs_considered or []],
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def _plan_task_metadata(plan_path: Path) -> dict[str, dict[str, Any]] | None:
    try:
        lines = plan_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    tasks: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(lines):
        match = TASK_LINE_RE.match(lines[index])
        if not match:
            index += 1
            continue
        task_id = match.group("task_id")
        start = index
        index += 1
        while index < len(lines):
            if TASK_LINE_RE.match(lines[index]) or lines[index].startswith("## Phase "):
                break
            index += 1
        tasks[task_id] = {"task_id": task_id, "fields": _plan_task_fields(lines[start:index])}
    return tasks


def _plan_task_fields(lines: Sequence[str]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    current_field: str | None = None
    for line in lines[1:]:
        match = FIELD_LINE_RE.match(line)
        if match:
            current_field = match.group("field").strip().lower().replace(" ", "_")
            fields.setdefault(current_field, []).append(match.group("value").strip())
            continue
        if current_field and line.startswith("    "):
            fields[current_field].append(line.strip())
    return {key: tuple(values) for key, values in fields.items()}


def _task_diff_candidates(
    project: Path,
    paths: WorkflowPaths,
    task_id: str,
    task: Mapping[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def add_candidate(run_dir: Path | None, *, source: str, run_id: str | None = None) -> None:
        if run_dir is None:
            return
        resolved = run_dir.expanduser()
        if not resolved.is_absolute():
            resolved = project / resolved
        try:
            resolved = resolved.resolve()
        except OSError:
            resolved = resolved.absolute()
        if not _is_relative_to(resolved, project):
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append({"run_dir": resolved, "run_id": run_id or resolved.name, "source": source})

    latest_path = _task_latest_path(project, paths, task_id, task)
    latest = _load_json_mapping(latest_path)
    if isinstance(latest, Mapping):
        latest_run_dir = latest.get("latest_run_dir")
        if isinstance(latest_run_dir, str) and latest_run_dir.strip():
            add_candidate(Path(latest_run_dir), source="latest_json", run_id=_string_or_none(latest.get("latest_run_id")))

    for summary in _read_jsonl_records(paths.read_models_dir / "run_summaries.jsonl"):
        if str(summary.get("task_id") or "") != task_id:
            continue
        run_id = _string_or_none(summary.get("run_id"))
        if run_id:
            add_candidate(paths.results_dir / task_id / "runs" / run_id, source="run_summaries", run_id=run_id)

    runs_dir = paths.results_dir / task_id / "runs"
    if runs_dir.exists():
        for run_dir in sorted((path for path in runs_dir.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True):
            add_candidate(run_dir, source="runs_directory", run_id=run_dir.name)
    return candidates


def _task_latest_path(project: Path, paths: WorkflowPaths, task_id: str, task: Mapping[str, Any]) -> Path:
    fields = task.get("fields")
    latest = None
    if isinstance(fields, Mapping):
        values = fields.get("latest") or fields.get("latest_pointer_path")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            latest = values[0]
    latest_text = str(latest or f"{paths.value('results_dir').rstrip('/')}/{task_id}/latest.json")
    path = Path(latest_text)
    return path if path.is_absolute() else project / path


def _candidate_records(project: Path, candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": candidate.get("run_id"),
            "source": candidate.get("source"),
            "run_dir": _path_for_project_record(project, candidate.get("run_dir")),
            "diff_metadata_found": bool((candidate.get("run_dir") / "git" / "changed_files.json").is_file())
            if isinstance(candidate.get("run_dir"), Path)
            else False,
        }
        for candidate in candidates
    ]


def _sanitize_changed_files(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], ["changed_files.json has no changed_files list."]
    changed_files: list[dict[str, Any]] = []
    for raw_entry in value:
        if not isinstance(raw_entry, Mapping):
            warnings.append("Ignoring malformed changed-file entry.")
            continue
        path = _safe_project_path(raw_entry.get("path"))
        if path is None:
            warnings.append("Ignoring unsafe changed-file path in diff metadata.")
            continue
        entry: dict[str, Any] = {
            "path": path,
            "change_type": str(raw_entry.get("change_type") or "modified"),
        }
        old_path = _safe_project_path(raw_entry.get("old_path"))
        if old_path is not None:
            entry["old_path"] = old_path
        for key in ("lines_added", "lines_deleted", "line_delta"):
            parsed = _coerce_optional_int(raw_entry.get(key))
            if parsed is not None:
                entry[key] = parsed
        changed_files.append(entry)
    return changed_files, warnings


def _safe_project_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if not path or path.startswith("/") or path == ".git" or path.startswith(".git/") or "/.git/" in path:
        return None
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return None
    return path


def _coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _changed_files_summary(changed_files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lines_added = 0
    lines_deleted = 0
    by_change_type: dict[str, int] = {}
    for entry in changed_files:
        change_type = str(entry.get("change_type") or "modified")
        by_change_type[change_type] = by_change_type.get(change_type, 0) + 1
        lines_added += int(entry.get("lines_added") or 0)
        lines_deleted += int(entry.get("lines_deleted") or 0)
    return {
        "files_changed": len(changed_files),
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "line_delta": lines_added - lines_deleted,
        "by_change_type": by_change_type,
    }


def _patch_artifact(project: Path, patch_path: Path) -> dict[str, Any]:
    if not patch_path.is_file():
        return {"available": False, "path": _path_for_project_record(project, patch_path)}
    try:
        data = patch_path.read_bytes()
    except OSError:
        return {"available": False, "path": _path_for_project_record(project, patch_path)}
    return {
        "available": True,
        "path": _path_for_project_record(project, patch_path),
        "size_bytes": len(data),
        "sha256": f"sha256:{_sha256_bytes(data)}",
    }


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _task_run_checkpoints(path: Path, *, task_id: str, run_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in _read_jsonl_records(path):
        if str(record.get("task_id") or "") != task_id or str(record.get("run_id") or "") != run_id:
            continue
        sanitized = _sanitize_checkpoint_record(record)
        sanitized["status"] = record.get("status")
        records.append(sanitized)
    return records


def _checkpoint_log_records(path: Path) -> tuple[list[dict[str, Any]], list[str], bool]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], [], True

    records: list[dict[str, Any]] = []
    problems: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw_record = json.loads(line)
        except JSONDecodeError:
            problems.append(f"git_checkpoints.jsonl line {line_number} is malformed JSON and was ignored.")
            continue
        if not isinstance(raw_record, Mapping):
            problems.append(f"git_checkpoints.jsonl line {line_number} is not a JSON object and was ignored.")
            continue
        record, record_problems = _sanitize_checkpoint_log_record(raw_record, line_number=line_number)
        problems.extend(record_problems)
        if record is not None:
            records.append(record)
    return records, problems, False


def _sanitize_checkpoint_log_record(
    record: Mapping[str, Any],
    *,
    line_number: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    checkpoint_id = _safe_log_text(record.get("checkpoint_id"))
    if checkpoint_id is None:
        problems.append(f"git_checkpoints.jsonl line {line_number} is missing a safe checkpoint_id and was ignored.")
        return None, problems

    sanitized = {
        "checkpoint_id": checkpoint_id,
        "created_at": _safe_log_text(record.get("created_at")),
        "reason": _safe_log_text(record.get("reason")),
        "task_id": _safe_log_text(record.get("task_id")),
        "run_id": _safe_log_text(record.get("run_id")),
        "status": _safe_log_text(record.get("status")) or "created",
        "provider": _safe_log_text(record.get("provider")) or SUPPORTED_PROVIDER,
        "backend": _safe_log_text(record.get("backend")) or _safe_log_text(record.get("checkpoint_backend")),
        "commit": _safe_log_text(record.get("commit")) or _safe_log_text(record.get("commit_sha")),
        "source_line": line_number,
    }
    if record.get("ref") and _safe_log_text(record.get("ref")) is None:
        problems.append(f"git_checkpoints.jsonl line {line_number} contains an unsafe ref field that was omitted.")
    if record.get("repository_root") and _safe_log_text(record.get("repository_root")) is None:
        problems.append(f"git_checkpoints.jsonl line {line_number} contains an unsafe repository_root field that was omitted.")
    return sanitized, problems


def _safe_log_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    if normalized == ".git" or normalized.startswith(".git/") or normalized.endswith("/.git") or "/.git/" in normalized:
        return None
    return text


def _read_model_git_available(read_model: Mapping[str, Any] | None) -> bool | None:
    if not isinstance(read_model, Mapping):
        return None
    if "git_available" in read_model:
        return bool(read_model.get("git_available"))
    git = read_model.get("git")
    if isinstance(git, Mapping) and "available" in git:
        return bool(git.get("available"))
    return None


def _read_model_problem(read_model: Mapping[str, Any] | None) -> str | None:
    if not isinstance(read_model, Mapping):
        return None
    status = _safe_log_text(read_model.get("status"))
    problem = read_model.get("problem")
    if isinstance(problem, Mapping):
        message = _safe_log_text(problem.get("message"))
        if message:
            return message
        reason = _safe_log_text(problem.get("reason"))
        if reason:
            return f"Version-control read model reports a problem: {reason}."
        return "Version-control read model reports a problem."
    if status in {"waiting_config", "unavailable"}:
        return f"version_control_status.json status is {status}."
    return None


def _checkpoint_log_status(
    *,
    errors: Sequence[str],
    invalid_request: bool,
    metadata_invalid: bool,
    enabled: bool,
    checkpoint_count: int,
) -> str:
    if invalid_request:
        return "invalid_request"
    if errors:
        return "waiting_config"
    if metadata_invalid:
        return "invalid_metadata"
    if not enabled:
        return "disabled"
    if checkpoint_count == 0:
        return "empty"
    return "ok"


def _rollback_policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = config.get("rollback_policy") if isinstance(config, Mapping) else None
    if not isinstance(policy, Mapping):
        policy = {}
    configured_requires_approval = bool(policy.get("rollback_requires_approval", False))
    return {
        "allow_rollback": bool(policy.get("allow_rollback", True)),
        "configured_requires_approval": configured_requires_approval,
        "requires_approval": configured_requires_approval,
        "never_auto_rollback_user_changes": bool(policy.get("never_auto_rollback_user_changes", False)),
    }


def _rollback_request_status(
    *,
    errors: Sequence[str],
    record_problems: Sequence[str],
    enabled: bool,
    checkpoint_count: int,
) -> str:
    if record_problems:
        return "invalid_metadata"
    if not enabled:
        return "disabled"
    if errors:
        if any("checkpoint id is required" in error for error in errors):
            return "invalid_request"
        checkpoint_errors = [
            error
            for error in errors
            if "No checkpoints are recorded" in error or "was not found" in error
        ]
        if len(checkpoint_errors) != len(errors):
            return "waiting_config"
        if any("No checkpoints are recorded" in error for error in checkpoint_errors) or checkpoint_count == 0:
            return "no_checkpoints"
        if any("was not found" in error for error in checkpoint_errors):
            return "checkpoint_not_found"
        return "waiting_config"
    return "ready"


def _rollback_sources(
    project: Path,
    paths: WorkflowPaths,
    *,
    checkpoint_log_loaded: bool,
    read_model_loaded: bool,
) -> dict[str, Any]:
    return {
        "checkpoint_log": _path_for_project_record(project, paths.runtime_dir / "git_checkpoints.jsonl"),
        "checkpoint_log_loaded": checkpoint_log_loaded,
        "version_control_status": _path_for_project_record(
            project,
            paths.read_models_dir / "version_control_status.json",
        ),
        "read_model_loaded": read_model_loaded,
        "rollback_requests": _path_for_project_record(project, paths.requests_dir / ROLLBACK_REQUESTS_FILENAME),
        "approval_requests": _path_for_project_record(project, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME),
        "approval_responses": _path_for_project_record(project, paths.runtime_dir / APPROVAL_RESPONSES_FILENAME),
        "managed_checkpoint_metadata": True,
        "direct_git_reads": False,
        "mutation_performed": False,
    }


def _rollback_result(
    *,
    project: Path,
    workflow_id: str | None,
    generated_at: str,
    status: str,
    ok: bool,
    checkpoint_id: str | None,
    target_checkpoint: Mapping[str, Any] | None,
    affected_paths: Sequence[Mapping[str, Any]],
    risk_summary: Mapping[str, Any],
    rollback_request: Mapping[str, Any] | None,
    approval_request: Mapping[str, Any] | None,
    approval_policy: Mapping[str, Any] | None,
    approval_required: bool,
    execution: Mapping[str, Any] | None,
    sources: Mapping[str, Any],
    warnings: Sequence[str],
    errors: Sequence[str],
) -> dict[str, Any]:
    execution_record = dict(execution) if isinstance(execution, Mapping) else {
        "performed": False,
        "reason": "approval_required" if approval_required and ok else status,
        "worktree_mutated": False,
        "history_rewritten": False,
        "user_branch_preserved": True,
        "user_index_preserved": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": ok,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "checkpoint_id": checkpoint_id,
        "target_checkpoint": dict(target_checkpoint) if isinstance(target_checkpoint, Mapping) else None,
        "affected_paths": [dict(path) for path in affected_paths],
        "risk_summary": dict(risk_summary),
        "approval_required": bool(approval_required),
        "approval_policy": dict(approval_policy) if isinstance(approval_policy, Mapping) else None,
        "rollback_request": dict(rollback_request) if isinstance(rollback_request, Mapping) else None,
        "approval_request": dict(approval_request) if isinstance(approval_request, Mapping) else None,
        "execution": execution_record,
        "sources": dict(sources),
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def _bundle_export_result(
    *,
    project: Path,
    workflow_id: str | None,
    generated_at: str,
    status: str,
    ok: bool,
    output: Path,
    git: Mapping[str, Any],
    repository: Mapping[str, Any],
    refs_namespace: str | None,
    refs: Sequence[str],
    checkpoint_records: Sequence[Mapping[str, Any]],
    checkpoint_log_path: Path,
    bundle_heads: Sequence[Mapping[str, Any]],
    active_branch_before: str | None,
    active_branch_after: str | None,
    head_before: str | None,
    head_after: str | None,
    index_unchanged: bool,
    warnings: Sequence[str],
    errors: Sequence[str],
) -> dict[str, Any]:
    managed_heads_only = all(
        _is_managed_checkpoint_ref(str(head.get("ref") or ""), refs_namespace)
        for head in bundle_heads
    )
    active_branch_unchanged = active_branch_before == active_branch_after
    head_unchanged = head_before == head_after
    return {
        "schema_version": GIT_REF_BUNDLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": ok,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "output": output.as_posix(),
        "git": {
            "available": bool(git.get("available")),
        },
        "repository": {
            "detected": bool(repository.get("inside_work_tree")),
            "inside_work_tree": bool(repository.get("inside_work_tree")),
            "root": _repository_root_for_record(project, repository),
            "head_commit": repository.get("head_commit"),
        },
        "bundle": {
            "path": output.as_posix(),
            "format": "git_bundle",
            "refs_namespace": refs_namespace,
            "ref_count": len(refs),
            "refs": list(refs),
            "head_count": len(bundle_heads),
            "heads": [dict(head) for head in bundle_heads],
            "managed_refs_only": managed_heads_only,
        },
        "checkpoint_log": {
            "path": _path_for_project_record(project, checkpoint_log_path),
            "record_count": len(checkpoint_records),
        },
        "safety": {
            "no_remote_push": True,
            "no_remote_fetch": True,
            "remote_operations_performed": False,
            "active_branch_before": active_branch_before,
            "active_branch_after": active_branch_after,
            "active_branch_unchanged": active_branch_unchanged,
            "head_before": head_before,
            "head_after": head_after,
            "head_unchanged": head_unchanged,
            "user_index_unchanged": index_unchanged,
            "branch_switch_performed": False,
            "history_rewritten": False,
            "user_history_modified": False,
            "user_branch_modified": False,
        },
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def _bundle_import_result(
    *,
    project: Path,
    workflow_id: str | None,
    generated_at: str,
    status: str,
    ok: bool,
    bundle: Path,
    git: Mapping[str, Any],
    repository: Mapping[str, Any],
    refs_namespace: str | None,
    bundle_heads: Sequence[Mapping[str, Any]],
    managed_heads: Sequence[Mapping[str, Any]],
    ignored_heads: Sequence[Mapping[str, Any]],
    imported_refs: Sequence[Mapping[str, Any]],
    checkpoint_records: Sequence[Mapping[str, Any]],
    checkpoint_log_path: Path,
    init_status: Mapping[str, Any] | None,
    active_branch_before: str | None,
    active_branch_after: str | None,
    head_before: str | None,
    head_after: str | None,
    index_unchanged: bool,
    warnings: Sequence[str],
    errors: Sequence[str],
) -> dict[str, Any]:
    imported_refs_managed_only = all(
        _is_managed_checkpoint_ref(str(record.get("ref") or ""), refs_namespace)
        for record in imported_refs
    )
    active_branch_unchanged = active_branch_before == active_branch_after
    head_unchanged = head_before == head_after
    return {
        "schema_version": GIT_REF_BUNDLE_IMPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ok": ok,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "input": bundle.as_posix(),
        "git": {
            "available": bool(git.get("available")),
        },
        "repository": {
            "detected": bool(repository.get("inside_work_tree")),
            "inside_work_tree": bool(repository.get("inside_work_tree")),
            "root": _repository_root_for_record(project, repository),
            "head_commit": repository.get("head_commit"),
        },
        "bundle": {
            "path": bundle.as_posix(),
            "format": "git_bundle",
            "refs_namespace": refs_namespace,
            "head_count": len(bundle_heads),
            "heads": [dict(head) for head in bundle_heads],
            "importable_ref_count": len(managed_heads),
            "importable_refs": [str(head.get("ref") or "") for head in managed_heads],
            "ignored_ref_count": len(ignored_heads),
            "ignored_refs": [str(head.get("ref") or "") for head in ignored_heads],
            "managed_refs_only": not ignored_heads
            and all(
                _is_managed_checkpoint_ref(str(head.get("ref") or ""), refs_namespace)
                for head in bundle_heads
            ),
        },
        "import": {
            "performed": bool(imported_refs),
            "imported_count": len(imported_refs),
            "refs": [dict(record) for record in imported_refs],
            "managed_refs_only": imported_refs_managed_only,
            "non_managed_refs_updated": False,
        },
        "checkpoint_log": {
            "path": _path_for_project_record(project, checkpoint_log_path),
            "record_count": len(checkpoint_records),
        },
        "local_repository": {
            "initialized_or_detected": bool(init_status and init_status.get("ok")),
            "reason": init_status.get("reason") if isinstance(init_status, Mapping) else None,
        },
        "safety": {
            "no_remote_push": True,
            "no_remote_fetch": True,
            "remote_operations_performed": False,
            "active_branch_before": active_branch_before,
            "active_branch_after": active_branch_after,
            "active_branch_unchanged": active_branch_unchanged,
            "head_before": head_before,
            "head_after": head_after,
            "head_unchanged": head_unchanged,
            "user_index_unchanged": index_unchanged,
            "branch_switch_performed": False,
            "history_rewritten": False,
            "user_history_modified": False,
            "user_branch_modified": False,
        },
        "warnings": _dedupe_text(warnings),
        "errors": _dedupe_text(errors),
    }


def _bundle_export_status(*, errors: Sequence[str], no_refs: bool) -> str:
    if not errors:
        return "exported"
    if no_refs and any("No LoopPlane-managed checkpoint refs" in error for error in errors):
        return "no_checkpoints"
    if any("output" in error.lower() for error in errors):
        return "invalid_request"
    return "waiting_config"


def _bundle_import_status(*, errors: Sequence[str], imported_refs: Sequence[Mapping[str, Any]]) -> str:
    if not errors:
        return "imported"
    if any("No LoopPlane-managed checkpoint refs" in error for error in errors):
        return "no_checkpoints"
    if any("bundle" in error.lower() for error in errors):
        return "invalid_bundle"
    if imported_refs:
        return "partial_import"
    return "waiting_config"


def _validate_bundle_output_path(project: Path, output: Path) -> str | None:
    if ".git" in output.parts:
        return "Bundle output path must not be inside a .git directory."
    if output.exists() and output.is_dir():
        return "Bundle output path points to a directory."
    try:
        if output.resolve() == project.resolve():
            return "Bundle output path must be a file, not the project directory."
    except OSError:
        pass
    return None


def _validate_bundle_input_path(bundle: Path) -> str | None:
    if ".git" in bundle.parts:
        return "Bundle input path must not be inside a .git directory."
    if not bundle.exists():
        return "Bundle input path does not exist."
    if not bundle.is_file():
        return "Bundle input path must be a file."
    return None


def _managed_checkpoint_refs(
    project: Path,
    runner: GitCommandRunner,
    refs_namespace: str,
) -> tuple[list[str], str | None]:
    prefix = refs_namespace.rstrip("/") + "/checkpoints"
    result = _run_git(runner, project, ("for-each-ref", "--format=%(refname)", prefix))
    if result.returncode != 0:
        return [], f"git for-each-ref failed while listing LoopPlane checkpoint refs: {_compact_command_error(result)}"
    refs = sorted(
        ref.strip()
        for ref in result.stdout.splitlines()
        if _is_managed_checkpoint_ref(ref.strip(), refs_namespace)
    )
    return refs, None


def _is_managed_checkpoint_ref(ref: str, refs_namespace: str | None) -> bool:
    if not refs_namespace:
        return False
    prefix = refs_namespace.rstrip("/") + "/checkpoints/"
    return ref.startswith(prefix) and ref.startswith(DEFAULT_REFS_PREFIX) and _safe_log_text(ref) is not None


def _bundle_heads(
    project: Path,
    runner: GitCommandRunner,
    output: Path,
) -> tuple[list[dict[str, str]], list[str]]:
    result = _run_git(runner, project, ("bundle", "list-heads", str(output)))
    if result.returncode != 0:
        return [], [f"git bundle list-heads failed: {_compact_command_error(result)}"]
    heads: list[dict[str, str]] = []
    warnings: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            warnings.append("Ignoring malformed git bundle head entry.")
            continue
        commit, ref = parts
        heads.append({"commit": commit, "ref": ref})
    return sorted(heads, key=lambda item: item["ref"]), warnings


def _rollback_affected_paths(
    project: Path,
    paths: WorkflowPaths,
    runner: GitCommandRunner,
    config: Mapping[str, Any] | None,
    checkpoint: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    commit = _safe_log_text(checkpoint.get("commit"))
    if commit is None:
        return [], ["Checkpoint metadata is missing a safe checkpoint commit."]

    verify = _run_git(runner, project, ("cat-file", "-e", f"{commit}^{{commit}}"))
    if verify.returncode != 0:
        return [], ["Checkpoint commit from metadata is unavailable; affected paths could not be computed."]

    repository = _detect_repository(project, runner, runner.git_path() is not None)
    path_policy = _workspace_path_policy(
        project,
        dict(config or {}),
        paths,
        extra_excludes=_checkpoint_metadata_excludes(paths),
    )
    pathspecs = tuple(str(item) for item in path_policy["pathspecs"])
    diff = _run_git(
        runner,
        project,
        ("diff", "--name-status", "--find-renames", "-z", commit, "--", *pathspecs),
    )
    affected: dict[str, dict[str, Any]] = {}
    if diff.returncode == 0:
        for entry in _parse_name_status_z(diff.stdout):
            _merge_affected_path(affected, entry)
    else:
        warnings.append("Unable to compute checkpoint diff; falling back to dirty worktree status.")

    status = _run_git(runner, project, ("status", "--porcelain=v1", "--untracked-files=all", "-z", "--", *pathspecs))
    if status.returncode == 0:
        for entry in _parse_porcelain_status_z(project, repository, status.stdout):
            path = _safe_project_path(entry.get("path"))
            if path is None:
                warnings.append("Ignoring unsafe dirty path while preparing rollback risk summary.")
                continue
            _merge_affected_path(
                affected,
                {
                    "path": path,
                    "old_path": _safe_project_path(entry.get("old_path")),
                    "change_type": entry.get("change_type") or "modified",
                    "source": "worktree_status",
                },
            )
    else:
        warnings.append("Unable to inspect dirty worktree status while preparing rollback risk summary.")

    return sorted(affected.values(), key=lambda item: str(item.get("path") or "")), warnings


def _parse_name_status_z(output: str) -> list[dict[str, Any]]:
    parts = [part for part in output.split("\0") if part]
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        change_type = _name_status_change_type(status)
        old_path = None
        if status.startswith(("R", "C")) and index + 1 < len(parts):
            old_path = _safe_project_path(parts[index])
            path = _safe_project_path(parts[index + 1])
            index += 2
        elif index < len(parts):
            path = _safe_project_path(parts[index])
            index += 1
        else:
            break
        if path is None:
            continue
        entry: dict[str, Any] = {
            "path": path,
            "change_type": change_type,
            "source": "checkpoint_diff",
        }
        if old_path is not None:
            entry["old_path"] = old_path
        entries.append(entry)
    return entries


def _name_status_change_type(status: str) -> str:
    code = status[:1]
    return {
        "A": "added",
        "C": "copied",
        "D": "deleted",
        "M": "modified",
        "R": "renamed",
        "T": "type_changed",
        "U": "unmerged",
        "X": "unknown",
    }.get(code, "modified")


def _merge_affected_path(affected: dict[str, dict[str, Any]], entry: Mapping[str, Any]) -> None:
    path = _safe_project_path(entry.get("path"))
    if path is None:
        return
    existing = affected.get(path, {})
    merged = dict(existing)
    merged["path"] = path
    merged["change_type"] = entry.get("change_type") or existing.get("change_type") or "modified"
    old_path = _safe_project_path(entry.get("old_path"))
    if old_path is not None:
        merged["old_path"] = old_path
    sources = set()
    for value in (existing.get("sources"),):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sources.update(str(item) for item in value if item)
    if entry.get("source"):
        sources.add(str(entry.get("source")))
    merged["sources"] = sorted(sources)
    affected[path] = merged


def _rollback_risk_summary(
    *,
    dirty: bool,
    dirty_files_count: int,
    affected_paths: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    untracked_count = sum(1 for path in affected_paths if str(path.get("change_type") or "") == "untracked")
    requires_approval = bool(policy.get("requires_approval"))
    notes = []
    if requires_approval:
        notes.extend(
            [
                "Rollback request recorded only; no files, branches, refs, or index entries were changed.",
                "Execution requires explicit approval before any future rollback executor may run.",
            ]
        )
    else:
        notes.append("Rollback executes automatically under unattended full-access policy.")
    if dirty:
        notes.append("Dirty worktree detected; unattended rollback may overwrite files covered by the checkpoint policy.")
    return {
        "risk_level": "high" if dirty else "medium",
        "approval_required": requires_approval,
        "dirty_worktree": dirty,
        "dirty_files_count": max(0, int(dirty_files_count)),
        "untracked_files_count": untracked_count,
        "affected_paths_count": len(affected_paths),
        "history_rewrite_before_approval": False,
        "worktree_mutation_before_approval": False,
        "user_branch_preserved_before_approval": True,
        "user_index_preserved_before_approval": True,
        "never_auto_rollback_user_changes": bool(policy.get("never_auto_rollback_user_changes", False)),
        "notes": notes,
    }


def _effective_rollback_risk_summary(
    risk_summary: Mapping[str, Any],
    *,
    approval_required: bool,
) -> dict[str, Any]:
    summary = dict(risk_summary)
    summary["approval_required"] = approval_required
    notes = [
        str(note)
        for note in summary.get("notes", [])
        if isinstance(note, str)
        and "Execution requires explicit approval" not in note
        and "Rollback request recorded only" not in note
    ]
    if approval_required:
        notes.insert(0, "Execution requires explicit approval before any future rollback executor may run.")
        notes.insert(0, "Rollback request recorded only; no files, branches, refs, or index entries were changed.")
    else:
        notes.insert(0, "Rollback executes automatically under unattended full-access policy.")
    summary["notes"] = _dedupe_text(notes)
    return summary


def _execute_checkpoint_rollback(
    *,
    project: Path,
    runner: GitCommandRunner,
    checkpoint: Mapping[str, Any],
    affected_paths: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    started_at = _utc_now()
    commit = _safe_log_text(checkpoint.get("commit"))
    if commit is None:
        return {
            "ok": False,
            "performed": False,
            "reason": "checkpoint_commit_missing",
            "errors": ["Checkpoint metadata is missing a safe checkpoint commit."],
            "started_at": started_at,
            "ended_at": _utc_now(),
        }

    verify = _run_git(runner, project, ("cat-file", "-e", f"{commit}^{{commit}}"))
    if verify.returncode != 0:
        return {
            "ok": False,
            "performed": False,
            "reason": "checkpoint_commit_unavailable",
            "errors": [f"Checkpoint commit is unavailable: {_compact_command_error(verify)}"],
            "started_at": started_at,
            "ended_at": _utc_now(),
        }

    repository = _detect_repository(project, runner, runner.git_path() is not None)
    branch_before = _active_branch(project, runner)
    head_before = _rev_parse_optional(project, runner, "HEAD")
    index_before = _index_fingerprint(project, runner)
    checkpoint_path_set = set(_checkpoint_included_paths(project, runner, commit, repository))
    affected = _rollback_execution_paths(affected_paths, checkpoint_path_set)
    restore_paths = [path for path in affected if path in checkpoint_path_set]
    remove_paths = [path for path in affected if path not in checkpoint_path_set]
    errors: list[str] = []
    commands: list[dict[str, Any]] = []

    if restore_paths:
        restore = _run_git(
            runner,
            project,
            ("restore", "--source", commit, "--staged", "--worktree", "--", *restore_paths),
        )
        commands.append(
            {
                "cmd": ["git", "restore", "--source", commit, "--staged", "--worktree", "--", *restore_paths],
                "exit_code": restore.returncode,
            }
        )
        if restore.returncode != 0:
            errors.append(f"git restore failed while applying checkpoint rollback: {_compact_command_error(restore)}")

    for path in remove_paths:
        remove = _run_git(runner, project, ("rm", "-f", "--ignore-unmatch", "--", path))
        commands.append({"cmd": ["git", "rm", "-f", "--ignore-unmatch", "--", path], "exit_code": remove.returncode})
        if remove.returncode != 0:
            errors.append(f"git rm failed while removing {path}: {_compact_command_error(remove)}")
            continue
        cleanup_error = _remove_untracked_rollback_path(project, path)
        if cleanup_error:
            errors.append(cleanup_error)

    branch_after = _active_branch(project, runner)
    head_after = _rev_parse_optional(project, runner, "HEAD")
    index_after = _index_fingerprint(project, runner)
    ok = not errors
    return {
        "ok": ok,
        "performed": ok,
        "reason": "executed" if ok else "rollback_failed",
        "started_at": started_at,
        "ended_at": _utc_now(),
        "checkpoint_commit": commit,
        "restored_paths": restore_paths,
        "removed_paths": remove_paths,
        "affected_paths_count": len(affected),
        "commands": commands,
        "worktree_mutated": ok and bool(affected),
        "history_rewritten": head_before != head_after,
        "user_branch_preserved": branch_before == branch_after,
        "user_index_preserved": index_before == index_after,
        "errors": errors,
    }


def _rollback_execution_paths(
    affected_paths: Sequence[Mapping[str, Any]],
    checkpoint_paths: set[str],
) -> list[str]:
    paths: list[str] = []
    for entry in affected_paths:
        if not isinstance(entry, Mapping):
            continue
        path = _safe_project_path(entry.get("path"))
        if path is not None:
            paths.append(path)
        old_path = _safe_project_path(entry.get("old_path"))
        if old_path is not None:
            paths.append(old_path)
    if paths:
        return _dedupe_text(sorted(paths))
    return _dedupe_text(sorted(checkpoint_paths))


def _remove_untracked_rollback_path(project: Path, path: str) -> str | None:
    safe_path = _safe_project_path(path)
    if safe_path is None:
        return f"Ignoring unsafe rollback removal path: {path!r}."
    target = (project / safe_path).resolve()
    if not _is_relative_to(target, project):
        return f"Ignoring out-of-project rollback removal path: {safe_path}."
    if not target.exists() and not target.is_symlink():
        return None
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        return f"Failed to remove untracked rollback path {safe_path}: {exc}"
    return None


def _existing_pending_rollback_request(
    paths: WorkflowPaths,
    checkpoint_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    approvals = read_approval_requests(paths)
    responses = read_approval_responses(paths)
    approvals_by_id = {
        str(approval.get("approval_id") or approval.get("request_id") or ""): approval
        for approval in approvals
        if isinstance(approval, Mapping)
    }
    for request in reversed(_read_jsonl_records(paths.requests_dir / ROLLBACK_REQUESTS_FILENAME)):
        if str(request.get("checkpoint_id") or "") != checkpoint_id:
            continue
        approval_id = str(request.get("approval_request_id") or "")
        approval = approvals_by_id.get(approval_id)
        if approval is None:
            continue
        approval_status = approval_record_status(approval, responses=responses, now=_utc_now())
        if approval_status.get("status") == "pending":
            return dict(request), dict(approval_status)
    return None, None


def _new_rollback_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"rollback_{stamp}_{uuid.uuid4().hex[:8]}"


def _new_rollback_approval_request(
    *,
    workflow_id: str | None,
    rollback_request_id: str,
    checkpoint: Mapping[str, Any],
    affected_paths: Sequence[Mapping[str, Any]],
    risk_summary: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "unknown_checkpoint")
    return {
        "schema_version": SCHEMA_VERSION,
        "approval_id": new_approval_id(),
        "created_at": _utc_now(),
        "workflow_id": workflow_id,
        "type": "version_control_rollback",
        "request_id": rollback_request_id,
        "rollback_request_id": rollback_request_id,
        "checkpoint_id": checkpoint_id,
        "message": f"Approve rollback to checkpoint {checkpoint_id}.",
        "scope": f"version_control rollback checkpoint {checkpoint_id}",
        "status": "pending",
        "expires_at": default_expires_at(),
        "source": "vc_rollback_cli",
        "target_checkpoint": dict(checkpoint),
        "affected_paths": [dict(path) for path in affected_paths],
        "risk_summary": dict(risk_summary),
        "sources": dict(sources),
    }


def _new_rollback_request(
    *,
    workflow_id: str | None,
    rollback_request_id: str,
    checkpoint: Mapping[str, Any],
    affected_paths: Sequence[Mapping[str, Any]],
    risk_summary: Mapping[str, Any],
    approval_request: Mapping[str, Any] | None,
    approval_policy: Mapping[str, Any],
    sources: Mapping[str, Any],
    status: str,
    approval_required: bool,
    execution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "unknown_checkpoint")
    execution_record = dict(execution) if isinstance(execution, Mapping) else {
        "performed": False,
        "reason": "approval_required" if approval_required else status,
        "worktree_mutated": False,
        "history_rewritten": False,
        "user_branch_preserved": True,
        "user_index_preserved": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "rollback_request_id": rollback_request_id,
        "created_at": _utc_now(),
        "workflow_id": workflow_id,
        "checkpoint_id": checkpoint_id,
        "status": status,
        "source": "vc_rollback_cli",
        "target_checkpoint": dict(checkpoint),
        "affected_paths": [dict(path) for path in affected_paths],
        "risk_summary": dict(risk_summary),
        "approval_required": bool(approval_required),
        "approval_request_id": approval_request.get("approval_id") if isinstance(approval_request, Mapping) else None,
        "approval_policy_enabled": bool(approval_policy.get("enabled")),
        "execution": execution_record,
        "sources": dict(sources),
    }


def _before_run_checkpoint(
    state: Mapping[str, Any] | None,
    checkpoints: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    state_checkpoint_id = _mapping_value(state, "checkpoint_id")
    for checkpoint in checkpoints:
        if checkpoint.get("reason") == "before_worker_run":
            return dict(checkpoint)
        if state_checkpoint_id and checkpoint.get("checkpoint_id") == state_checkpoint_id:
            return dict(checkpoint)
    if isinstance(state, Mapping) and (state.get("checkpoint_id") or state.get("base_commit")):
        return {
            "checkpoint_id": state.get("checkpoint_id"),
            "created_at": state.get("generated_at"),
            "reason": "before_worker_run",
            "task_id": state.get("task_id"),
            "run_id": state.get("run_id"),
            "commit": state.get("base_commit"),
        }
    return None


def _after_run_checkpoint(checkpoints: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for checkpoint in reversed(checkpoints):
        if checkpoint.get("reason") in {"after_validation_pass", "plan_reconciled", "worker_project_changes_detected"}:
            return dict(checkpoint)
    return None


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except JSONDecodeError:
            continue
        if isinstance(data, Mapping):
            records.append(dict(data))
    return records


def _mapping_value(mapping: Mapping[str, Any] | None, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, Mapping) else None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_for_project_record(project: Path, path: Any) -> str | None:
    if not isinstance(path, Path):
        return None
    try:
        return serialize_project_path(project, path)
    except PathSerializationError:
        return None


def _repository_root_for_record(project: Path, repository: Mapping[str, Any]) -> str | None:
    root = repository.get("root")
    if not root:
        return None
    return relative_root_value(project, Path(str(root)))


def _repository_root_path(project: Path, repository: Mapping[str, Any]) -> Path:
    root = repository.get("root") if isinstance(repository, Mapping) else None
    if isinstance(root, str) and root.strip():
        try:
            return Path(root).expanduser().resolve()
        except OSError:
            return Path(root).expanduser().absolute()
    return project


def _git_path_for_project_record(
    project: Path,
    repository: Mapping[str, Any],
    path: Any,
) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    text = path.strip().replace("\\", "/")
    if text.startswith("/") or any(part in {"", "."} for part in text.split("/")):
        return None
    repo_root = _repository_root_path(project, repository)
    repo_candidate = (repo_root / text).resolve()
    repo_project_prefix = os.path.relpath(project, start=repo_root).replace(os.sep, "/")
    if repo_project_prefix != "." and (text == repo_project_prefix or text.startswith(repo_project_prefix + "/")):
        return os.path.relpath(repo_candidate, start=project).replace(os.sep, "/")
    if repo_candidate.exists():
        return os.path.relpath(repo_candidate, start=project).replace(os.sep, "/")
    if _is_relative_to(repo_candidate, project):
        return os.path.relpath(repo_candidate, start=project).replace(os.sep, "/")
    project_candidate = (project / text).resolve()
    if _is_relative_to(project_candidate, project):
        return os.path.relpath(project_candidate, start=project).replace(os.sep, "/")
    return os.path.relpath(repo_candidate, start=project).replace(os.sep, "/")


def _repository_with_scoped_dirty_status(
    project: Path,
    runner: GitCommandRunner,
    repository: Mapping[str, Any],
    path_policy: Mapping[str, Any],
) -> dict[str, Any]:
    scoped = dict(repository)
    entries = _status_entries(
        project,
        runner,
        pathspecs=tuple(str(item) for item in path_policy.get("pathspecs", (".",))),
        repository=repository,
    )
    scoped["dirty"] = bool(entries)
    scoped["dirty_files_count"] = len(entries)
    return scoped


def _is_git_internal_path(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    normalized = path.strip().replace("\\", "/")
    return normalized == ".git" or normalized.startswith(".git/") or "/.git/" in normalized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _format_file_stats(entry: Mapping[str, Any]) -> str:
    added = entry.get("lines_added")
    deleted = entry.get("lines_deleted")
    parts: list[str] = []
    if isinstance(added, int):
        parts.append(f"+{added}")
    if isinstance(deleted, int):
        parts.append(f"-{deleted}")
    return f", {' '.join(parts)}" if parts else ""


def _checkpoint_label(checkpoint: Any) -> str:
    if not isinstance(checkpoint, Mapping):
        return "unavailable"
    checkpoint_id = checkpoint.get("checkpoint_id") or "unknown"
    commit = checkpoint.get("commit")
    if commit:
        return f"{checkpoint_id} ({commit})"
    return str(checkpoint_id)


def _load_workflow(project: Path, warnings: list[str], errors: list[str]) -> dict[str, Any]:
    try:
        workflow_config = load_workflow_config(project)
    except FileNotFoundError:
        errors.append("Missing .loopplane/config/workflow.json; run loopplane init before using version control.")
        workflow_config = {}
    except JSONDecodeError as error:
        errors.append(f".loopplane/config/workflow.json is invalid JSON: {error.msg}.")
        workflow_config = {}
    except OSError as error:
        errors.append(f"Unable to read .loopplane/config/workflow.json: {error}.")
        workflow_config = {}
    except WorkflowPathError as error:
        if "workflow config file is missing" in str(error):
            errors.append("Missing .loopplane/config/workflow.json; run loopplane init before using version control.")
        else:
            errors.append(f"Invalid workflow path configuration: {error}.")
        workflow_config = {}

    workflow_id = workflow_config.get("workflow_id") if isinstance(workflow_config, dict) else None
    if workflow_id is not None and not isinstance(workflow_id, str):
        warnings.append("workflow_id is not a string; refs_namespace placeholders cannot be fully resolved.")
        workflow_id = None

    try:
        paths = WorkflowPaths.from_config(project, workflow_config)
    except WorkflowPathError as error:
        errors.append(f"Invalid workflow path configuration: {error}.")
        paths = WorkflowPaths.from_config(project, {})

    return {"config": workflow_config, "workflow_id": workflow_id, "paths": paths}


def _checkpoint_failure(
    project: Path,
    workflow_id: str | None,
    git: dict[str, Any],
    warnings: list[str],
    errors: list[str],
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "waiting_config",
        "ok": False,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "git": git,
        "checkpoint": None,
        "warnings": warnings,
        "errors": errors,
    }


def _run_metadata_failure(
    project: Path,
    run_dir: Path,
    workflow_id: str | None,
    git: dict[str, Any],
    warnings: list[str],
    errors: list[str],
    generated_at: str,
    task_id: str,
    run_id: str,
    run_kind: str,
    stage: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "waiting_config",
        "ok": False,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "stage": stage,
        "run_dir": _path_for_project_record(project, run_dir),
        "git": git,
        "metadata": None,
        "warnings": warnings,
        "errors": errors,
    }


def _load_json_mapping(path: Path) -> dict[str, Any] | None:
    data = _load_json_file(path)
    return data if isinstance(data, dict) else None


def _latest_checkpoint_record(path: Path) -> dict[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(record, Mapping) or str(record.get("status") or "") != "created":
            continue
        return _sanitize_checkpoint_record(record)
    return None


def _sanitize_checkpoint_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_id": record.get("checkpoint_id"),
        "created_at": record.get("created_at"),
        "reason": record.get("reason"),
        "task_id": record.get("task_id"),
        "run_id": record.get("run_id"),
        "commit": record.get("commit") or record.get("commit_sha"),
    }


def _checkpoint_from_read_model(read_model: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(read_model, Mapping):
        return None
    checkpoint = read_model.get("last_checkpoint")
    if not isinstance(checkpoint, Mapping):
        checkpoint = read_model.get("latest_checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    return _sanitize_checkpoint_record(checkpoint)


def _repository_from_read_model(
    read_model: Mapping[str, Any] | None,
    *,
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    repository = dict(fallback)
    if not isinstance(read_model, Mapping):
        return repository
    raw_repository = read_model.get("repository")
    if isinstance(raw_repository, Mapping):
        detected = raw_repository.get("detected")
        if detected is None:
            detected = raw_repository.get("inside_work_tree")
        repository.update(
            {
                "inside_work_tree": bool(detected),
                "root": raw_repository.get("root"),
                "head_commit": raw_repository.get("head_commit"),
                "dirty": bool(raw_repository.get("dirty")),
                "dirty_files_count": int(raw_repository.get("dirty_files_count") or 0),
            }
        )
    elif "repo_detected" in read_model:
        repository["inside_work_tree"] = bool(read_model.get("repo_detected"))
    if "dirty" in read_model:
        repository["dirty"] = bool(read_model.get("dirty"))
    if "dirty_files_count" in read_model:
        repository["dirty_files_count"] = int(read_model.get("dirty_files_count") or 0)
    return repository


def _config_enabled(config: Mapping[str, Any] | None, read_model: Mapping[str, Any] | None) -> bool:
    if isinstance(config, Mapping) and "enabled" in config:
        return bool(config.get("enabled"))
    if isinstance(read_model, Mapping) and "enabled" in read_model:
        return bool(read_model.get("enabled"))
    return True


def _provider(config: Mapping[str, Any] | None, read_model: Mapping[str, Any] | None) -> str:
    value = _config_value(config, read_model, "provider")
    return str(value or SUPPORTED_PROVIDER)


def _config_value(
    config: Mapping[str, Any] | None,
    read_model: Mapping[str, Any] | None,
    *keys: str,
) -> Any:
    for key in keys:
        if isinstance(config, Mapping) and key in config:
            return config.get(key)
        if isinstance(read_model, Mapping) and key in read_model:
            return read_model.get(key)
        configuration = read_model.get("configuration") if isinstance(read_model, Mapping) else None
        if isinstance(configuration, Mapping) and key in configuration:
            return configuration.get(key)
    return None


def _read_model_dirty(read_model: Mapping[str, Any] | None, *, default: bool) -> bool:
    if not isinstance(read_model, Mapping):
        return default
    if "dirty" in read_model:
        return bool(read_model.get("dirty"))
    repository = read_model.get("repository")
    if isinstance(repository, Mapping) and "dirty" in repository:
        return bool(repository.get("dirty"))
    return default


def _read_model_dirty_files_count(read_model: Mapping[str, Any] | None, *, default: int) -> int:
    if not isinstance(read_model, Mapping):
        return default
    if "dirty_files_count" in read_model:
        return int(read_model.get("dirty_files_count") or 0)
    repository = read_model.get("repository")
    if isinstance(repository, Mapping) and "dirty_files_count" in repository:
        return int(repository.get("dirty_files_count") or 0)
    return default


def _rollback_status(
    *,
    config: Mapping[str, Any] | None,
    enabled: bool,
    git_available: bool,
    repo_detected: bool,
    latest_checkpoint: Mapping[str, Any] | None,
    errors: Sequence[str],
) -> dict[str, Any]:
    policy = config.get("rollback_policy") if isinstance(config, Mapping) else None
    if not isinstance(policy, Mapping):
        policy = {}
    allow_rollback = bool(policy.get("allow_rollback", True))
    requires_approval = bool(policy.get("rollback_requires_approval", False))
    never_auto = bool(policy.get("never_auto_rollback_user_changes", False))
    available = bool(
        enabled
        and git_available
        and repo_detected
        and latest_checkpoint
        and allow_rollback
        and not errors
    )
    reason = "available"
    if not enabled:
        reason = "version_control_disabled"
    elif not git_available:
        reason = "git_unavailable"
    elif not repo_detected:
        reason = "repository_unavailable"
    elif not latest_checkpoint:
        reason = "no_checkpoint"
    elif not allow_rollback:
        reason = "rollback_disabled"
    elif errors:
        reason = "configuration_problem"
    return {
        "available": available,
        "requires_approval": requires_approval,
        "never_auto_rollback_user_changes": never_auto,
        "reason": reason,
    }


def _status_label(*, errors: Sequence[str], warnings: Sequence[str], enabled: bool) -> str:
    if not enabled:
        return "disabled" if not errors else "waiting_config"
    if errors:
        return "waiting_config"
    if warnings:
        return "warning"
    return "ok"


def _dedupe_text(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _capture_pre_run_git_metadata(
    project: Path,
    run_dir: Path,
    git_dir: Path,
    workflow_id: str,
    paths: WorkflowPaths,
    config: dict[str, Any],
    runner: GitCommandRunner,
    git: dict[str, Any],
    repository: dict[str, Any],
    warnings: list[str],
    task_id: str,
    run_id: str,
    run_kind: str,
    generated_at: str,
) -> dict[str, Any]:
    git_dir.mkdir(parents=True, exist_ok=True)
    head = _rev_parse_optional(project, runner, "HEAD")
    path_policy = _workspace_path_policy(project, config, paths)
    pre_status = _status_snapshot(
        project,
        runner,
        repository,
        path_policy["pathspecs"],
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "pre_run",
        generated_at,
        head,
    )
    _write_text_file(git_dir / "pre_run_head.txt", f"{head or 'UNBORN'}\n")
    _write_json_file(git_dir / "pre_run_status.json", pre_status)

    checkpoint_result: dict[str, Any] | None = None
    checkpoint: Mapping[str, Any] | None = None
    if _checkpoint_policy_enabled(config, "before_worker_run"):
        checkpoint_result = create_git_checkpoint(
            project,
            reason="before_worker_run",
            task_id=task_id,
            run_id=run_id,
            runner=runner,
        )
        if not checkpoint_result.get("ok"):
            return _run_metadata_failure(
                project,
                run_dir,
                workflow_id,
                git,
                warnings + list(checkpoint_result.get("warnings", [])),
                [str(error) for error in checkpoint_result.get("errors", [])],
                generated_at,
                task_id,
                run_id,
                run_kind,
                "pre",
            )
        raw_checkpoint = checkpoint_result.get("checkpoint")
        if isinstance(raw_checkpoint, Mapping):
            checkpoint = raw_checkpoint
    state = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "stage": "pre",
        "run_dir": _path_for_project_record(project, run_dir),
        "base_commit": checkpoint.get("commit") if checkpoint is not None else head,
        "base_ref": checkpoint.get("ref") if checkpoint is not None else None,
        "checkpoint_id": checkpoint.get("checkpoint_id") if checkpoint is not None else None,
        "pre_run_head": head,
        "pre_run_status_file": "pre_run_status.json",
        "path_policy_applied": True,
        "path_policy": {
            "scope": "workspace_boundary",
            "workspace_boundary": path_policy["workspace_boundary"],
            "resolved_workspace_boundary": path_policy["resolved_workspace_boundary"],
            "pathspecs": list(path_policy["pathspecs"]),
        },
    }
    _write_json_file(git_dir / "run_metadata_state.json", state)

    metadata = {
        "git_dir": _path_for_project_record(project, git_dir),
        "pre_run_head": head,
        "pre_run_status_file": _path_for_project_record(project, git_dir / "pre_run_status.json"),
        "repository_root": _repository_root_for_record(project, repository),
        "config_path": _path_for_project_record(project, paths.version_control_config_file),
        "artifacts": [
            "pre_run_head.txt",
            "pre_run_status.json",
            "run_metadata_state.json",
        ],
    }
    if checkpoint is not None:
        metadata["checkpoint"] = {
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "ref": checkpoint.get("ref"),
            "commit": checkpoint.get("commit"),
            "reason": checkpoint.get("reason"),
        }
    return _run_metadata_success(
        project,
        run_dir,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "pre",
        git,
        warnings + list(checkpoint_result.get("warnings", [])) if checkpoint_result is not None else warnings,
        generated_at,
        metadata,
    )


def _capture_pre_run_git_status_metadata(
    project: Path,
    run_dir: Path,
    git_dir: Path,
    workflow_id: str,
    paths: WorkflowPaths,
    config: dict[str, Any],
    runner: GitCommandRunner,
    git: dict[str, Any],
    repository: dict[str, Any],
    warnings: list[str],
    task_id: str,
    run_id: str,
    run_kind: str,
    generated_at: str,
) -> dict[str, Any]:
    git_dir.mkdir(parents=True, exist_ok=True)
    head = _rev_parse_optional(project, runner, "HEAD")
    pathspecs = _run_metadata_pathspecs(config, paths, project, git_dir)
    pre_status = _status_snapshot(
        project,
        runner,
        repository,
        pathspecs,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "pre_run",
        generated_at,
        head,
    )
    _write_text_file(git_dir / "pre_run_head.txt", f"{head or 'UNBORN'}\n")
    _write_json_file(git_dir / "pre_run_status.json", pre_status)
    checkpoint_result: dict[str, Any] | None = None
    checkpoint: Mapping[str, Any] | None = None
    if _checkpoint_policy_enabled(config, "before_worker_run"):
        checkpoint_result = create_git_checkpoint(
            project,
            reason="before_worker_run",
            task_id=task_id,
            run_id=run_id,
            runner=runner,
        )
        if not checkpoint_result.get("ok"):
            return _run_metadata_failure(
                project,
                run_dir,
                workflow_id,
                git,
                warnings + list(checkpoint_result.get("warnings", [])),
                [str(error) for error in checkpoint_result.get("errors", [])],
                generated_at,
                task_id,
                run_id,
                run_kind,
                "pre",
            )
        raw_checkpoint = checkpoint_result.get("checkpoint")
        if isinstance(raw_checkpoint, Mapping):
            checkpoint = raw_checkpoint
    state = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "stage": "pre",
        "detail_level": "status",
        "run_dir": _path_for_project_record(project, run_dir),
        "base_commit": checkpoint.get("commit") if checkpoint is not None else head,
        "base_ref": checkpoint.get("ref") if checkpoint is not None else None,
        "checkpoint_id": checkpoint.get("checkpoint_id") if checkpoint is not None else None,
        "pre_run_head": head,
        "pre_run_status_file": "pre_run_status.json",
        "path_policy_applied": True,
        "path_policy": {
            "scope": "workspace_boundary",
            "pathspecs": list(pathspecs),
        },
    }
    _write_json_file(git_dir / "run_metadata_state.json", state)
    metadata = {
        "git_dir": _path_for_project_record(project, git_dir),
        "detail_level": "status",
        "pre_run_head": head,
        "pre_run_status_file": _path_for_project_record(project, git_dir / "pre_run_status.json"),
        "repository_root": _repository_root_for_record(project, repository),
        "config_path": _path_for_project_record(project, paths.version_control_config_file),
        "artifacts": [
            "pre_run_head.txt",
            "pre_run_status.json",
            "run_metadata_state.json",
        ],
    }
    if checkpoint is not None:
        metadata["checkpoint"] = {
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "ref": checkpoint.get("ref"),
            "commit": checkpoint.get("commit"),
            "reason": checkpoint.get("reason"),
        }
    return _run_metadata_success(
        project,
        run_dir,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "pre",
        git,
        warnings + list(checkpoint_result.get("warnings", [])) if checkpoint_result is not None else warnings,
        generated_at,
        metadata,
    )


def _capture_post_run_git_status_metadata(
    project: Path,
    run_dir: Path,
    git_dir: Path,
    workflow_id: str,
    paths: WorkflowPaths,
    config: dict[str, Any],
    runner: GitCommandRunner,
    git: dict[str, Any],
    repository: dict[str, Any],
    warnings: list[str],
    task_id: str,
    run_id: str,
    run_kind: str,
    generated_at: str,
) -> dict[str, Any]:
    state_path = git_dir / "run_metadata_state.json"
    state = _load_json_file(state_path)
    if not isinstance(state, dict):
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            [f"Missing pre-run metadata state: {state_path}."],
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )
    head = _rev_parse_optional(project, runner, "HEAD")
    pathspecs = _run_metadata_pathspecs(config, paths, project, git_dir)
    post_status = _status_snapshot(
        project,
        runner,
        repository,
        pathspecs,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "post_run",
        generated_at,
        head,
    )
    pre_status = _load_json_file(git_dir / "pre_run_status.json")
    pre_entries = _status_entries_from_snapshot(pre_status)
    changed_entries = _changed_files_from_status_delta(pre_entries, _status_entries_from_snapshot(post_status))
    changed_files = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "detail_level": "status",
        "base_commit": state.get("base_commit"),
        "current_head": head,
        "changed_files": changed_entries,
        "path_policy": {
            "scope": "workspace_boundary",
            "pathspecs": list(pathspecs),
        },
    }
    git_dir.mkdir(parents=True, exist_ok=True)
    _write_json_file(git_dir / "post_run_status.json", post_status)
    _write_json_file(git_dir / "changed_files.json", changed_files)
    metadata = {
        "git_dir": _path_for_project_record(project, git_dir),
        "detail_level": "status",
        "base_commit": state.get("base_commit"),
        "current_head": head,
        "repository_root": _repository_root_for_record(project, repository),
        "post_run_status_file": _path_for_project_record(project, git_dir / "post_run_status.json"),
        "changed_files_file": _path_for_project_record(project, git_dir / "changed_files.json"),
        "changed_files_count": len(changed_entries),
        "artifacts": [
            "pre_run_head.txt",
            "pre_run_status.json",
            "post_run_status.json",
            "changed_files.json",
            "run_metadata_state.json",
        ],
    }
    return _run_metadata_success(
        project,
        run_dir,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "post",
        git,
        warnings,
        generated_at,
        metadata,
    )


def _capture_post_run_git_metadata(
    project: Path,
    run_dir: Path,
    git_dir: Path,
    workflow_id: str,
    paths: WorkflowPaths,
    config: dict[str, Any],
    runner: GitCommandRunner,
    git: dict[str, Any],
    repository: dict[str, Any],
    warnings: list[str],
    task_id: str,
    run_id: str,
    run_kind: str,
    generated_at: str,
) -> dict[str, Any]:
    state_path = git_dir / "run_metadata_state.json"
    state = _load_json_file(state_path)
    if not isinstance(state, dict):
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            [f"Missing pre-run metadata state: {state_path}."],
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )

    base_commit = state.get("base_commit")
    if not isinstance(base_commit, str) or not base_commit:
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            [f"{state_path}: base_commit is missing."],
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )

    verify_base = _run_git(runner, project, ("cat-file", "-e", f"{base_commit}^{{commit}}"))
    if verify_base.returncode != 0:
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            [f"Pre-run checkpoint commit is unavailable: {_compact_command_error(verify_base)}"],
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )

    head = _rev_parse_optional(project, runner, "HEAD")
    pathspecs = _run_metadata_pathspecs(config, paths, project, git_dir)
    post_status = _status_snapshot(
        project,
        runner,
        repository,
        pathspecs,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "post_run",
        generated_at,
        head,
    )
    tree = _current_worktree_tree(project, runner, config, paths, git_dir, generated_at)
    if not tree["ok"]:
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            tree["errors"],
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )

    changed_files_result = _changed_files_from_diff(
        runner,
        project,
        base_commit,
        tree["tree"],
        pathspecs,
        repository,
    )
    patch = _run_git(
        runner,
        project,
        ("diff", "--patch", "--find-renames", base_commit, tree["tree"], "--", *pathspecs),
    )
    errors = list(changed_files_result.get("errors", []))
    if patch.returncode != 0:
        errors.append(f"git diff patch failed: {_compact_command_error(patch)}")
    if errors:
        return _run_metadata_failure(
            project,
            run_dir,
            workflow_id,
            git,
            warnings,
            errors,
            generated_at,
            task_id,
            run_id,
            run_kind,
            "post",
        )

    changed_files = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "base_commit": base_commit,
        "current_tree": tree["tree"],
        "changed_files": changed_files_result["changed_files"],
        "path_policy": {
            "scope": "workspace_boundary",
            "pathspecs": list(pathspecs),
        },
    }
    git_dir.mkdir(parents=True, exist_ok=True)
    _write_json_file(git_dir / "post_run_status.json", post_status)
    _write_json_file(git_dir / "changed_files.json", changed_files)
    _write_text_file(git_dir / "project_diff.patch", patch.stdout)

    metadata = {
        "git_dir": _path_for_project_record(project, git_dir),
        "base_commit": base_commit,
        "current_tree": tree["tree"],
        "repository_root": _repository_root_for_record(project, repository),
        "post_run_status_file": _path_for_project_record(project, git_dir / "post_run_status.json"),
        "changed_files_file": _path_for_project_record(project, git_dir / "changed_files.json"),
        "project_diff_file": _path_for_project_record(project, git_dir / "project_diff.patch"),
        "changed_files_count": len(changed_files_result["changed_files"]),
        "artifacts": [
            "pre_run_head.txt",
            "pre_run_status.json",
            "post_run_status.json",
            "changed_files.json",
            "project_diff.patch",
            "run_metadata_state.json",
        ],
    }
    return _run_metadata_success(
        project,
        run_dir,
        workflow_id,
        task_id,
        run_id,
        run_kind,
        "post",
        git,
        warnings,
        generated_at,
        metadata,
    )


def _run_metadata_success(
    project: Path,
    run_dir: Path,
    workflow_id: str,
    task_id: str,
    run_id: str,
    run_kind: str,
    stage: str,
    git: dict[str, Any],
    warnings: list[str],
    generated_at: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "ok",
        "ok": True,
        "project_root": str(project),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "stage": stage,
        "run_dir": _path_for_project_record(project, run_dir),
        "git": git,
        "metadata": metadata,
        "warnings": warnings,
        "errors": [],
    }


def _create_checkpoint_commit(
    project: Path,
    runner: GitCommandRunner,
    checkpoint_ref: str,
    checkpoint_id: str,
    workflow_id: str,
    reason: str,
    task_id: str | None,
    run_id: str | None,
    parent: str | None,
    created_at: str,
    config: dict[str, Any],
    path_policy: Mapping[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="loopplane_git_index_") as temp_dir:
        env = _checkpoint_git_env(Path(temp_dir) / "index", created_at)

        read_tree = _run_git(runner, project, ("read-tree", "--empty"), extra_env=env)
        if read_tree.returncode != 0:
            return {"ok": False, "errors": [f"git read-tree failed: {_compact_command_error(read_tree)}"]}

        pathspecs = tuple(str(item) for item in path_policy.get("pathspecs", _checkpoint_pathspecs(config)))
        add_result = _add_checkpoint_visible_paths(runner, project, pathspecs, env)
        if not add_result["ok"]:
            return add_result

        tree = _run_git(runner, project, ("write-tree",), extra_env=env)
        if tree.returncode != 0:
            return {"ok": False, "errors": [f"git write-tree failed: {_compact_command_error(tree)}"]}
        tree_id = tree.stdout.strip()

        commit_args = ["commit-tree", tree_id]
        if parent:
            commit_args.extend(("-p", parent))
        commit_args.extend(("-m", _checkpoint_commit_message(checkpoint_id, workflow_id, reason, task_id, run_id)))
        commit = _run_git(runner, project, tuple(commit_args), extra_env=env)
        if commit.returncode != 0:
            return {"ok": False, "errors": [f"git commit-tree failed: {_compact_command_error(commit)}"]}
        commit_id = commit.stdout.strip()

    update_ref = _run_git(runner, project, ("update-ref", checkpoint_ref, commit_id))
    if update_ref.returncode != 0:
        return {"ok": False, "errors": [f"git update-ref failed: {_compact_command_error(update_ref)}"]}

    return {"ok": True, "commit": commit_id, "tree": tree_id}


def _checkpoint_git_env(index_path: Path, created_at: str) -> dict[str, str]:
    return {
        "GIT_INDEX_FILE": str(index_path),
        "GIT_AUTHOR_NAME": "LoopPlane",
        "GIT_AUTHOR_EMAIL": "loopplane@example.invalid",
        "GIT_AUTHOR_DATE": created_at,
        "GIT_COMMITTER_NAME": "LoopPlane",
        "GIT_COMMITTER_EMAIL": "loopplane@example.invalid",
        "GIT_COMMITTER_DATE": created_at,
    }


def _checkpoint_commit_message(
    checkpoint_id: str,
    workflow_id: str,
    reason: str,
    task_id: str | None,
    run_id: str | None,
) -> str:
    lines = [
        f"loopplane checkpoint: {reason}",
        "",
        f"workflow_id: {workflow_id}",
        f"checkpoint_id: {checkpoint_id}",
    ]
    if task_id:
        lines.append(f"task_id: {task_id}")
    if run_id:
        lines.append(f"run_id: {run_id}")
    return "\n".join(lines)


def _checkpoint_pathspecs(config: dict[str, Any]) -> tuple[str, ...]:
    excludes: list[str] = []
    path_policy = config.get("path_policy")
    if isinstance(path_policy, dict):
        configured = path_policy.get("exclude")
        if isinstance(configured, list):
            excludes = [item for item in configured if isinstance(item, str) and item.strip()]

    pathspecs = ["."]
    for exclude in excludes:
        pathspecs.extend(_exclude_pathspec_variants(exclude))
    return tuple(pathspecs)


def _workspace_path_policy(
    project: Path,
    config: Mapping[str, Any],
    paths: WorkflowPaths,
    *,
    extra_excludes: Sequence[str] = (),
) -> dict[str, Any]:
    workspace = _load_json_mapping(project / ".loopplane" / "workspace.json")
    boundary_root = project
    workspace_boundary = "project_root"
    allow_out_of_boundary_writes = False
    if isinstance(workspace, Mapping):
        workspace_boundary = str(workspace.get("workspace_boundary") or "project_root")
        allow_out_of_boundary_writes = bool(workspace.get("allow_out_of_boundary_writes"))
        try:
            boundary_root = workspace_boundary_root(project, workspace)
        except (OSError, ValueError):
            boundary_root = project

    configured_includes, configured_excludes = _configured_path_policy(config)
    excludes = [*configured_excludes, *extra_excludes]
    pathspecs = ["."]
    for exclude in excludes:
        if _is_git_internal_path(exclude):
            continue
        if _project_policy_path_inside_boundary(project, boundary_root, exclude):
            pathspecs.extend(_exclude_pathspec_variants(exclude))

    return {
        "scope": "workspace_boundary",
        "workspace_boundary": workspace_boundary,
        "resolved_workspace_boundary": boundary_root.as_posix(),
        "allow_out_of_boundary_writes": allow_out_of_boundary_writes,
        "configured_includes": tuple(configured_includes),
        "configured_excludes": tuple(configured_excludes),
        "pathspecs": tuple(dict.fromkeys(pathspecs)),
        "paths": paths,
    }


def _exclude_pathspec_variants(exclude: str) -> list[str]:
    text = str(exclude or "").strip()
    if not text:
        return []
    variants = [f":(exclude){text}"]
    if text.endswith("/"):
        variants.append(f":(exclude){text.rstrip('/')}")
    return variants


def _configured_path_policy(config: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path_policy = config.get("path_policy") if isinstance(config, Mapping) else None
    includes: list[str] = []
    excludes: list[str] = []
    if isinstance(path_policy, Mapping):
        raw_includes = path_policy.get("include")
        if isinstance(raw_includes, Sequence) and not isinstance(raw_includes, (str, bytes)):
            includes = [_clean_policy_path(item) for item in raw_includes if _clean_policy_path(item)]
        raw_excludes = path_policy.get("exclude")
        if isinstance(raw_excludes, Sequence) and not isinstance(raw_excludes, (str, bytes)):
            excludes = [_clean_policy_path(item) for item in raw_excludes if _clean_policy_path(item)]
    return tuple(dict.fromkeys(includes)), tuple(dict.fromkeys(excludes))


def _clean_policy_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\\", "/")
    if not text or text.startswith("/") or any(part == ".." for part in text.split("/")):
        return None
    return text


def _project_policy_path_inside_boundary(project: Path, boundary_root: Path, value: str) -> bool:
    text = value.rstrip("/")
    if not text:
        return True
    if any(char in text for char in "*?["):
        return True
    return _is_relative_to((project / text).resolve(), boundary_root)


def _checkpoint_included_paths(
    project: Path,
    runner: GitCommandRunner,
    commit: str,
    repository: Mapping[str, Any],
) -> list[str]:
    tree = _run_git(runner, project, ("ls-tree", "-r", "--name-only", commit))
    if tree.returncode != 0:
        return []
    included: list[str] = []
    for raw_path in tree.stdout.splitlines():
        path = _git_path_for_project_record(project, repository, raw_path)
        if path is not None and not _is_git_internal_path(path) and ".git" not in path:
            included.append(path)
    return _dedupe_text(sorted(included))


def _excluded_status_paths(
    all_status: Sequence[Mapping[str, Any]],
    scoped_status: Sequence[Mapping[str, Any]],
) -> list[str]:
    scoped = {str(entry.get("path") or "") for entry in scoped_status if isinstance(entry, Mapping)}
    excluded: list[str] = []
    for entry in all_status:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "")
        if not path or path in scoped or _is_git_internal_path(path):
            continue
        excluded.append(path)
        old_path = str(entry.get("old_path") or "")
        if old_path and old_path not in scoped and not _is_git_internal_path(old_path):
            excluded.append(old_path)
    return _dedupe_text(sorted(excluded))


def _current_worktree_tree(
    project: Path,
    runner: GitCommandRunner,
    config: dict[str, Any],
    paths: WorkflowPaths,
    git_dir: Path,
    created_at: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="loopplane_run_git_index_") as temp_dir:
        env = _checkpoint_git_env(Path(temp_dir) / "index", created_at)
        read_tree = _run_git(runner, project, ("read-tree", "--empty"), extra_env=env)
        if read_tree.returncode != 0:
            return {"ok": False, "errors": [f"git read-tree failed: {_compact_command_error(read_tree)}"]}

        pathspecs = _run_metadata_pathspecs(config, paths, project, git_dir)
        add_result = _add_checkpoint_visible_paths(runner, project, pathspecs, env)
        if not add_result["ok"]:
            return add_result

        tree = _run_git(runner, project, ("write-tree",), extra_env=env)
        if tree.returncode != 0:
            return {"ok": False, "errors": [f"git write-tree failed: {_compact_command_error(tree)}"]}
        return {"ok": True, "tree": tree.stdout.strip()}


def _add_checkpoint_visible_paths(
    runner: GitCommandRunner,
    project: Path,
    pathspecs: Sequence[str],
    temp_index_env: Mapping[str, str],
) -> dict[str, Any]:
    listed = _run_git(
        runner,
        project,
        ("ls-files", "-c", "-o", "--exclude-standard", "-z", "--", *tuple(pathspecs)),
    )
    if listed.returncode != 0:
        return {"ok": False, "errors": [f"git ls-files for checkpoint failed: {_compact_command_error(listed)}"]}
    paths = [
        item
        for item in listed.stdout.split("\0")
        if item and ((project / item).exists() or (project / item).is_symlink())
    ]
    if not paths:
        return {"ok": True, "added_paths": 0}
    for start in range(0, len(paths), 200):
        chunk = paths[start : start + 200]
        add = _run_git(runner, project, ("add", "-A", "--", *chunk), extra_env=temp_index_env)
        if add.returncode != 0:
            return {"ok": False, "errors": [f"git add with temporary index failed: {_compact_command_error(add)}"]}
    return {"ok": True, "added_paths": len(paths)}


def _run_metadata_pathspecs(
    config: dict[str, Any],
    paths: WorkflowPaths,
    project: Path,
    git_dir: Path,
) -> tuple[str, ...]:
    policy = _workspace_path_policy(
        project,
        config,
        paths,
        extra_excludes=_run_metadata_excludes(paths, project, git_dir),
    )
    return tuple(str(item) for item in policy["pathspecs"])


def _run_metadata_excludes(paths: WorkflowPaths, project: Path, git_dir: Path) -> tuple[str, ...]:
    excludes = [
        *_default_metadata_excludes(paths, project),
        _dir_prefix(paths.value("read_models_dir")),
        f"{paths.value('runtime_dir')}/git_checkpoints.jsonl",
        f"{paths.value('read_models_dir')}/version_control_status.json",
    ]
    relative_git_dir = _relative_project_path(git_dir, project)
    if relative_git_dir:
        excludes.append(_dir_prefix(relative_git_dir))
    return tuple(dict.fromkeys(excludes))


def _checkpoint_metadata_excludes(paths: WorkflowPaths, project: Path | None = None) -> tuple[str, ...]:
    project_root = project or paths.project_root
    return tuple(
        dict.fromkeys(
            [
                *_default_metadata_excludes(paths, project_root),
                f"{paths.value('runtime_dir')}/git_checkpoints.jsonl",
                f"{paths.value('read_models_dir')}/version_control_status.json",
            ]
        )
    )


def _default_metadata_excludes(paths: WorkflowPaths, project: Path) -> tuple[str, ...]:
    excludes = [
        *PROJECT_LOCAL_MACHINE_STATE_EXCLUDES,
        *DEFAULT_GENERATED_PATH_EXCLUDES,
    ]
    loopplane_home_relative = _loopplane_home_relative_to_project(project)
    if loopplane_home_relative:
        excludes.append(_dir_prefix(loopplane_home_relative))
    return tuple(dict.fromkeys(excludes))


def _loopplane_home_relative_to_project(project: Path) -> str | None:
    raw = os.environ.get("LOOPPLANE_HOME")
    if not raw or not raw.strip():
        return None
    try:
        home = Path(raw).expanduser()
        if not home.is_absolute():
            home = (Path.cwd() / home).resolve()
        else:
            home = home.resolve()
    except OSError:
        return None
    return _relative_project_path(home, project)


def _changed_files_from_diff(
    runner: GitCommandRunner,
    project: Path,
    base_commit: str,
    current_tree: str,
    pathspecs: tuple[str, ...],
    repository: Mapping[str, Any],
) -> dict[str, Any]:
    name_status = _run_git(
        runner,
        project,
        ("diff", "--name-status", "--find-renames", base_commit, current_tree, "--", *pathspecs),
    )
    numstat = _run_git(
        runner,
        project,
        ("diff", "--numstat", "--find-renames", base_commit, current_tree, "--", *pathspecs),
    )
    errors: list[str] = []
    if name_status.returncode != 0:
        errors.append(f"git diff name-status failed: {_compact_command_error(name_status)}")
    if numstat.returncode != 0:
        errors.append(f"git diff numstat failed: {_compact_command_error(numstat)}")
    if errors:
        return {"changed_files": [], "errors": errors}

    stats_by_path = _parse_numstat(project, repository, numstat.stdout)
    changed_files: list[dict[str, Any]] = []
    for entry in _parse_name_status(project, repository, name_status.stdout):
        path = entry["path"]
        stats = stats_by_path.get(path, {})
        lines_added = stats.get("lines_added")
        lines_deleted = stats.get("lines_deleted")
        record: dict[str, Any] = {
            "path": path,
            "change_type": entry["change_type"],
            "lines_added": lines_added,
            "lines_deleted": lines_deleted,
        }
        if isinstance(lines_added, int) and isinstance(lines_deleted, int):
            record["line_delta"] = lines_added - lines_deleted
        if entry.get("old_path"):
            record["old_path"] = entry["old_path"]
        changed_files.append(record)
    return {"changed_files": changed_files, "errors": []}


def _status_entries_from_snapshot(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    entries = snapshot.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping) and entry.get("path")]


def _changed_files_from_status_delta(
    pre_entries: Sequence[Mapping[str, Any]],
    post_entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pre_by_path = {str(entry.get("path")): _status_entry_signature(entry) for entry in pre_entries if entry.get("path")}
    post_by_path = {str(entry.get("path")): entry for entry in post_entries if entry.get("path")}
    changed: list[dict[str, Any]] = []
    for path in sorted(set(pre_by_path) | set(post_by_path)):
        post = post_by_path.get(path)
        if post is None:
            changed.append({"path": path, "change_type": "status_cleared"})
            continue
        signature = _status_entry_signature(post)
        if pre_by_path.get(path) == signature:
            continue
        record = {
            "path": path,
            "change_type": str(post.get("change_type") or "modified"),
        }
        for field in ("old_path", "index_status", "worktree_status"):
            value = post.get(field)
            if value:
                record[field] = value
        changed.append(record)
    return changed


def _status_entry_signature(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("path"),
        entry.get("old_path"),
        entry.get("index_status"),
        entry.get("worktree_status"),
        entry.get("change_type"),
    )


def _parse_name_status(
    project: Path,
    repository: Mapping[str, Any],
    output: str,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        code = status[:1]
        if code in {"R", "C"} and len(parts) >= 3:
            path = parts[2]
            old_path = parts[1]
        else:
            path = parts[-1]
            old_path = ""
        normalized_path = _git_path_for_project_record(project, repository, path)
        if normalized_path is None:
            continue
        entry = {
            "path": normalized_path,
            "change_type": _diff_change_type(code),
        }
        if old_path:
            normalized_old_path = _git_path_for_project_record(project, repository, old_path)
            if normalized_old_path is not None:
                entry["old_path"] = normalized_old_path
        entries.append(entry)
    return entries


def _parse_numstat(
    project: Path,
    repository: Mapping[str, Any],
    output: str,
) -> dict[str, dict[str, int | None]]:
    stats: dict[str, dict[str, int | None]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = _git_path_for_project_record(project, repository, parts[-1])
        if path is None:
            continue
        stats[path] = {
            "lines_added": _parse_optional_int(parts[0]),
            "lines_deleted": _parse_optional_int(parts[1]),
        }
    return stats


def _diff_change_type(code: str) -> str:
    return {
        "A": "added",
        "C": "copied",
        "D": "deleted",
        "M": "modified",
        "R": "renamed",
        "T": "type_changed",
        "U": "unmerged",
        "X": "unknown",
    }.get(code, "modified")


def _parse_optional_int(value: str) -> int | None:
    if value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _run_git(
    runner: GitCommandRunner,
    project: Path,
    args: Sequence[str],
    *,
    extra_env: Mapping[str, str] | None = None,
) -> GitCommandResult:
    if not extra_env:
        return runner.run(project, args)
    if isinstance(runner, SubprocessGitCommandRunner):
        return runner.run(project, args, extra_env=extra_env)

    executable = runner.git_path()
    if executable is None:
        return GitCommandResult(127, "", "git executable not found")
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.update(extra_env)
    completed = subprocess.run(
        [executable, "-C", str(project), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    return GitCommandResult(completed.returncode, completed.stdout, completed.stderr)


def _active_branch(project: Path, runner: GitCommandRunner) -> str | None:
    branch = _run_git(runner, project, ("symbolic-ref", "--quiet", "--short", "HEAD"))
    if branch.returncode == 0 and branch.stdout.strip():
        return branch.stdout.strip()
    detached = _run_git(runner, project, ("rev-parse", "--short", "HEAD"))
    if detached.returncode == 0 and detached.stdout.strip():
        return f"HEAD:{detached.stdout.strip()}"
    return None


def _rev_parse_optional(project: Path, runner: GitCommandRunner, revision: str) -> str | None:
    result = _run_git(runner, project, ("rev-parse", "--verify", revision))
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _index_fingerprint(project: Path, runner: GitCommandRunner) -> str:
    result = _run_git(runner, project, ("ls-files", "--stage", "-z"))
    if result.returncode != 0:
        return ""
    return result.stdout


def _status_entries(
    project: Path,
    runner: GitCommandRunner,
    *,
    pathspecs: Sequence[str] | None = None,
    repository: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    args = ["status", "--porcelain=v1", "-z"]
    if pathspecs is not None:
        args.extend(("--", *pathspecs))
    result = _run_git(runner, project, tuple(args))
    if result.returncode != 0:
        return []
    repo = repository if isinstance(repository, Mapping) else _detect_repository(project, runner, runner.git_path() is not None)
    return _parse_porcelain_status_z(project, repo, result.stdout)


def _status_snapshot(
    project: Path,
    runner: GitCommandRunner,
    repository: Mapping[str, Any],
    pathspecs: Sequence[str],
    workflow_id: str,
    task_id: str,
    run_id: str,
    run_kind: str,
    phase: str,
    generated_at: str,
    head: str | None,
) -> dict[str, Any]:
    result = _run_git(runner, project, ("status", "--porcelain=v1", "-z", "--", *pathspecs))
    entries = _parse_porcelain_status_z(project, repository, result.stdout) if result.returncode == 0 else []
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_kind": run_kind,
        "phase": phase,
        "head": head,
        "repository_root": _repository_root_for_record(project, repository),
        "dirty": bool(entries),
        "dirty_files_count": len(entries),
        "entries": entries,
        "path_policy": {
            "scope": "workspace_boundary",
            "pathspecs": list(pathspecs),
        },
    }
    if result.returncode != 0:
        snapshot["status_error"] = _compact_command_error(result)
    return snapshot


def _parse_porcelain_status_z(
    project: Path,
    repository: Mapping[str, Any],
    output: str,
) -> list[dict[str, Any]]:
    raw_entries = [entry for entry in output.split("\0") if entry]
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(raw_entries):
        raw = raw_entries[index]
        status = raw[:2]
        path = raw[3:] if len(raw) > 3 and raw[2] == " " else raw[2:].strip()
        normalized_path = _git_path_for_project_record(project, repository, path)
        item: dict[str, Any] = {
            "path": normalized_path or path,
            "index_status": status[0] if len(status) > 0 else "",
            "worktree_status": status[1] if len(status) > 1 else "",
            "change_type": _status_change_type(status),
        }
        if ("R" in status or "C" in status) and index + 1 < len(raw_entries):
            old_path = _git_path_for_project_record(project, repository, raw_entries[index + 1])
            item["old_path"] = old_path or raw_entries[index + 1]
            index += 2
        else:
            index += 1
        entries.append(item)
    return entries


def _status_change_type(status: str) -> str:
    if status == "??":
        return "untracked"
    if status == "!!":
        return "ignored"
    if "R" in status:
        return "renamed"
    if "C" in status:
        return "copied"
    if "A" in status:
        return "added"
    if "D" in status:
        return "deleted"
    if "T" in status:
        return "type_changed"
    if "U" in status:
        return "unmerged"
    return "modified"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_json_file(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, JSONDecodeError):
        return None


def _write_version_control_status(
    path: Path,
    checkpoint_record: dict[str, Any],
    repository: dict[str, Any],
    *,
    project: Path,
) -> None:
    read_model = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": checkpoint_record["workflow_id"],
        "status": "ok",
        "generated_at": _utc_now(),
        "provider": "git",
        "git_available": True,
        "repository": {
            "inside_work_tree": repository.get("inside_work_tree", False),
            "root": _repository_root_for_record(project, repository),
            "dirty": bool(checkpoint_record.get("status_entries_after")),
            "dirty_files_count": int(checkpoint_record.get("status_entries_after") or 0),
        },
        "dirty": bool(checkpoint_record.get("status_entries_after")),
        "dirty_files_count": int(checkpoint_record.get("status_entries_after") or 0),
        "path_policy": checkpoint_record.get("path_policy"),
        "problem": None,
        "latest_checkpoint": {
            "checkpoint_id": checkpoint_record["checkpoint_id"],
            "created_at": checkpoint_record["created_at"],
            "reason": checkpoint_record["reason"],
            "ref": checkpoint_record["ref"],
            "commit": checkpoint_record["commit"],
            "active_branch_unchanged": checkpoint_record["active_branch_unchanged"],
            "head_unchanged": checkpoint_record["head_unchanged"],
            "user_index_unchanged": checkpoint_record["user_index_unchanged"],
            "included_paths": checkpoint_record.get("included_paths", []),
            "excluded_paths": checkpoint_record.get("excluded_paths", []),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(read_model, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _load_version_control_config(config_path: Path, errors: list[str]) -> dict[str, Any] | None:
    if not config_path.is_file():
        errors.append(f"Missing version-control config: {config_path}.")
        return None
    try:
        with config_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except JSONDecodeError as error:
        errors.append(f"{config_path}: invalid JSON: {error.msg}.")
        return None
    except OSError as error:
        errors.append(f"{config_path}: unable to read config: {error}.")
        return None
    if not isinstance(data, dict):
        errors.append(f"{config_path}: version-control config must be a JSON object.")
        return None
    return data


def _detect_git(runner: GitCommandRunner) -> dict[str, Any]:
    executable = runner.git_path()
    if executable is None:
        return {"available": False, "executable": None, "version": None}
    result = runner.run(Path.cwd(), ("--version",))
    if result.returncode != 0:
        return {
            "available": False,
            "executable": executable,
            "version": None,
            "error": _compact_command_error(result),
        }
    return {
        "available": True,
        "executable": executable,
        "version": result.stdout.strip() or None,
    }


def _detect_repository(project: Path, runner: GitCommandRunner, git_available: bool) -> dict[str, Any]:
    repository = {
        "inside_work_tree": False,
        "root": None,
        "head_commit": None,
        "dirty": False,
        "dirty_files_count": 0,
    }
    if not git_available:
        return repository

    inside = runner.run(project, ("rev-parse", "--is-inside-work-tree"))
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return repository

    repository["inside_work_tree"] = True
    root = runner.run(project, ("rev-parse", "--show-toplevel"))
    if root.returncode == 0 and root.stdout.strip():
        repository["root"] = root.stdout.strip()

    head = runner.run(project, ("rev-parse", "--verify", "HEAD"))
    if head.returncode == 0 and head.stdout.strip():
        repository["head_commit"] = head.stdout.strip()

    status = runner.run(project, ("status", "--porcelain=v1", "-z"))
    if status.returncode == 0:
        dirty_count = _porcelain_z_entry_count(status.stdout)
        repository["dirty"] = dirty_count > 0
        repository["dirty_files_count"] = dirty_count
    return repository


def _empty_repository_status() -> dict[str, Any]:
    return {
        "inside_work_tree": False,
        "root": None,
        "head_commit": None,
        "dirty": False,
        "dirty_files_count": 0,
    }


def _git_init_status(
    status: str,
    git: dict[str, Any],
    repository: dict[str, Any],
    *,
    reason: str,
    message: str | None = None,
) -> dict[str, Any]:
    problem = None
    if status == "waiting_config":
        problem = {
            "code": "version_control_unavailable",
            "reason": reason,
            "message": message or "Version control is unavailable.",
        }
    return {
        "status": status,
        "ok": status == "ok",
        "reason": reason,
        "problem": problem,
        "git": git,
        "repository": repository,
    }


def _detect_local_init_capability(
    project: Path,
    git_available: bool,
    inside_work_tree: bool,
) -> dict[str, Any]:
    if inside_work_tree:
        return {"checked": True, "possible": False, "reason": "existing_repository"}
    if not git_available:
        return {"checked": True, "possible": False, "reason": "git_unavailable"}
    if not project.exists():
        return {"checked": True, "possible": False, "reason": "project_path_missing"}
    if not project.is_dir():
        return {"checked": True, "possible": False, "reason": "project_path_not_directory"}
    git_path = project / ".git"
    if git_path.exists():
        return {"checked": True, "possible": False, "reason": "unusable_git_path_exists"}
    if not os.access(project, os.W_OK | os.X_OK):
        return {"checked": True, "possible": False, "reason": "project_not_writable"}
    return {"checked": True, "possible": True, "reason": "git_init_available"}


def _configuration_summary(
    config_path: Path,
    config: dict[str, Any] | None,
    workflow_id: str | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "config_found": config is not None,
        "config_path": str(config_path),
        "enabled": None,
        "provider": None,
        "default_on": None,
        "user_configuration_required": None,
        "auto_init_if_missing": None,
        "repository_mode": None,
        "checkpoint_backend": None,
        "refs_namespace": None,
        "resolved_refs_namespace": None,
        "no_remote_push": None,
        "do_not_switch_user_branch": None,
        "do_not_modify_user_index": None,
        "checkpoint_policy": {},
        "run_metadata": {},
        "effective_worker_checkpointing": {
            "enabled": False,
            "reason": "config_missing",
        },
        "commit_policy": {},
    }
    if config is None:
        return summary

    for key in (
        "enabled",
        "provider",
        "default_on",
        "user_configuration_required",
        "auto_init_if_missing",
        "repository_mode",
        "checkpoint_backend",
        "refs_namespace",
        "no_remote_push",
        "do_not_switch_user_branch",
        "do_not_modify_user_index",
    ):
        summary[key] = config.get(key)

    refs_namespace = config.get("refs_namespace")
    if isinstance(refs_namespace, str):
        summary["resolved_refs_namespace"] = _resolve_refs_namespace(refs_namespace, workflow_id)

    commit_policy = config.get("commit_policy")
    if isinstance(commit_policy, dict):
        summary["commit_policy"] = {
            "checkpoint_protocol_files": commit_policy.get("checkpoint_protocol_files"),
            "checkpoint_project_changes": commit_policy.get("checkpoint_project_changes"),
            "write_to_user_branch": commit_policy.get("write_to_user_branch"),
            "require_approval_for_user_branch_commit": commit_policy.get(
                "require_approval_for_user_branch_commit"
            ),
        }
    checkpoint_policy = config.get("checkpoint_policy")
    if isinstance(checkpoint_policy, dict):
        summary["checkpoint_policy"] = {
            "before_worker_run": checkpoint_policy.get("before_worker_run"),
            "after_validation_pass": checkpoint_policy.get("after_validation_pass"),
            "before_plan_activation": checkpoint_policy.get("before_plan_activation"),
            "after_plan_activation": checkpoint_policy.get("after_plan_activation"),
            "before_final_completion": checkpoint_policy.get("before_final_completion"),
            "after_final_completion": checkpoint_policy.get("after_final_completion"),
        }
    run_metadata = config.get("run_metadata")
    if isinstance(run_metadata, dict):
        summary["run_metadata"] = {
            "enabled": run_metadata.get("enabled"),
            "detail_level": run_metadata.get("detail_level"),
        }
    summary["effective_worker_checkpointing"] = _effective_worker_checkpointing(config)
    return summary


def _effective_worker_checkpointing(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {"enabled": False, "reason": "config_missing"}
    checkpoint_policy = config.get("checkpoint_policy")
    before_worker = isinstance(checkpoint_policy, Mapping) and checkpoint_policy.get("before_worker_run") is True
    after_validation = isinstance(checkpoint_policy, Mapping) and checkpoint_policy.get("after_validation_pass") is True
    if not before_worker and not after_validation:
        return {"enabled": False, "reason": "checkpoint_policy_disabled"}
    return {
        "enabled": True,
        "reason": "checkpoint_policy_enabled",
        "configured_before_worker_run": before_worker,
        "configured_after_validation_pass": after_validation,
    }


def _checkpoint_policy_enabled(config: Mapping[str, Any] | None, key: str) -> bool:
    if not isinstance(config, Mapping):
        return False
    checkpoint_policy = config.get("checkpoint_policy")
    return isinstance(checkpoint_policy, Mapping) and checkpoint_policy.get(key) is True


def _configuration_warnings(config: dict[str, Any] | None) -> list[str]:
    if config is None:
        return []
    warnings: list[str] = []
    if config.get("enabled") is False:
        warnings.append("Version control is disabled; default Git checkpointing will not run.")
    if config.get("default_on") is not True:
        warnings.append("default_on is not true; LoopPlane Git checkpointing should be enabled by default.")
    if config.get("user_configuration_required") is not False:
        warnings.append("user_configuration_required is not false; default Git should not need manual setup.")
    if config.get("auto_init_if_missing") is not True:
        warnings.append("auto_init_if_missing is not true; missing repositories will not initialize automatically.")

    commit_policy = config.get("commit_policy")
    if isinstance(commit_policy, dict):
        if (
            commit_policy.get("require_approval_for_user_branch_commit") is True
            and commit_policy.get("write_to_user_branch") is True
        ):
            warnings.append("commit_policy.require_approval_for_user_branch_commit is true; unattended workflows should not require human approval for agent-managed commits.")
    else:
        warnings.append("commit_policy is missing or not a JSON object.")
    return warnings


def _configuration_errors(config: dict[str, Any] | None) -> list[str]:
    if config is None:
        return []
    errors: list[str] = []
    if config.get("provider") != SUPPORTED_PROVIDER:
        errors.append(f"Unsupported version-control provider: {config.get('provider')!r}; only 'git' is supported.")
    if config.get("repository_mode") != SUPPORTED_REPOSITORY_MODE:
        errors.append(
            f"Unsupported repository_mode: {config.get('repository_mode')!r}; "
            "expected 'existing_or_local_init'."
        )
    if config.get("checkpoint_backend") != SUPPORTED_CHECKPOINT_BACKEND:
        errors.append(
            f"Unsupported checkpoint_backend: {config.get('checkpoint_backend')!r}; expected 'managed_refs'."
        )
    refs_namespace = config.get("refs_namespace")
    if not isinstance(refs_namespace, str) or not _is_safe_refs_namespace(refs_namespace):
        errors.append("refs_namespace must resolve under refs/loopplane/<workflow_id> without unsafe path segments.")
    if config.get("no_remote_push") is not True:
        errors.append("Unsafe config: no_remote_push must be true.")
    if config.get("do_not_switch_user_branch") is not True:
        errors.append("Unsafe config: do_not_switch_user_branch must be true.")
    if config.get("do_not_modify_user_index") is not True:
        errors.append("Unsafe config: do_not_modify_user_index must be true.")

    commit_policy = config.get("commit_policy")
    if isinstance(commit_policy, dict) and commit_policy.get("write_to_user_branch") is not False:
        errors.append("Unsafe config: commit_policy.write_to_user_branch must be false.")
    return errors


def _resolve_refs_namespace(refs_namespace: str, workflow_id: str | None) -> str:
    return refs_namespace.replace("{{workflow_id}}", workflow_id or "<workflow_id>")


def _resolve_run_dir(project: Path, run_dir: Path) -> Path:
    expanded = run_dir.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (project / expanded).resolve()


def _relative_project_path(path: Path, project: Path) -> str | None:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return None


def _dir_prefix(path: str) -> str:
    return path if path.endswith("/") else f"{path}/"


def _new_checkpoint_id(created_at: str) -> str:
    timestamp = created_at.replace("-", "").replace(":", "").replace("Z", "Z")
    return f"cp_{timestamp}_{uuid.uuid4().hex[:8]}"


def _is_safe_refs_namespace(refs_namespace: str) -> bool:
    probe = _resolve_refs_namespace(refs_namespace, "wf_probe")
    if not probe.startswith(DEFAULT_REFS_PREFIX):
        return False
    unsafe_tokens = ("..", " ", "\t", "\n", "\\")
    return not any(token in probe for token in unsafe_tokens)


def _porcelain_z_entry_count(output: str) -> int:
    entries = [entry for entry in output.split("\0") if entry]
    count = 0
    index = 0
    while index < len(entries):
        entry = entries[index]
        count += 1
        status = entry[:2]
        if "R" in status or "C" in status:
            index += 2
        else:
            index += 1
    return count


def _compact_command_error(result: GitCommandResult) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[0] if text else f"exit {result.returncode}"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _yes_no(value: object) -> str:
    return "yes" if value is True else "no"
