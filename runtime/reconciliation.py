from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.active_projections import sync_active_workflow_projections
from runtime.adapters.base import utc_timestamp
from runtime.exit_codes import EXIT_GENERIC_FAILURE, EXIT_SUCCESS, EXIT_VALIDATION_FAILED, EXIT_WAITING_APPROVAL
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import is_task_block_terminator
from runtime.read_model_requests import READ_MODEL_REBUILD_REQUEST_FILENAME, request_read_model_rebuild
from runtime.scheduler import AtomicOwnerLock, append_event
from runtime.validation import PASSING_STATUSES
from runtime.workspace_boundary_policy import evaluate_worker_write_boundary, worker_write_boundary_message


SCHEMA_VERSION = "1.5"
VALIDATION_FILENAME = "validation.json"
FAILURE_REGISTRY_FILENAME = "failure_registry.json"
NODE_SUMMARY_FILENAME = "node_summary.json"
RECONCILER_NAME = "reconciler"
VALIDATION_STATUSES = frozenset({"pass", "pass_with_warnings", "fail", "blocked", "needs_human"})
TERMINAL_FAILURE_STATUSES = frozenset({"recovered", "waived", "exhausted", "needs_human"})
ACCEPTED_VALIDATION_RECOVERABLE_FAILURE_CLASSES = frozenset(
    {"background_job_failed", "validation_failed", "worker_failed"}
)
TASK_LINE_RE = re.compile(r"^- \[(?P<status>[ x~!\-])\]\s+(?P<task_id>[A-Za-z0-9_.-]+):\s+(?P<title>.+?)\s*$")
FIELD_LINE_RE = re.compile(r"^  - (?P<field>[A-Za-z0-9_ -]+):(?P<value>.*)$")
PLAN_PATCH_APPEND_BEGIN = "LOOPPLANE_PLAN_APPEND_BEGIN"
PLAN_PATCH_APPEND_END = "LOOPPLANE_PLAN_APPEND_END"
PLAN_PATCH_REPLACE_BEGIN = "LOOPPLANE_PLAN_REPLACE_BEGIN"
PLAN_PATCH_REPLACE_END = "LOOPPLANE_PLAN_REPLACE_END"
PLAN_PATCH_OPERATION_APPEND = "append_to_end"
PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE = "insert_task_into_phase"
PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES = "insert_phase_before_final_objectives"
PLAN_PATCH_OPERATION_REPLACE_TASKS = "replace_tasks"
PLAN_PATCH_OPERATIONS = frozenset(
    {
        PLAN_PATCH_OPERATION_APPEND,
        PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE,
        PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES,
        PLAN_PATCH_OPERATION_REPLACE_TASKS,
    }
)
FINAL_OBJECTIVE_HEADING_RE = re.compile(r"^##\s+Final Objective Checklist\s*$", re.IGNORECASE)


class ReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanTask:
    task_id: str
    status: str
    title: str
    line_index: int
    phase: str
    fields: Mapping[str, tuple[str, ...]]

    @property
    def latest_path(self) -> str | None:
        values = self.fields.get("latest") or self.fields.get("latest_pointer_path") or ()
        return values[0] if values else None

    @property
    def max_attempts(self) -> int:
        values = self.fields.get("max_attempts") or ()
        if not values:
            return 1
        try:
            return max(1, int(values[0]))
        except (TypeError, ValueError):
            return 1


def run_reconciler(
    project_root: Path | str,
    *,
    task_id: str | None = None,
    run_dir: Path | str | None = None,
    validation_path: Path | str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _load_failure(project=project, started_at=started_at, message=f"Unable to load workflow configuration: {error}")

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    resolved_validation_path = _resolve_validation_path(project, run_dir=run_dir, validation_path=validation_path)
    resolved_run_dir = _resolve_run_dir(project, run_dir=run_dir, validation_path=resolved_validation_path)
    validation, validation_problem = _read_validation(resolved_validation_path)
    if validation_problem is not None:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="invalid_validation",
            message=validation_problem,
        )

    primary_task_id = str(validation.get("primary_task_id") or validation.get("task_id") or task_id or "")
    if task_id is not None and primary_task_id and task_id != primary_task_id:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="invalid_validation",
            message=f"validation primary_task_id {primary_task_id!r} does not match requested task {task_id!r}",
        )
    if not primary_task_id:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="invalid_validation",
            message="validation.json is missing primary_task_id",
        )

    validation_status = str(validation.get("status") or "").lower()
    if validation_status not in VALIDATION_STATUSES:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="invalid_validation",
            message=f"validation status {validation_status!r} is not supported",
        )

    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
    except OSError as error:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="plan_unavailable",
            message=f"Unable to read PLAN.md: {error}",
        )
    tasks = parse_plan_tasks(plan_text)
    if primary_task_id not in tasks:
        return _no_mutation_result(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            status="plan_unavailable",
            message=f"Primary task {primary_task_id!r} was not found in PLAN.md",
        )

    if validation_status in PASSING_STATUSES:
        agent_status = _read_json_object(resolved_run_dir / "agent_status.json", default=None)
        boundary_policy = evaluate_worker_write_boundary(
            project,
            paths,
            task_id=primary_task_id,
            run_dir=resolved_run_dir,
            agent_status=agent_status if isinstance(agent_status, Mapping) else None,
        )
        if not boundary_policy.get("ok"):
            blocked = _no_mutation_result(
                project=project,
                workflow_id=workflow_id,
                started_at=started_at,
                validation_path=resolved_validation_path,
                run_dir=resolved_run_dir,
                status="workspace_boundary_violation",
                message=worker_write_boundary_message(boundary_policy),
                primary_task_id=primary_task_id,
                extra={"worker_write_boundary": boundary_policy},
            )
            return blocked
        accepted_task_ids, acceptance_warnings = _accepted_task_ids(validation)
        accepted_task_ids = [accepted for accepted in accepted_task_ids if accepted in tasks]
        result = _reconcile_pass(
            project=project,
            paths=paths,
            workflow_config=workflow_config,
            workflow_id=workflow_id,
            started_at=started_at,
            plan_text=plan_text,
            tasks=tasks,
            validation=validation,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            primary_task_id=primary_task_id,
            accepted_task_ids=accepted_task_ids,
            warnings=acceptance_warnings,
            write=write,
        )
    else:
        result = _reconcile_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            started_at=started_at,
            validation=validation,
            validation_path=resolved_validation_path,
            run_dir=resolved_run_dir,
            primary_task_id=primary_task_id,
            task=tasks[primary_task_id],
            write=write,
        )

    if write and result.get("ok"):
        _write_reconciliation_summary(project=project, run_dir=resolved_run_dir, result=result, validation=validation)
        if result.get("status") == "reconciled":
            if _auto_human_summaries_after_reconcile_enabled(workflow_config):
                try:
                    from runtime.human_summaries import ensure_human_summaries

                    result["human_summaries"] = ensure_human_summaries(
                        project,
                        task_ids=[str(task_id) for task_id in result.get("accepted_task_ids") or []],
                        write=True,
                        blocking=False,
                    )
                except Exception as error:  # pragma: no cover - summaries are advisory, reconciliation is authoritative.
                    result["human_summaries"] = {
                        "ok": False,
                        "status": "summary_failed",
                        "errors": [str(error)],
                        "warnings": ["Task reconciliation succeeded, but human-readable summary generation failed."],
                    }
            else:
                result["human_summaries"] = {
                    "ok": True,
                    "status": "deferred",
                    "reason": "auto_after_reconcile_disabled",
                    "task_ids": [str(task_id) for task_id in result.get("accepted_task_ids") or []],
                    "warnings": ["Human summaries were deferred; run summarize to generate them on demand."],
                }
    return result


def reconciliation_exit_code(result: Mapping[str, Any]) -> int:
    status = str(result.get("status") or "")
    if status == "reconciled" or (bool(result.get("ok")) and status in {"applied", "validated"}):
        return EXIT_SUCCESS
    if status == "needs_human":
        return EXIT_WAITING_APPROVAL
    if status == "validation_failed":
        return EXIT_VALIDATION_FAILED
    return EXIT_GENERIC_FAILURE


def format_reconciliation_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane reconciliation: {result.get('status', 'unknown')}",
        f"primary_task_id: {result.get('primary_task_id', 'unknown')}",
        f"run_id: {result.get('run_id', 'unknown')}",
    ]
    accepted = result.get("accepted_task_ids")
    if isinstance(accepted, Sequence) and not isinstance(accepted, (str, bytes)):
        lines.append("accepted_task_ids: " + ", ".join(str(item) for item in accepted))
    failure = result.get("failure_registry_update")
    if isinstance(failure, Mapping):
        lines.append(f"failure_id: {failure.get('failure_id', 'unknown')}")
        lines.append(f"failure_status: {failure.get('status', 'unknown')}")
    approval = result.get("approval_request")
    if isinstance(approval, Mapping):
        lines.append(f"approval_id: {approval.get('approval_id', 'unknown')}")
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and warnings and not isinstance(warnings, (str, bytes)):
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and errors and not isinstance(errors, (str, bytes)):
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def apply_approved_plan_patch(
    project_root: Path | str,
    *,
    change_request_id: str,
    plan_patch_path: Path | str,
    response_id: str = "",
    approval_request_id: str = "",
    before_checkpoint_id: str | None = None,
    plan_patch_operation: str = PLAN_PATCH_OPERATION_APPEND,
    target_phase_id: str | None = None,
    supersede_task_ids: Sequence[str] = (),
    supersede_reason: str = "",
    supersede_authorization: str = "",
    expected_plan_patch_sha256: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _load_failure(project=project, started_at=started_at, message=f"Unable to load workflow configuration: {error}")

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    patch_path = Path(plan_patch_path).expanduser()
    if not patch_path.is_absolute():
        patch_path = project / patch_path
    patch_path = patch_path.resolve()
    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
        patch_text = patch_path.read_text(encoding="utf-8")
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "unavailable",
            "workflow_id": workflow_id,
            "change_request_id": change_request_id,
            "plan_patch_path": _path_for_record(project, patch_path),
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "added_tasks": [],
            "modified_tasks": [],
            "added_phase_ids": [],
            "plan_patch_operation": str(plan_patch_operation or PLAN_PATCH_OPERATION_APPEND).strip(),
            "event_id": None,
            "errors": [str(error)],
            "warnings": [],
        }

    expected_patch_sha256 = str(expected_plan_patch_sha256 or "").strip()
    actual_patch_sha256 = "sha256:" + sha256(patch_text.encode("utf-8")).hexdigest()
    if expected_patch_sha256 and actual_patch_sha256 != expected_patch_sha256:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="plan_patch_content_changed",
            errors=[
                "PLAN_PATCH.md content changed after validation or approval; "
                f"expected {expected_patch_sha256}, observed {actual_patch_sha256}."
            ],
            plan_patch_operation=str(plan_patch_operation or PLAN_PATCH_OPERATION_APPEND).strip(),
        )

    operation = str(plan_patch_operation or PLAN_PATCH_OPERATION_APPEND).strip()
    if operation not in PLAN_PATCH_OPERATIONS:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="invalid_plan_patch",
            errors=[f"PLAN_PATCH.md operation must be one of: {', '.join(sorted(PLAN_PATCH_OPERATIONS))}."],
            plan_patch_operation=operation,
        )

    existing_tasks = parse_plan_tasks(plan_text)
    block: str | None
    block_error: str | None
    if operation == PLAN_PATCH_OPERATION_REPLACE_TASKS:
        block, block_error = _extract_plan_patch_replace_block(patch_text)
        if block_error and PLAN_PATCH_REPLACE_BEGIN not in patch_text and PLAN_PATCH_REPLACE_END not in patch_text:
            block, block_error = _extract_legacy_plan_patch_replace_block(patch_text)
    else:
        block, block_error = _extract_plan_patch_append_block(patch_text)
    if block_error:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="invalid_plan_patch",
            errors=[block_error],
            plan_patch_operation=operation,
        )
    assert block is not None

    patch_task_blocks = _task_block_texts(block)
    patch_task_ids = [task_id for task_id, _task_block in patch_task_blocks]
    duplicate_patch_ids = sorted({task_id for task_id in patch_task_ids if patch_task_ids.count(task_id) > 1})
    if duplicate_patch_ids:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="invalid_plan_patch",
            errors=[f"PLAN_PATCH.md contains duplicate task IDs: {', '.join(duplicate_patch_ids)}"],
            plan_patch_operation=operation,
        )
    patch_tasks = parse_plan_tasks(block)
    if not patch_tasks:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="invalid_plan_patch",
            errors=["PLAN_PATCH.md task block does not contain any tasks."],
            plan_patch_operation=operation,
        )

    structural_errors: list[str] = []
    added_phase_ids: list[str] = []
    added_tasks: list[str] = []
    modified_tasks: list[str] = []
    normalized_supersede_ids = list(dict.fromkeys(str(item).strip() for item in supersede_task_ids if str(item).strip()))
    unknown_supersede_ids = sorted(set(normalized_supersede_ids) - set(existing_tasks))
    if unknown_supersede_ids:
        structural_errors.append(
            "supersede_task_ids references task IDs not present in the active plan: " + ", ".join(unknown_supersede_ids)
        )
    patch_supersede_overlap = sorted(set(normalized_supersede_ids).intersection(patch_tasks))
    if patch_supersede_overlap:
        structural_errors.append(
            "PLAN_PATCH.md cannot add and supersede the same task IDs: " + ", ".join(patch_supersede_overlap)
        )
    normalized_target_phase_id = str(target_phase_id or "").strip()
    patch_phase_headings = _phase_headings(block)
    existing_phase_ids = {phase_id for _line_index, phase_id, _phase_title in _phase_headings(plan_text) if phase_id}
    if operation == PLAN_PATCH_OPERATION_REPLACE_TASKS:
        missing_task_ids = sorted(set(patch_tasks).difference(existing_tasks))
        if missing_task_ids:
            structural_errors.append(
                "replace_tasks PLAN_PATCH.md may only target existing task IDs; missing: " + ", ".join(missing_task_ids)
            )
        existing_task_ids = [task_id for task_id, _task_block in _task_block_texts(plan_text)]
        ambiguous_task_ids = sorted(
            task_id for task_id in patch_tasks if existing_task_ids.count(task_id) != 1
        )
        if ambiguous_task_ids:
            structural_errors.append(
                "replace_tasks target IDs must occur exactly once in PLAN.md: " + ", ".join(ambiguous_task_ids)
            )
        if patch_phase_headings:
            structural_errors.append("replace_tasks PLAN_PATCH.md replacement block must not contain phase headings.")
        declared_plan_sha = _declared_target_plan_sha256(patch_text)
        current_plan_sha = "sha256:" + sha256(plan_text.encode("utf-8")).hexdigest()
        if declared_plan_sha and declared_plan_sha != current_plan_sha:
            return _plan_patch_failure(
                project=project,
                workflow_id=workflow_id,
                change_request_id=change_request_id,
                patch_path=patch_path,
                started_at=started_at,
                status="stale_plan_patch",
                errors=[
                    f"PLAN_PATCH.md targets PLAN hash {declared_plan_sha}, but the active PLAN hash is {current_plan_sha}."
                ],
                plan_patch_operation=operation,
            )
        modified_tasks = list(patch_tasks)
    else:
        duplicate_ids = sorted(set(existing_tasks).intersection(patch_tasks))
        if duplicate_ids:
            return _plan_patch_failure(
                project=project,
                workflow_id=workflow_id,
                change_request_id=change_request_id,
                patch_path=patch_path,
                started_at=started_at,
                status="duplicate_task_ids",
                errors=[f"PLAN_PATCH.md would duplicate existing task IDs: {', '.join(duplicate_ids)}"],
                plan_patch_operation=operation,
            )
        if "## Phase " not in block:
            structural_errors.append("PLAN_PATCH.md append block must include a phase heading.")
        added_tasks = list(patch_tasks)

    if operation == PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE:
        if not normalized_target_phase_id:
            structural_errors.append("insert_task_into_phase requires target_phase_id.")
        if len(patch_phase_headings) != 1:
            structural_errors.append("insert_task_into_phase PLAN_PATCH.md must contain exactly one target phase heading.")
        patch_phase_ids = {phase_id for _line_index, phase_id, _phase_title in patch_phase_headings if phase_id}
        if normalized_target_phase_id and patch_phase_ids != {normalized_target_phase_id}:
            structural_errors.append(
                "insert_task_into_phase PLAN_PATCH.md phase heading must match target_phase_id "
                f"{normalized_target_phase_id}."
            )
        if normalized_target_phase_id and normalized_target_phase_id not in existing_phase_ids:
            structural_errors.append(f"insert_task_into_phase target phase {normalized_target_phase_id} was not found in PLAN.md.")
        task_blocks = _task_blocks_for_phase(block, phase_id=normalized_target_phase_id)
        if not task_blocks:
            structural_errors.append(f"insert_task_into_phase found no task blocks for target phase {normalized_target_phase_id}.")
    elif operation == PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES:
        if len(patch_phase_headings) != 1:
            structural_errors.append("insert_phase_before_final_objectives PLAN_PATCH.md must contain exactly one new phase heading.")
        patch_phase_ids = [phase_id for _line_index, phase_id, _phase_title in patch_phase_headings if phase_id]
        if len(patch_phase_ids) != 1:
            structural_errors.append("insert_phase_before_final_objectives PLAN_PATCH.md must declare a non-empty new phase id.")
        duplicate_phase_ids = sorted(set(patch_phase_ids).intersection(existing_phase_ids))
        if duplicate_phase_ids:
            structural_errors.append(
                "insert_phase_before_final_objectives must create a new phase; duplicate phase id(s): "
                + ", ".join(duplicate_phase_ids)
            )
        if any(FINAL_OBJECTIVE_HEADING_RE.match(line) for line in block.splitlines()):
            structural_errors.append("insert_phase_before_final_objectives PLAN_PATCH.md must not include the Final Objective Checklist.")
        if _final_objective_heading_index(plan_text.splitlines()) is None:
            structural_errors.append("insert_phase_before_final_objectives requires PLAN.md to contain a Final Objective Checklist.")
        added_phase_ids = patch_phase_ids
    if structural_errors:
        return _plan_patch_failure(
            project=project,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            started_at=started_at,
            status="invalid_plan_patch",
            errors=structural_errors,
            plan_patch_operation=operation,
        )

    event: dict[str, Any] | None = None
    superseded_tasks: list[str] = []
    already_completed_superseded_tasks: list[str] = []
    recovered_superseded_failure_ids: list[str] = []
    if write:
        if operation == PLAN_PATCH_OPERATION_REPLACE_TASKS:
            updated_plan = _replace_plan_task_blocks(plan_text, block)
        elif operation == PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE:
            updated_plan = _insert_plan_patch_tasks_into_phase(plan_text, block, phase_id=normalized_target_phase_id)
        elif operation == PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES:
            updated_plan = _insert_plan_patch_phase_before_final_objectives(plan_text, block)
        else:
            updated_plan = _append_plan_patch_block(plan_text, block)
        if normalized_supersede_ids:
            updated_plan, superseded_tasks, already_completed_superseded_tasks = _mark_plan_tasks_superseded(
                updated_plan,
                task_ids=normalized_supersede_ids,
                reason=supersede_reason or f"Superseded by approved change request {change_request_id}.",
                authorization=supersede_authorization or f"change_request:{change_request_id}",
            )
        _atomic_write_text(paths.plan_file, updated_plan)
        if normalized_supersede_ids:
            recovered_superseded_failure_ids = _mark_superseded_failures_recovered(
                paths,
                workflow_id=workflow_id,
                task_ids=normalized_supersede_ids,
                change_request_id=change_request_id,
            )
        event = append_event(
            paths,
            workflow_id=workflow_id,
            run_id=change_request_id,
            event_type="change_request_plan_patch_applied",
            data={
                "request_id": change_request_id,
                "change_request_id": change_request_id,
                "response_id": response_id,
                "approval_id": approval_request_id,
                "before_checkpoint_id": before_checkpoint_id,
                "plan_patch_file": _path_for_record(project, patch_path),
                "added_tasks": added_tasks,
                "modified_tasks": modified_tasks,
                "added_phase_ids": added_phase_ids,
                "plan_patch_operation": operation,
                "target_phase_id": normalized_target_phase_id or None,
                "superseded_tasks": superseded_tasks,
                "already_completed_superseded_tasks": already_completed_superseded_tasks,
                "recovered_superseded_failure_ids": recovered_superseded_failure_ids,
                "updated_by": RECONCILER_NAME,
            },
        )
        _update_runtime_state(
            paths,
            status="plan_updated",
            update={
                "workflow_id": workflow_id,
                "last_action": "apply_change_request_plan_patch",
                "last_change_request_id": change_request_id,
                "last_plan_patch_path": _path_for_record(project, patch_path),
                "last_added_task_ids": added_tasks,
                "last_modified_task_ids": modified_tasks,
                "last_added_phase_ids": added_phase_ids,
                "last_plan_patch_operation": operation,
                "last_plan_update_event_id": event.get("event_id"),
                "last_superseded_task_ids": superseded_tasks,
                "last_recovered_failure_ids": recovered_superseded_failure_ids,
            },
            clear_manual_plan_change=True,
        )
        _request_read_model_rebuild(
            paths,
            workflow_id=workflow_id,
            run_id=change_request_id,
            reason="change_request_plan_patch_applied",
            validation_path=patch_path,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "applied" if write else "validated",
        "workflow_id": workflow_id,
        "change_request_id": change_request_id,
        "plan_patch_path": _path_for_record(project, patch_path),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "added_tasks": added_tasks,
        "modified_tasks": modified_tasks,
        "added_phase_ids": added_phase_ids,
        "superseded_tasks": superseded_tasks,
        "already_completed_superseded_tasks": already_completed_superseded_tasks,
        "recovered_superseded_failure_ids": recovered_superseded_failure_ids,
        "plan_patch_operation": operation,
        "target_phase_id": normalized_target_phase_id or None,
        "event_id": event.get("event_id") if event is not None else None,
        "errors": [],
        "warnings": [],
    }


def parse_plan_tasks(plan_text: str) -> dict[str, PlanTask]:
    lines = plan_text.splitlines()
    tasks: dict[str, PlanTask] = {}
    current_phase = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("## Phase "):
            current_phase = line[3:].strip()
        match = TASK_LINE_RE.match(line)
        if not match:
            index += 1
            continue
        task_id = match.group("task_id")
        start = index
        index += 1
        while index < len(lines):
            if (
                TASK_LINE_RE.match(lines[index])
                or re.match(r"^#{1,6}\s", lines[index])
                or is_task_block_terminator(lines[index])
            ):
                break
            index += 1
        fields = _task_fields(lines[start:index])
        tasks[task_id] = PlanTask(
            task_id=task_id,
            status=match.group("status"),
            title=match.group("title").strip(),
            line_index=start,
            phase=current_phase,
            fields=fields,
        )
    return tasks


def _mark_plan_tasks_superseded(
    plan_text: str,
    *,
    task_ids: Sequence[str],
    reason: str,
    authorization: str,
) -> tuple[str, list[str], list[str]]:
    lines = plan_text.splitlines()
    tasks = parse_plan_tasks(plan_text)
    superseded: list[str] = []
    already_completed: list[str] = []
    reason_text = " ".join(str(reason).split())
    authorization_text = " ".join(str(authorization).split())
    selected = sorted(
        (tasks[task_id] for task_id in task_ids if task_id in tasks),
        key=lambda task: task.line_index,
        reverse=True,
    )
    for task in selected:
        if task.status == "x":
            already_completed.append(task.task_id)
            continue
        start = task.line_index
        end = start + 1
        while end < len(lines):
            if TASK_LINE_RE.match(lines[end]) or lines[end].startswith("## Phase ") or is_task_block_terminator(lines[end]):
                break
            end += 1
        lines[start] = re.sub(r"^- \[[ x~!\-]\]", "- [-]", lines[start], count=1)
        fields = _task_fields(lines[start:end])
        additions: list[str] = []
        if not fields.get("skip_reason"):
            additions.append(f"  - skip_reason: {reason_text}")
        if not (fields.get("skip_authorization") or fields.get("approval_id")):
            additions.append(f"  - skip_authorization: {authorization_text}")
        if additions:
            lines[end:end] = additions
        superseded.append(task.task_id)
    suffix = "\n" if plan_text.endswith("\n") else ""
    return "\n".join(lines) + suffix, sorted(superseded), sorted(already_completed)


def _mark_superseded_failures_recovered(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    task_ids: Sequence[str],
    change_request_id: str,
) -> list[str]:
    target_ids = {str(task_id) for task_id in task_ids if str(task_id)}

    def update(registry: dict[str, Any]) -> list[str]:
        recovered: list[str] = []
        resolved_at = utc_timestamp()
        for failure in registry.get("failures", []):
            if not isinstance(failure, dict) or str(failure.get("task_id") or "") not in target_ids:
                continue
            if str(failure.get("status") or "").lower() in {"recovered", "waived"}:
                continue
            failure["status"] = "recovered"
            failure["recoverable"] = False
            failure["recovered_at"] = resolved_at
            failure["resolution"] = "superseded_by_approved_change_request"
            failure["resolution_change_request_id"] = change_request_id
            failure_id = str(failure.get("failure_id") or "")
            if failure_id:
                recovered.append(failure_id)
        return sorted(recovered)

    return _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)


def _reconcile_pass(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_config: Mapping[str, Any],
    workflow_id: str,
    started_at: str,
    plan_text: str,
    tasks: Mapping[str, PlanTask],
    validation: Mapping[str, Any],
    validation_path: Path,
    run_dir: Path,
    primary_task_id: str,
    accepted_task_ids: Sequence[str],
    warnings: Sequence[str],
    write: bool,
) -> dict[str, Any]:
    now = utc_timestamp()
    errors: list[str] = []
    blocked_task_ids: list[str] = []
    writable_task_ids: list[str] = []
    for accepted in accepted_task_ids:
        task = tasks.get(accepted)
        if task is None:
            errors.append(f"Accepted task {accepted!r} was not found in PLAN.md.")
            continue
        if task.status == "~" and not _task_approval_granted(paths, accepted):
            blocked_task_ids.append(accepted)
            errors.append(f"Accepted task {accepted!r} is partial and lacks explicit approval.")
            continue
        if task.status in {"!", "-"}:
            blocked_task_ids.append(accepted)
            errors.append(f"Accepted task {accepted!r} has PLAN.md status {task.status!r} and cannot be reconciled complete.")
            continue
        writable_task_ids.append(accepted)

    if primary_task_id not in writable_task_ids:
        errors.append(f"Primary task {primary_task_id!r} was not accepted for reconciliation.")

    updated_plan_task_ids: list[str] = []
    latest_updates: list[dict[str, Any]] = []
    recovered_failure_updates: list[dict[str, Any]] = []
    projection_sync: dict[str, Any] | None = None
    if write and not errors:
        updated_plan, updated_plan_task_ids = _mark_plan_tasks_complete(plan_text, tasks, writable_task_ids)
        if updated_plan != plan_text:
            _atomic_write_text(paths.plan_file, updated_plan)
        for accepted in writable_task_ids:
            task = tasks[accepted]
            latest_path = _latest_path(project, paths, task)
            latest_record = _latest_record(
                project=project,
                task_id=accepted,
                run_dir=run_dir,
                validation_path=validation_path,
                validation=validation,
                updated_at=now,
            )
            _atomic_write_json(latest_path, latest_record)
            latest_updates.append({"task_id": accepted, "latest_path": _path_for_record(project, latest_path), **latest_record})

        append_event(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            event_type="validation_passed",
            data={
                "primary_task_id": primary_task_id,
                "accepted_task_ids": list(writable_task_ids),
                "validation_path": _path_for_record(project, validation_path),
            },
        )
        for accepted in writable_task_ids:
            if accepted != primary_task_id:
                append_event(
                    paths,
                    workflow_id=workflow_id,
                    run_id=str(validation.get("run_id") or run_dir.name),
                    event_type="task_absorbed",
                    data={
                        "primary_task_id": primary_task_id,
                        "task_id": accepted,
                        "validation_path": _path_for_record(project, validation_path),
                    },
                )
            append_event(
                paths,
                workflow_id=workflow_id,
                run_id=str(validation.get("run_id") or run_dir.name),
                event_type="plan_updated",
                data={
                    "task_id": accepted,
                    "new_status": "x",
                    "updated_by": RECONCILER_NAME,
                    "validation_path": _path_for_record(project, validation_path),
                },
            )
        recovered_failure_updates = _mark_accepted_validation_failures_recovered(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            accepted_task_ids=writable_task_ids,
            run_id=str(validation.get("run_id") or run_dir.name),
            validation_path=validation_path,
            recovered_at=now,
        )
        if recovered_failure_updates:
            append_event(
                paths,
                workflow_id=workflow_id,
                run_id=str(validation.get("run_id") or run_dir.name),
                event_type="failure_registry_updated",
                data={
                    "source": "reconciler",
                    "status": "recovered",
                    "recovered_failure_ids": [
                        str(failure.get("failure_id") or "") for failure in recovered_failure_updates
                    ],
                    "accepted_task_ids": list(writable_task_ids),
                },
            )
        _update_runtime_state(
            paths,
            status="reconciled",
            update={
                "last_action": "reconcile_validation",
                "last_validation_status": validation.get("status"),
                "last_task_id": primary_task_id,
                "last_run_id": validation.get("run_id") or run_dir.name,
                "last_accepted_task_ids": list(writable_task_ids),
                "last_recovered_failure_ids": [
                    str(failure.get("failure_id") or "") for failure in recovered_failure_updates
                ],
            },
        )
        _request_read_model_rebuild(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            reason="validation_passed",
            validation_path=validation_path,
        )
        projection_sync = sync_active_workflow_projections(
            project,
            workflow_config,
            paths,
            reason="reconcile_validation",
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "status": "reconciled" if not errors else "blocked",
        "workflow_id": workflow_id,
        "primary_task_id": primary_task_id,
        "run_id": str(validation.get("run_id") or run_dir.name),
        "validation_status": validation.get("status"),
        "validation_path": _path_for_record(project, validation_path),
        "run_dir": _path_for_record(project, run_dir),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "accepted_task_ids": list(writable_task_ids),
        "blocked_task_ids": blocked_task_ids,
        "updated_plan_task_ids": updated_plan_task_ids,
        "latest_updates": latest_updates,
        "failure_registry_updates": recovered_failure_updates,
        "recovered_failure_ids": [
            str(failure.get("failure_id") or "") for failure in recovered_failure_updates
        ],
        "projection_sync": projection_sync,
        "warnings": list(warnings),
        "errors": errors,
    }


def _reconcile_failure(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    started_at: str,
    validation: Mapping[str, Any],
    validation_path: Path,
    run_dir: Path,
    primary_task_id: str,
    task: PlanTask,
    write: bool,
) -> dict[str, Any]:
    failure_update: dict[str, Any] | None = None
    if write:
        failure_update = _record_validation_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            validation=validation,
            validation_path=validation_path,
            run_dir=run_dir,
            primary_task_id=primary_task_id,
            task=task,
        )
        append_event(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            event_type="validation_failed",
            data={
                "primary_task_id": primary_task_id,
                "validation_status": validation.get("status"),
                "validation_path": _path_for_record(project, validation_path),
                "failure_id": failure_update.get("failure_id") if failure_update else None,
            },
        )
        append_event(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            event_type="failure_registry_updated",
            data={
                "source": "reconciler",
                "failure_id": failure_update.get("failure_id") if failure_update else None,
                "status": failure_update.get("status") if failure_update else None,
                "budget_remaining": failure_update.get("budget_remaining") if failure_update else None,
            },
        )
        _update_runtime_state(
            paths,
            status="recovery_pending" if failure_update and failure_update.get("budget_remaining") else "recovery_exhausted",
            update={
                "last_action": "reconcile_validation",
                "last_validation_status": validation.get("status"),
                "last_task_id": primary_task_id,
                "last_run_id": validation.get("run_id") or run_dir.name,
                "last_failure_id": failure_update.get("failure_id") if failure_update else None,
            },
        )
        _request_read_model_rebuild(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            reason="validation_failed",
            validation_path=validation_path,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "validation_failed",
        "workflow_id": workflow_id,
        "primary_task_id": primary_task_id,
        "run_id": str(validation.get("run_id") or run_dir.name),
        "validation_status": validation.get("status"),
        "validation_path": _path_for_record(project, validation_path),
        "run_dir": _path_for_record(project, run_dir),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "accepted_task_ids": [],
        "failure_registry_update": failure_update or {},
        "warnings": [],
        "errors": [],
    }


def _reconcile_needs_human(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    started_at: str,
    validation: Mapping[str, Any],
    validation_path: Path,
    run_dir: Path,
    primary_task_id: str,
    write: bool,
) -> dict[str, Any]:
    approval_request: dict[str, Any] | None = None
    if write:
        approval_request = _record_approval_request(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            validation=validation,
            validation_path=validation_path,
            run_dir=run_dir,
            primary_task_id=primary_task_id,
        )
        append_event(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            event_type="approval_requested",
            data={
                "primary_task_id": primary_task_id,
                "approval_id": approval_request.get("approval_id") if approval_request else None,
                "validation_path": _path_for_record(project, validation_path),
            },
        )
        _update_runtime_state(
            paths,
            status="waiting_approval",
            update={
                "last_action": "reconcile_validation",
                "last_validation_status": validation.get("status"),
                "last_task_id": primary_task_id,
                "last_run_id": validation.get("run_id") or run_dir.name,
                "last_approval_id": approval_request.get("approval_id") if approval_request else None,
            },
        )
        _request_read_model_rebuild(
            paths,
            workflow_id=workflow_id,
            run_id=str(validation.get("run_id") or run_dir.name),
            reason="approval_requested",
            validation_path=validation_path,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "needs_human",
        "workflow_id": workflow_id,
        "primary_task_id": primary_task_id,
        "run_id": str(validation.get("run_id") or run_dir.name),
        "validation_status": validation.get("status"),
        "validation_path": _path_for_record(project, validation_path),
        "run_dir": _path_for_record(project, run_dir),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "accepted_task_ids": [],
        "approval_request": approval_request or {},
        "warnings": list(validation.get("warnings") or []),
        "errors": [],
    }


def _accepted_task_ids(validation: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    task_statuses: dict[str, str] = {}
    task_results = validation.get("task_results")
    if isinstance(task_results, Sequence) and not isinstance(task_results, (str, bytes)):
        for result in task_results:
            if not isinstance(result, Mapping):
                continue
            task_id = result.get("task_id")
            if isinstance(task_id, str) and task_id:
                task_statuses[task_id] = str(result.get("status") or "").lower()

    accepted: list[str] = []
    warnings: list[str] = []
    raw_ids = validation.get("accepted_task_ids")
    if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
        return [], ["validation.accepted_task_ids is missing or not a list."]
    for raw_id in raw_ids:
        task_id = str(raw_id or "").strip()
        if not task_id or task_id in accepted:
            continue
        result_status = task_statuses.get(task_id)
        if result_status is not None and result_status not in PASSING_STATUSES:
            warnings.append(f"Accepted task {task_id!r} has non-passing task_results status {result_status!r}; ignored.")
            continue
        accepted.append(task_id)
    return accepted, warnings


def _extract_plan_patch_append_block(patch_text: str) -> tuple[str | None, str | None]:
    start = patch_text.find(PLAN_PATCH_APPEND_BEGIN)
    end = patch_text.find(PLAN_PATCH_APPEND_END)
    if start < 0 or end < 0 or end <= start:
        return None, f"PLAN_PATCH.md must contain {PLAN_PATCH_APPEND_BEGIN} and {PLAN_PATCH_APPEND_END} markers."
    block = patch_text[start + len(PLAN_PATCH_APPEND_BEGIN) : end].strip()
    if not block:
        return None, "PLAN_PATCH.md append block is empty."
    return block + "\n", None


def _extract_plan_patch_replace_block(patch_text: str) -> tuple[str | None, str | None]:
    start = patch_text.find(PLAN_PATCH_REPLACE_BEGIN)
    end = patch_text.find(PLAN_PATCH_REPLACE_END)
    if start < 0 or end < 0 or end <= start:
        return None, f"PLAN_PATCH.md must contain {PLAN_PATCH_REPLACE_BEGIN} and {PLAN_PATCH_REPLACE_END} markers."
    block = patch_text[start + len(PLAN_PATCH_REPLACE_BEGIN) : end].strip()
    if not block:
        return None, "PLAN_PATCH.md replacement block is empty."
    return block + "\n", None


def _extract_legacy_plan_patch_replace_block(patch_text: str) -> tuple[str | None, str | None]:
    declared_modify = re.search(
        r"(?im)^(?:\s*-\s*)?operation:\s*.*\b(?:modify|replace)\b|^##\s+(?:modify|replace)\b",
        patch_text,
    )
    if declared_modify is None or PLAN_PATCH_APPEND_BEGIN in patch_text or PLAN_PATCH_APPEND_END in patch_text:
        return None, (
            f"PLAN_PATCH.md must contain {PLAN_PATCH_REPLACE_BEGIN} and {PLAN_PATCH_REPLACE_END} markers; "
            "legacy replacement extraction requires an explicit MODIFY or REPLACE declaration and no append markers."
        )
    task_blocks = _task_block_texts(patch_text)
    if not task_blocks:
        return None, "Legacy replacement PLAN_PATCH.md does not contain a complete task block."
    return "\n\n".join(block for _task_id, block in task_blocks).rstrip() + "\n", None


def _declared_target_plan_sha256(patch_text: str) -> str | None:
    match = re.search(
        r"(?im)^\s*-\s*target_plan_sha256:\s*(?:sha256:)?(?P<digest>[0-9a-f]{64})\s*$",
        patch_text,
    )
    if match is None:
        return None
    return "sha256:" + match.group("digest").lower()


def _task_block_texts(plan_text: str) -> list[tuple[str, str]]:
    lines = plan_text.splitlines()
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        match = TASK_LINE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        start = index
        index += 1
        while index < len(lines):
            if (
                TASK_LINE_RE.match(lines[index])
                or re.match(r"^#{1,6}\s", lines[index])
                or is_task_block_terminator(lines[index])
            ):
                break
            index += 1
        content_end = index
        while content_end > start and not lines[content_end - 1].strip():
            content_end -= 1
        blocks.append((match.group("task_id"), "\n".join(lines[start:content_end]).rstrip()))
    return blocks


def _append_plan_patch_block(plan_text: str, append_block: str) -> str:
    base = plan_text.rstrip()
    return f"{base}\n\n{append_block.rstrip()}\n"


def _replace_plan_task_blocks(plan_text: str, replacement_block: str) -> str:
    replacements = {task_id: block for task_id, block in _task_block_texts(replacement_block)}
    lines = plan_text.splitlines()
    ranges: list[tuple[int, int, str]] = []
    seen: dict[str, int] = {}
    index = 0
    while index < len(lines):
        match = TASK_LINE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        task_id = match.group("task_id")
        start = index
        index += 1
        while index < len(lines):
            if (
                TASK_LINE_RE.match(lines[index])
                or re.match(r"^#{1,6}\s", lines[index])
                or is_task_block_terminator(lines[index])
            ):
                break
            index += 1
        content_end = index
        while content_end > start and not lines[content_end - 1].strip():
            content_end -= 1
        if task_id in replacements:
            seen[task_id] = seen.get(task_id, 0) + 1
            ranges.append((start, content_end, task_id))
    invalid = sorted(task_id for task_id in replacements if seen.get(task_id, 0) != 1)
    if invalid:
        raise ReconciliationError("Replacement task IDs must occur exactly once in PLAN.md: " + ", ".join(invalid))
    for start, end, task_id in sorted(ranges, reverse=True):
        lines[start:end] = replacements[task_id].splitlines()
    trailing_newline = "\n" if plan_text.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline


def _insert_plan_patch_tasks_into_phase(plan_text: str, append_block: str, *, phase_id: str) -> str:
    task_block = _task_blocks_for_phase(append_block, phase_id=phase_id)
    lines = plan_text.splitlines()
    bounds = _phase_bounds(lines, phase_id=phase_id)
    if bounds is None:
        return plan_text
    _phase_start, phase_end = bounds
    insert_index = phase_end
    for index in range(_phase_start + 1, phase_end):
        if re.match(r"^###\s+Phase Objective Checklist\s*$", lines[index], flags=re.IGNORECASE):
            insert_index = index
            break
    return _insert_block_at_line(plan_text, insert_index, task_block)


def _insert_plan_patch_phase_before_final_objectives(plan_text: str, append_block: str) -> str:
    lines = plan_text.splitlines()
    insert_index = _final_objective_heading_index(lines)
    if insert_index is None:
        insert_index = len(lines)
    return _insert_block_at_line(plan_text, insert_index, append_block)


def _insert_block_at_line(plan_text: str, insert_index: int, block: str) -> str:
    lines = plan_text.splitlines()
    block_lines = block.strip().splitlines()
    insertion: list[str] = []
    if insert_index > 0 and lines[insert_index - 1].strip():
        insertion.append("")
    insertion.extend(block_lines)
    if insert_index < len(lines) and lines[insert_index].strip():
        insertion.append("")
    updated = lines[:insert_index] + insertion + lines[insert_index:]
    return "\n".join(updated).rstrip() + "\n"


def _task_blocks_for_phase(plan_block: str, *, phase_id: str) -> str:
    lines = plan_block.splitlines()
    blocks: list[str] = []
    current_phase_id = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("## Phase "):
            current_phase_id = _phase_id_from_heading(line[3:].strip())
            index += 1
            continue
        if TASK_LINE_RE.match(line) and current_phase_id == phase_id:
            start = index
            index += 1
            while index < len(lines):
                if TASK_LINE_RE.match(lines[index]) or lines[index].startswith("## Phase ") or is_task_block_terminator(lines[index]):
                    break
                index += 1
            block_lines = lines[start:index]
            while block_lines and not block_lines[-1].strip():
                block_lines.pop()
            if block_lines:
                blocks.append("\n".join(block_lines))
            continue
        index += 1
    return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")


def _phase_headings(plan_text: str) -> list[tuple[int, str, str]]:
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(plan_text.splitlines()):
        if not line.startswith("## Phase "):
            continue
        phase_title = line[3:].strip()
        headings.append((index, _phase_id_from_heading(phase_title), phase_title))
    return headings


def _phase_bounds(lines: Sequence[str], *, phase_id: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        if not line.startswith("## Phase "):
            continue
        current_phase_id = _phase_id_from_heading(line[3:].strip())
        if start is None:
            if current_phase_id == phase_id:
                start = index
            continue
        return start, index
    if start is None:
        return None
    for index in range(start + 1, len(lines)):
        if FINAL_OBJECTIVE_HEADING_RE.match(lines[index]):
            return start, index
    return start, len(lines)


def _phase_id_from_heading(phase_title: str) -> str:
    match = re.match(r"^Phase\s+([^:]+)", phase_title.strip())
    if match:
        return match.group(1).strip()
    return phase_title.strip()


def _final_objective_heading_index(lines: Sequence[str]) -> int | None:
    for index, line in enumerate(lines):
        if FINAL_OBJECTIVE_HEADING_RE.match(line):
            return index
    return None


def _plan_patch_failure(
    *,
    project: Path,
    workflow_id: str,
    change_request_id: str,
    patch_path: Path,
    started_at: str,
    status: str,
    errors: Sequence[str],
    plan_patch_operation: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "workflow_id": workflow_id,
        "change_request_id": change_request_id,
        "plan_patch_path": _path_for_record(project, patch_path),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "added_tasks": [],
        "modified_tasks": [],
        "added_phase_ids": [],
        "plan_patch_operation": plan_patch_operation,
        "event_id": None,
        "errors": list(errors),
        "warnings": [],
    }


def _mark_plan_tasks_complete(
    plan_text: str,
    tasks: Mapping[str, PlanTask],
    accepted_task_ids: Sequence[str],
) -> tuple[str, list[str]]:
    lines = plan_text.splitlines()
    updated: list[str] = []
    for task_id in accepted_task_ids:
        task = tasks[task_id]
        if task.status == "x":
            continue
        line = lines[task.line_index]
        lines[task.line_index] = line.replace(f"- [{task.status}] {task_id}:", f"- [x] {task_id}:", 1)
        updated.append(task_id)
    trailing_newline = "\n" if plan_text.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline, updated


def _latest_record(
    *,
    project: Path,
    task_id: str,
    run_dir: Path,
    validation_path: Path,
    validation: Mapping[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "latest_run_id": str(validation.get("run_id") or run_dir.name),
        "latest_run_dir": _path_for_record(project, run_dir),
        "validation_path": _path_for_record(project, validation_path),
        "validation_status": str(validation.get("status") or ""),
        "updated_at": updated_at,
        "updated_by": RECONCILER_NAME,
    }


def _latest_path(project: Path, paths: WorkflowPaths, task: PlanTask) -> Path:
    latest = task.latest_path or f"{paths.value('results_dir').rstrip('/')}/{task.task_id}/latest.json"
    path = Path(latest)
    if path.is_absolute():
        return path
    return project / latest


def _record_validation_failure(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    validation: Mapping[str, Any],
    validation_path: Path,
    run_dir: Path,
    primary_task_id: str,
    task: PlanTask,
) -> dict[str, Any]:
    now = utc_timestamp()
    signature = _validation_failure_signature(primary_task_id, validation)
    candidate = {
        "failure_id": _new_failure_id(),
        "task_id": primary_task_id,
        "run_id": str(validation.get("run_id") or run_dir.name),
        "status": "unrecovered",
        "failure_class": "validation_failed",
        "failure_signature": signature,
        "summary": str(validation.get("summary") or "Validation failed."),
        "first_seen_at": now,
        "last_seen_at": now,
        "attempts": 1,
        "recovery_attempts": 0,
        "max_recovery_attempts": task.max_attempts,
        "budget_remaining": True,
        "recoverable": True,
        "run_ids": [str(validation.get("run_id") or run_dir.name)],
        "source_validation_path": _path_for_record(project, validation_path),
    }
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        changed = _upsert_failure(registry, candidate)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed or candidate


def _mark_accepted_validation_failures_recovered(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    accepted_task_ids: Sequence[str],
    run_id: str,
    validation_path: Path,
    recovered_at: str,
) -> list[dict[str, Any]]:
    accepted = {str(task_id) for task_id in accepted_task_ids if str(task_id)}
    if not accepted:
        return []
    recovered: list[dict[str, Any]] = []
    validation_record_path = _path_for_record(project, validation_path)

    def update(registry: dict[str, Any]) -> None:
        failures = registry.setdefault("failures", [])
        if not isinstance(failures, list):
            failures = []
            registry["failures"] = failures
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            if str(failure.get("task_id") or "") not in accepted:
                continue
            if str(failure.get("failure_class") or "") not in ACCEPTED_VALIDATION_RECOVERABLE_FAILURE_CLASSES:
                continue
            if str(failure.get("status") or "unrecovered") in {"recovered", "waived"}:
                continue
            failure["status"] = "recovered"
            failure["recoverable"] = False
            failure["budget_remaining"] = False
            failure["recovered_at"] = recovered_at
            failure["resolved_at"] = recovered_at
            failure["recovered_by"] = RECONCILER_NAME
            failure["recovered_by_run_id"] = run_id
            failure["recovered_by_validation_path"] = validation_record_path
            failure["last_seen_at"] = recovered_at
            failure.pop("active_recovery_run_id", None)
            failure.pop("exhausted_reason", None)
            failure.pop("needs_human_reason", None)
            recovered.append(dict(failure))

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return recovered


def _upsert_failure(registry: dict[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    failures = registry.setdefault("failures", [])
    if not isinstance(failures, list):
        failures = []
        registry["failures"] = failures
    existing = _matching_failure(failures, candidate)
    if existing is None:
        record = dict(candidate)
        _refresh_failure_budget(record)
        failures.append(record)
        return record

    existing["last_seen_at"] = candidate.get("last_seen_at") or utc_timestamp()
    existing["attempts"] = _int_value(existing.get("attempts"), default=0) + 1
    existing["run_id"] = candidate.get("run_id") or existing.get("run_id")
    existing["summary"] = candidate.get("summary") or existing.get("summary")
    existing["source_validation_path"] = candidate.get("source_validation_path") or existing.get("source_validation_path")
    existing["run_ids"] = _append_unique_string(existing.get("run_ids"), candidate.get("run_id"))
    if str(existing.get("status") or "unrecovered") not in TERMINAL_FAILURE_STATUSES:
        existing["status"] = "unrecovered"
    _refresh_failure_budget(existing)
    return existing


def _matching_failure(failures: Sequence[Any], candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("status") or "") in TERMINAL_FAILURE_STATUSES:
            continue
        if (
            str(failure.get("task_id") or "") == str(candidate.get("task_id") or "")
            and str(failure.get("failure_class") or "") == str(candidate.get("failure_class") or "")
            and str(failure.get("failure_signature") or "") == str(candidate.get("failure_signature") or "")
        ):
            return failure
    return None


def _refresh_failure_budget(failure: dict[str, Any]) -> None:
    status = str(failure.get("status") or "unrecovered")
    recovery_attempts = _int_value(failure.get("recovery_attempts"), default=0)
    max_attempts = _int_value(failure.get("max_recovery_attempts"), default=1)
    budget_remaining = status == "unrecovered" and recovery_attempts < max_attempts
    failure["budget_remaining"] = budget_remaining
    failure["recoverable"] = status == "unrecovered" and budget_remaining
    if status == "unrecovered" and not budget_remaining:
        failure["status"] = "exhausted"
        failure["exhausted_reason"] = "max_recovery_attempts_exhausted"


def _record_approval_request(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    validation: Mapping[str, Any],
    validation_path: Path,
    run_dir: Path,
    primary_task_id: str,
) -> dict[str, Any]:
    existing = _matching_approval_request(paths, validation_path)
    if existing is not None:
        return existing
    now = utc_timestamp()
    request = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": _new_approval_id(),
        "created_at": now,
        "workflow_id": workflow_id,
        "task_id": primary_task_id,
        "run_id": str(validation.get("run_id") or run_dir.name),
        "type": "validation_needs_human",
        "message": str(validation.get("summary") or f"Validation for {primary_task_id} requires human input."),
        "scope": f"{primary_task_id} validation only",
        "status": "pending",
        "source_validation_path": _path_for_record(project, validation_path),
        "validation_status": str(validation.get("status") or ""),
    }
    _append_jsonl_locked(paths, paths.runtime_dir / "human_approval_requests.jsonl", request)
    return request


def _matching_approval_request(paths: WorkflowPaths, validation_path: Path) -> dict[str, Any] | None:
    source = _path_for_record(paths.project_root, validation_path)
    for record in _read_jsonl(paths.runtime_dir / "human_approval_requests.jsonl"):
        if str(record.get("source_validation_path") or "") == source and str(record.get("status") or "") == "pending":
            return record
    return None


def _auto_human_summaries_after_reconcile_enabled(workflow_config: Mapping[str, Any]) -> bool:
    for key in ("human_summaries", "summaries"):
        config = workflow_config.get(key)
        if isinstance(config, Mapping):
            for option in ("auto_after_reconcile", "auto_generate_after_reconcile", "generate_after_reconcile"):
                if option in config:
                    return config.get(option) is True
    if "auto_human_summaries_after_reconcile" in workflow_config:
        return workflow_config.get("auto_human_summaries_after_reconcile") is True
    # Human summaries are presentation artifacts.  Keeping them opt-in avoids
    # spending runner quota and traversing result trees on every successful
    # reconciliation while preserving the explicit `summarize` workflow.
    return False


def _request_read_model_rebuild(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    run_id: str,
    reason: str,
    validation_path: Path,
) -> None:
    request_read_model_rebuild(
        paths,
        workflow_id=workflow_id,
        run_id=run_id,
        reason=reason,
        requested_by=RECONCILER_NAME,
        source_path=validation_path,
        extra={"validation_path": _path_for_record(paths.project_root, validation_path)},
    )


def _write_reconciliation_summary(
    *,
    project: Path,
    run_dir: Path,
    result: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> None:
    node_summary_path = run_dir / NODE_SUMMARY_FILENAME
    existing = _read_json_object(node_summary_path, default={})
    if not isinstance(existing, dict):
        existing = {}
    existing["schema_version"] = SCHEMA_VERSION
    existing["workflow_id"] = result.get("workflow_id")
    existing["run_id"] = result.get("run_id")
    existing["task_id"] = result.get("primary_task_id")
    existing["reconciliation"] = {
        "status": result.get("status"),
        "validation_status": result.get("validation_status"),
        "accepted_task_ids": list(result.get("accepted_task_ids") or []),
        "updated_plan_task_ids": list(result.get("updated_plan_task_ids") or []),
        "validation_path": result.get("validation_path"),
        "updated_at": result.get("ended_at"),
    }
    existing["multi_task_absorption"] = dict(validation.get("multi_task_absorption") or {})
    existing["multi_task_absorption"]["reconciled_at"] = result.get("ended_at")
    _atomic_write_json(node_summary_path, existing)


def _update_failure_registry_locked(paths: WorkflowPaths, *, workflow_id: str, update: Any) -> Any:
    owner = f"failure-registry:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "failure_registry_lock", owner, ttl_seconds=30)
    with lock.acquire():
        registry = _read_failure_registry(paths, workflow_id=workflow_id)
        result = update(registry)
        _atomic_write_json(paths.runtime_dir / FAILURE_REGISTRY_FILENAME, registry)
        return result


def _read_failure_registry(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    data = _read_json_object(paths.runtime_dir / FAILURE_REGISTRY_FILENAME, default={})
    failures = data.get("failures") if isinstance(data, Mapping) else []
    if not isinstance(failures, list):
        failures = []
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(data.get("workflow_id") or workflow_id) if isinstance(data, Mapping) else workflow_id,
        "failures": [dict(failure) for failure in failures if isinstance(failure, Mapping)],
    }


def _update_runtime_state(
    paths: WorkflowPaths,
    *,
    status: str,
    update: Mapping[str, Any],
    clear_manual_plan_change: bool = False,
) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_object(state_path, default={})
    if not isinstance(state, dict):
        state = {}
    state["schema_version"] = str(state.get("schema_version") or SCHEMA_VERSION)
    state["workflow_id"] = str(state.get("workflow_id") or update.get("workflow_id") or "")
    state["status"] = status
    state["updated_at"] = utc_timestamp()
    manual_plan_change_present = _manual_plan_change_present(state)
    if status in {"reconciled", "plan_updated"}:
        plan_sha = _sha256_file(paths.plan_file)
        if plan_sha and (clear_manual_plan_change or not manual_plan_change_present):
            state["active_plan_sha256"] = plan_sha
        if clear_manual_plan_change:
            state.pop("manual_plan_change", None)
            state["configuration_problems"] = [
                dict(problem)
                for problem in state.get("configuration_problems", [])
                if isinstance(problem, Mapping) and str(problem.get("code") or "") != "manual_plan_change_detected"
            ]
    reconciler = state.get("reconciler")
    if not isinstance(reconciler, dict):
        reconciler = {}
    reconciler.update(dict(update))
    if status in {"reconciled", "plan_updated"} and state.get("active_plan_sha256"):
        reconciler["active_plan_sha256"] = state["active_plan_sha256"]
    reconciler["heartbeat_at"] = utc_timestamp()
    state["reconciler"] = reconciler
    _atomic_write_json(state_path, state)


def _manual_plan_change_present(state: Mapping[str, Any]) -> bool:
    if isinstance(state.get("manual_plan_change"), Mapping):
        return True
    problems = state.get("configuration_problems")
    if not isinstance(problems, Sequence) or isinstance(problems, (str, bytes)):
        return False
    return any(
        isinstance(problem, Mapping) and str(problem.get("code") or "") == "manual_plan_change_detected"
        for problem in problems
    )


def _task_approval_granted(paths: WorkflowPaths, task_id: str) -> bool:
    for response in _read_jsonl(paths.runtime_dir / "human_approval_responses.jsonl"):
        decision = str(response.get("decision") or response.get("status") or "").lower()
        if decision != "approved":
            continue
        if str(response.get("task_id") or "") == task_id:
            return True
        scope = str(response.get("scope") or "")
        if task_id in {part.strip(" ,.;:") for part in scope.split()}:
            return True
    return False


def _read_validation(path: Path) -> tuple[Mapping[str, Any], str | None]:
    if path.name != VALIDATION_FILENAME:
        return {}, f"validation path must point to {VALIDATION_FILENAME}: {path}"
    data = _read_json_object(path, default=None)
    if not isinstance(data, Mapping):
        return {}, f"{path}: missing, malformed, or non-object validation JSON"
    if data.get("schema_version") != SCHEMA_VERSION:
        return {}, f"{path}: schema_version must be {SCHEMA_VERSION}"
    if str(data.get("validator") or "") == RECONCILER_NAME:
        return {}, f"{path}: reconciler-owned records are not authoritative validation"
    return data, None


def _resolve_validation_path(project: Path, *, run_dir: Path | str | None, validation_path: Path | str | None) -> Path:
    if validation_path is not None:
        path = Path(validation_path).expanduser()
        if not path.is_absolute():
            path = project / path
        return path.resolve()
    if run_dir is None:
        return (project / VALIDATION_FILENAME).resolve()
    resolved_run_dir = Path(run_dir).expanduser()
    if not resolved_run_dir.is_absolute():
        resolved_run_dir = project / resolved_run_dir
    return (resolved_run_dir / VALIDATION_FILENAME).resolve()


def _resolve_run_dir(project: Path, *, run_dir: Path | str | None, validation_path: Path) -> Path:
    if run_dir is not None:
        path = Path(run_dir).expanduser()
        if not path.is_absolute():
            path = project / path
        return path.resolve()
    return validation_path.parent.resolve()


def _task_fields(block_lines: Sequence[str]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    current_field: str | None = None
    for line in block_lines[1:]:
        match = FIELD_LINE_RE.match(line)
        if match:
            field = match.group("field").strip().lower().replace("-", "_").replace(" ", "_")
            value = match.group("value").strip()
            fields.setdefault(field, []).append(value)
            current_field = field
            continue
        if current_field and line.startswith("    "):
            fields[current_field][-1] = f"{fields[current_field][-1]} {line.strip()}".strip()
    return {field: tuple(values) for field, values in fields.items()}


def _append_jsonl_locked(paths: WorkflowPaths, path: Path, record: Mapping[str, Any]) -> None:
    owner = f"jsonl-append:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "event_append_lock", owner, ttl_seconds=30)
    with lock.acquire():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, Mapping):
            records.append(dict(data))
    return records


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _path_for_record(project: Path | None, path: Path) -> str:
    if project is not None:
        try:
            return path.resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _append_unique_string(existing: Any, value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes)):
        values.extend(str(item) for item in existing if item is not None and str(item))
    if value is not None and str(value):
        values.append(str(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _validation_failure_signature(task_id: str, validation: Mapping[str, Any]) -> str:
    explicit = validation.get("failure_signature") or validation.get("signature")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    material = {
        "task_id": task_id,
        "status": validation.get("status"),
        "summary": validation.get("summary") or validation.get("message") or validation.get("reason"),
        "failures": validation.get("failures") or [],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "validation_failed:" + sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_failure_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"fail_{stamp}_{uuid.uuid4().hex[:8]}"


def _new_approval_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"approval_{stamp}_{uuid.uuid4().hex[:8]}"


def _load_failure(*, project: Path, started_at: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "failed",
        "project_root": project.as_posix(),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "message": message,
        "accepted_task_ids": [],
        "warnings": [],
        "errors": [message],
    }


def _no_mutation_result(
    *,
    project: Path,
    workflow_id: str,
    started_at: str,
    validation_path: Path,
    run_dir: Path,
    status: str,
    message: str,
    primary_task_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "workflow_id": workflow_id,
        "primary_task_id": primary_task_id,
        "run_id": run_dir.name,
        "validation_path": _path_for_record(project, validation_path),
        "run_dir": _path_for_record(project, run_dir),
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "accepted_task_ids": [],
        "warnings": [],
        "errors": [message],
        "would_mutate": False,
    }
    if isinstance(extra, Mapping):
        result.update(dict(extra))
    return result
