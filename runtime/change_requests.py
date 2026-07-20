from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.adapters.base import ADAPTER_INPUT_FILENAME, AdapterContractError, AdapterInput, utc_timestamp
from runtime.adapters.registry import AdapterLookupError, get_adapter
from runtime.agent_runners import AgentRunnerConfigError, load_agent_runners
from runtime.approval import (
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
    approval_record_status,
    default_expires_at,
    load_approval_policy,
    new_approval_id,
)
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config, path_lines
from runtime.prompt_context import file_reference, prompt_reference_index
from runtime.reconciliation import (
    PLAN_PATCH_APPEND_BEGIN,
    PLAN_PATCH_APPEND_END,
    PLAN_PATCH_OPERATION_APPEND,
    PLAN_PATCH_OPERATION_REPLACE_TASKS,
    PLAN_PATCH_REPLACE_BEGIN,
    PLAN_PATCH_REPLACE_END,
    apply_approved_plan_patch,
    parse_plan_tasks,
)
from runtime.scheduler import AtomicOwnerLock, SCHEMA_VERSION, append_event
from runtime.version_control import create_git_checkpoint


CHANGE_REQUESTS_FILENAME = "change_requests.jsonl"
CHANGE_REQUEST_RESPONSES_FILENAME = "change_request_responses.jsonl"
PLAN_PATCH_FILENAME = "PLAN_PATCH.md"
CHANGE_REQUEST_CONTEXT_MANIFEST_FILENAME = "change_request_context_manifest.json"
CHANGE_REQUEST_WORKFLOW_PATHS_FILENAME = "workflow_paths.txt"
CHANGE_REQUEST_PLANNER_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "templates" / "change_request_planner_prompt.template.md"
)
ALLOWED_CHANGE_REQUEST_STATUSES = frozenset(
    {
        "pending_review",
        "planner_reviewing",
        "needs_user_approval",
        "approved",
        "rejected",
        "applied",
        "superseded",
        "failed",
    }
)


def submit_change_request(
    project_root: Path | str,
    user_request: str,
    *,
    source: str = "cli",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    created_at = utc_timestamp()
    text = str(user_request or "").strip()
    if not text:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="invalid_request",
            message="Change request text is required.",
        )
    request = {
        "schema_version": SCHEMA_VERSION,
        "change_request_id": new_change_request_id(),
        "created_at": created_at,
        "source": source or "cli",
        "workflow_id": workflow_id,
        "user_request": text,
        "status": "pending_review",
        "impact": {
            "scope_change": True,
            "requires_new_tasks": True,
            "requires_approval": True,
            "analysis_required": True,
        },
        "planner_response": None,
        "approval_request_id": None,
        "applied_plan_update_event_id": None,
    }
    if metadata:
        request["metadata"] = dict(metadata)
    _append_jsonl_locked(paths, paths.requests_dir / CHANGE_REQUESTS_FILENAME, request)
    event = append_event(
        paths,
        workflow_id=workflow_id,
        event_type="change_request_submitted",
        data={
            "request_id": request["change_request_id"],
            "change_request_id": request["change_request_id"],
            "source": request["source"],
            "message": "Change request submitted for planner review.",
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "pending_review",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "change_request": request,
        "change_request_id": request["change_request_id"],
        "event_id": event.get("event_id"),
        "files_written": [_path_for_record(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME)],
        "errors": [],
        "warnings": [],
    }


def review_change_request(
    project_root: Path | str,
    change_request_id: str | None = None,
    *,
    source: str = "cli",
    audit: bool | None = None,
    runner_id: str | None = None,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    request = _select_request_for_review(paths, change_request_id)
    if request is None:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="not_found",
            message="No pending change request was found for review.",
        )

    current = change_request_status_record(paths, request)
    if current["status"] not in {"pending_review", "planner_reviewing"}:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="not_reviewable",
            message=f"Change request {request['change_request_id']} is {current['status']}, not pending review.",
        )

    change_request_id = str(request["change_request_id"])
    try:
        runner = _change_request_planner_runner(project, runner_id=runner_id)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError) as error:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="waiting_config",
            message=f"Change request planner runner configuration is not usable: {error}",
            extra={"runner_id": runner_id or "change_request_planner"},
        )

    run_id = new_change_request_run_id()
    role_output_dir = paths.requests_dir / "change_runs" / run_id
    role_output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = role_output_dir / "change_request_planner_prompt.md"
    response_path = role_output_dir / "change_request_response.json"
    patch_path = role_output_dir / PLAN_PATCH_FILENAME
    audit_report_path = role_output_dir / "plan_patch_audit.json"
    node_summary_path = role_output_dir / "node_summary.json"

    plan_text = _safe_read(paths.plan_file)
    existing_tasks = parse_plan_tasks(plan_text)
    added_task_id = _next_change_task_id(existing_tasks)
    added_title = _change_task_title(str(request.get("user_request") or "Requested plan change"))
    policy = load_approval_policy(paths)
    approval_required = policy.get("enabled") is True
    fallback_patch_text = build_append_task_plan_patch(
        paths,
        change_request_id=change_request_id,
        task_id=added_task_id,
        task_title=added_title,
        user_request=str(request.get("user_request") or ""),
        approval_required=approval_required,
    )
    prompt_text = build_change_request_planner_prompt(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        run_id=run_id,
        request=request,
        role_output_dir=role_output_dir,
        patch_path=patch_path,
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")

    warnings: list[str] = []
    agent_run = _run_change_request_planner_agent(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        run_id=run_id,
        runner=runner,
        request=request,
        prompt_path=prompt_path,
        prompt_text=prompt_text,
        role_output_dir=role_output_dir,
        patch_path=patch_path,
        response_path=response_path,
    )
    planner_source = "change_request_planner_agent"
    if _has_text(patch_path):
        patch_text = _safe_read(patch_path)
    else:
        planner_source = (
            "deterministic_fallback_after_agent_failure" if agent_run.get("ran") else "deterministic_fallback"
        )
        patch_path.write_text(fallback_patch_text, encoding="utf-8")
        patch_text = fallback_patch_text
        warnings.append("Change request planner agent did not produce PLAN_PATCH.md; deterministic fallback patch was used.")
    agent_response = _read_json_object(response_path, default={}) if response_path.exists() else {}
    if not isinstance(agent_response, Mapping):
        agent_response = {}
    patch_task_ids = _patch_task_ids(patch_text)
    if patch_task_ids:
        added_tasks = [task_id for task_id in patch_task_ids if task_id not in existing_tasks]
        modified_tasks = [task_id for task_id in patch_task_ids if task_id in existing_tasks]
    else:
        added_tasks = [added_task_id]
        modified_tasks = []
    patch_type = _plan_patch_type(
        patch_text,
        added_tasks=added_tasks,
        modified_tasks=modified_tasks,
        declared_type=str(_mapping(agent_response.get("plan_patch")).get("type") or ""),
    )
    plan_patch_sha256 = _sha256_file(patch_path)

    auditor_required = bool(audit) if audit is not None else bool(_planning_config(paths).get("auditor_required", False))
    audit_report: dict[str, Any] | None = None
    if auditor_required:
        audit_report = _write_plan_patch_audit(
            audit_report_path,
            project=project,
            workflow_id=workflow_id,
            run_id=run_id,
            change_request_id=change_request_id,
            patch_path=patch_path,
            added_tasks=added_tasks,
            modified_tasks=modified_tasks,
            patch_type=patch_type,
            plan_patch_sha256=plan_patch_sha256,
        )
    audit_errors = [str(item) for item in (audit_report.get("blocking_findings") or [])] if audit_report else []
    if not plan_patch_sha256:
        audit_errors.append("PLAN_PATCH.md could not be content-bound for review.")
    audit_failed = bool(audit_errors) or (audit_report is not None and audit_report.get("passed") is not True)
    approval_request: dict[str, Any] | None = None
    if not audit_failed and approval_required and policy.get("enabled") is True:
        approval_request = _record_change_request_approval(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            request=request,
            patch_path=patch_path,
            plan_patch_sha256=plan_patch_sha256,
        )
        append_event(
            paths,
            workflow_id=workflow_id,
            event_type="approval_requested",
            data={
                "request_id": change_request_id,
                "change_request_id": change_request_id,
                "approval_id": approval_request.get("approval_id"),
                "type": "change_request_plan_patch",
                "plan_patch_sha256": plan_patch_sha256,
                "message": "Approval requested for audited change request plan patch.",
            },
            run_id=run_id,
        )
    change_request_status = "failed" if audit_failed else "needs_user_approval" if approval_required else "approved"

    response = {
        "schema_version": SCHEMA_VERSION,
        "change_request_id": change_request_id,
        "response_id": new_change_request_response_id(),
        "run_id": run_id,
        "role": "change_request_planner",
        "runner_id": runner.runner_id,
        "adapter": runner.adapter,
        "created_at": utc_timestamp(),
        "source": source or "cli",
        "status": str(agent_response.get("status") or "proposal_created"),
        "change_request_status": change_request_status,
        "planner_source": planner_source,
        "agent_run": agent_run,
        "impact": {
            "scope_change": bool(_mapping(agent_response.get("impact")).get("scope_change", True)),
            "requires_approval": approval_required,
            "adds_tasks": added_tasks,
            "modifies_tasks": modified_tasks,
            "supersedes_tasks": _text_list(_mapping(agent_response.get("impact")).get("supersedes_tasks")),
        },
        "plan_patch": {
            "type": patch_type,
            "patch_file": _path_for_record(project, patch_path),
            "sha256": plan_patch_sha256,
        },
        "auditor_required": auditor_required,
        "audit_passed": False if audit_failed else audit_report.get("passed") if audit_report is not None else None,
        "audit_report": _path_for_record(project, audit_report_path) if audit_report is not None else None,
        "approval_request_id": approval_request.get("approval_id") if approval_request is not None else None,
        "approval_policy": policy,
        "role_output_dir": _path_for_record(project, role_output_dir),
        "prompt_path": _path_for_record(project, prompt_path),
        "planner_must_not_apply_plan_directly": True,
        "can_continue_before_resolution": bool(agent_response.get("can_continue_before_resolution", not approval_required)),
    }
    if agent_response:
        response["agent_response"] = _json_safe(agent_response)
    response_path.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _append_jsonl_locked(paths, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME, response)

    node_summary = {
        "schema_version": SCHEMA_VERSION,
        "node_type": "change_request_planner",
        "role": "change_request_planner",
        "status": response["change_request_status"],
        "workflow_id": workflow_id,
        "run_id": run_id,
        "runner_id": runner.runner_id,
        "adapter": runner.adapter,
        "change_request_id": change_request_id,
        "message": f"Change request {change_request_id} reviewed; PLAN_PATCH.md proposed.",
        "role_output_dir": _path_for_record(project, role_output_dir),
        "prompt_path": _path_for_record(project, prompt_path),
        "plan_patch_path": _path_for_record(project, patch_path),
        "response_path": _path_for_record(project, response_path),
        "approval_id": response["approval_request_id"],
        "updated_at": response["created_at"],
    }
    node_summary_path.write_text(json.dumps(node_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    event = append_event(
        paths,
        workflow_id=workflow_id,
        event_type="change_request_reviewed",
        data={
            "request_id": change_request_id,
            "change_request_id": change_request_id,
            "role": "change_request_planner",
            "runner_id": runner.runner_id,
            "response_id": response["response_id"],
            "status": response["change_request_status"],
            "approval_id": response["approval_request_id"],
            "plan_patch_file": response["plan_patch"]["patch_file"],
            "adds_tasks": added_tasks,
            "modifies_tasks": modified_tasks,
            "planner_source": planner_source,
        },
        run_id=run_id,
    )

    if audit_failed:
        _update_runtime_state(
            paths,
            workflow_id=workflow_id,
            status="waiting_config",
            change_request_update={
                "last_action": "review_change_request",
                "last_change_request_id": change_request_id,
                "last_response_id": response["response_id"],
                "plan_patch_audit_failed": True,
                "plan_patch_audit_errors": audit_errors,
            },
        )
    elif approval_required:
        _update_runtime_state(
            paths,
            workflow_id=workflow_id,
            status="waiting_approval",
            change_request_update={
                "last_action": "review_change_request",
                "last_change_request_id": change_request_id,
                "last_response_id": response["response_id"],
                "last_approval_id": response["approval_request_id"],
            },
        )
    else:
        _update_runtime_state(
            paths,
            workflow_id=workflow_id,
            status="change_request_reviewed",
            change_request_update={
                "last_action": "review_change_request",
                "last_change_request_id": change_request_id,
                "last_response_id": response["response_id"],
            },
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not audit_failed,
        "status": response["change_request_status"],
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "role": "change_request_planner",
        "runner_id": runner.runner_id,
        "adapter": runner.adapter,
        "role_output_dir": _path_for_record(project, role_output_dir),
        "change_request": request,
        "response": response,
        "approval_request": approval_request,
        "audit_report": audit_report,
        "event_id": event.get("event_id"),
        "files_written": [
            _path_for_record(project, prompt_path),
            _path_for_record(project, patch_path),
            _path_for_record(project, response_path),
            _path_for_record(project, node_summary_path),
            _path_for_record(project, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME),
        ]
        + ([_path_for_record(project, audit_report_path)] if audit_report is not None else [])
        + ([_path_for_record(project, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME)] if approval_request is not None else []),
        "errors": audit_errors if audit_failed else [],
        "warnings": warnings,
    }


def apply_change_request(
    project_root: Path | str,
    change_request_id: str,
    *,
    source: str = "cli",
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    request = _request_by_id(_read_jsonl(paths.requests_dir / CHANGE_REQUESTS_FILENAME), change_request_id)
    if request is None:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="not_found",
            message=f"Change request {change_request_id!r} was not found.",
        )
    status_record = change_request_status_record(paths, request)
    if status_record["status"] == "applied":
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="already_applied",
            message=f"Change request {change_request_id} has already been applied.",
            extra={"change_request_status": status_record},
        )
    response = _latest_plan_patch_response(paths, change_request_id)
    if response is None:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="missing_plan_patch",
            message=f"Change request {change_request_id} does not have a planner response with a plan patch.",
            extra={"change_request_status": status_record},
        )

    response_status = str(response.get("change_request_status") or "").strip()
    if response_status not in {"approved", "needs_user_approval"}:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="audit_failed" if response_status == "failed" else "change_request_not_approved",
            message=(
                "Change request PLAN_PATCH.md failed its required audit and cannot be applied."
                if response_status == "failed"
                else f"Change request planner response is not applicable: {response_status or 'unknown'}."
            ),
            extra={"response": response, "change_request_status": status_record},
        )

    patch_ref = _mapping(response.get("plan_patch")).get("patch_file")
    patch_path = _resolve_project_path(project, patch_ref)
    expected_plan_patch_sha256 = str(_mapping(response.get("plan_patch")).get("sha256") or "").strip()
    if not expected_plan_patch_sha256:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="unbound_plan_patch",
            message="Change request PLAN_PATCH.md is not content-bound; review the change request again before applying it.",
            extra={"response": response, "change_request_status": status_record},
        )
    actual_plan_patch_sha256 = _sha256_file(patch_path)
    if actual_plan_patch_sha256 != expected_plan_patch_sha256:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="plan_patch_content_changed",
            message=(
                "Change request PLAN_PATCH.md changed after review; "
                f"expected {expected_plan_patch_sha256}, observed {actual_plan_patch_sha256 or 'unavailable'}."
            ),
            extra={"response": response, "change_request_status": status_record},
        )

    if response.get("auditor_required") is True:
        audit_ref = response.get("audit_report")
        audit_report: Mapping[str, Any] = {}
        if isinstance(audit_ref, str) and audit_ref.strip():
            audit_path = _resolve_project_path(project, audit_ref)
            loaded_audit_report = _read_json_object(audit_path, default={})
            if isinstance(loaded_audit_report, Mapping):
                audit_report = loaded_audit_report
        audit_patch_sha256 = str(audit_report.get("plan_patch_sha256") or "") if isinstance(audit_report, Mapping) else ""
        if (
            not isinstance(audit_report, Mapping)
            or audit_report.get("passed") is not True
            or audit_patch_sha256 != expected_plan_patch_sha256
        ):
            return _failure(
                project=project,
                workflow_id=workflow_id,
                status="audit_failed",
                message="Change request audit is missing, failed, or does not match the reviewed PLAN_PATCH.md content.",
                extra={"response": response, "audit_report": audit_report, "change_request_status": status_record},
            )

    approval_check = _approval_check(paths, response)
    if not approval_check["approved"]:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="approval_required",
            message=str(approval_check["message"]),
            extra={"approval": approval_check, "change_request_status": status_record},
        )

    before_checkpoint = create_git_checkpoint(
        project,
        reason="before_change_request_apply",
        run_id=str(response.get("run_id") or change_request_id),
    )
    if before_checkpoint.get("ok") is not True:
        _mark_waiting_config(
            paths,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            reason="before_change_request_apply checkpoint failed.",
            checkpoint=before_checkpoint,
        )
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="checkpoint_failed",
            message="Before-change-request checkpoint failed; PLAN.md was not modified.",
            extra={"before_checkpoint": before_checkpoint},
        )

    patch_contract = _plan_patch_apply_contract(response)
    detected_plan_patch_operation = _change_request_plan_patch_operation(
        plan_text=_safe_read(paths.plan_file),
        patch_text=_safe_read(patch_path),
        declared_type=str(_mapping(response.get("plan_patch")).get("type") or ""),
    )
    if detected_plan_patch_operation is None:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            status="invalid_plan_patch",
            message="Change request PLAN_PATCH.md mixes added and modified tasks or declares an unsupported operation.",
            extra={"change_request_status": status_record},
        )
    declared_plan_patch_operation = str(patch_contract["operation"] or PLAN_PATCH_OPERATION_APPEND)
    plan_patch_operation = (
        declared_plan_patch_operation
        if declared_plan_patch_operation != PLAN_PATCH_OPERATION_APPEND
        else detected_plan_patch_operation
    )
    apply_result = apply_approved_plan_patch(
        project,
        change_request_id=change_request_id,
        plan_patch_path=patch_path,
        response_id=str(response.get("response_id") or ""),
        approval_request_id=str(response.get("approval_request_id") or ""),
        before_checkpoint_id=_checkpoint_id(before_checkpoint),
        plan_patch_operation=plan_patch_operation,
        target_phase_id=patch_contract["target_phase_id"],
        supersede_task_ids=patch_contract["supersede_task_ids"],
        supersede_reason=f"Superseded by approved change request {change_request_id}.",
        supersede_authorization=f"change_request:{change_request_id}",
        expected_plan_patch_sha256=expected_plan_patch_sha256,
    )
    if apply_result.get("ok") is not True:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": apply_result.get("status") or "failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "change_request": request,
            "response": response,
            "before_checkpoint": before_checkpoint,
            "apply_result": apply_result,
            "errors": list(apply_result.get("errors") or ["Plan patch application failed."]),
            "warnings": list(apply_result.get("warnings") or []),
        }

    after_checkpoint = create_git_checkpoint(
        project,
        reason="after_change_request_apply",
        run_id=str(response.get("run_id") or change_request_id),
    )
    if after_checkpoint.get("ok") is not True:
        _mark_waiting_config(
            paths,
            workflow_id=workflow_id,
            change_request_id=change_request_id,
            reason="after_change_request_apply checkpoint failed after PLAN.md mutation.",
            checkpoint=after_checkpoint,
        )
        status = "checkpoint_failed"
        ok = False
        errors = ["After-change-request checkpoint failed after PLAN.md was modified."]
    else:
        status = "applied"
        ok = True
        errors = []

    apply_response = {
        "schema_version": SCHEMA_VERSION,
        "change_request_id": change_request_id,
        "response_id": new_change_request_response_id(prefix="crr_apply"),
        "created_at": utc_timestamp(),
        "source": source or "cli",
        "status": status,
        "change_request_status": "applied" if ok else "failed",
        "applied_plan_update_event_id": apply_result.get("event_id"),
        "plan_patch": response.get("plan_patch"),
        "approval_request_id": response.get("approval_request_id"),
        "before_checkpoint_id": _checkpoint_id(before_checkpoint),
        "after_checkpoint_id": _checkpoint_id(after_checkpoint),
        "added_tasks": list(apply_result.get("added_tasks") or []),
        "modified_tasks": list(apply_result.get("modified_tasks") or []),
        "superseded_tasks": list(apply_result.get("superseded_tasks") or []),
        "already_completed_superseded_tasks": list(apply_result.get("already_completed_superseded_tasks") or []),
        "recovered_superseded_failure_ids": list(apply_result.get("recovered_superseded_failure_ids") or []),
        "plan_patch_operation": apply_result.get("plan_patch_operation"),
        "target_phase_id": apply_result.get("target_phase_id"),
    }
    _append_jsonl_locked(paths, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME, apply_response)
    append_event(
        paths,
        workflow_id=workflow_id,
        event_type="change_request_applied" if ok else "change_request_apply_checkpoint_failed",
        data={
            "request_id": change_request_id,
            "change_request_id": change_request_id,
            "response_id": apply_response["response_id"],
            "status": apply_response["change_request_status"],
            "plan_patch_file": _path_for_record(project, patch_path),
            "applied_plan_update_event_id": apply_result.get("event_id"),
            "before_checkpoint_id": apply_response["before_checkpoint_id"],
            "after_checkpoint_id": apply_response["after_checkpoint_id"],
            "modified_tasks": apply_response["modified_tasks"],
            "superseded_tasks": apply_response["superseded_tasks"],
            "recovered_superseded_failure_ids": apply_response["recovered_superseded_failure_ids"],
        },
        run_id=str(response.get("run_id") or change_request_id),
    )
    _update_runtime_state(
        paths,
        workflow_id=workflow_id,
        status="plan_updated" if ok else "waiting_config",
        change_request_update={
            "last_action": "apply_change_request",
            "last_change_request_id": change_request_id,
            "last_response_id": apply_response["response_id"],
            "last_plan_update_event_id": apply_result.get("event_id"),
            "before_checkpoint_id": apply_response["before_checkpoint_id"],
            "after_checkpoint_id": apply_response["after_checkpoint_id"],
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "change_request": request,
        "planner_response": response,
        "apply_response": apply_response,
        "before_checkpoint": before_checkpoint,
        "after_checkpoint": after_checkpoint,
        "apply_result": apply_result,
        "files_written": [
            paths.value("plan_file"),
            _path_for_record(project, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME),
            _path_for_record(project, paths.runtime_dir / "events" / "events_000001.jsonl"),
            _path_for_record(project, paths.runtime_dir / "read_model_rebuild_request.json"),
            _path_for_record(project, paths.runtime_dir / "git_checkpoints.jsonl"),
        ],
        "errors": errors,
        "warnings": list(apply_result.get("warnings") or []),
    }


def load_change_request_status(
    project_root: Path | str,
    *,
    change_request_id: str | None = None,
    include_all: bool = False,
) -> dict[str, Any]:
    project, paths, workflow_id = _load_project_paths(project_root)
    requests = _read_jsonl(paths.requests_dir / CHANGE_REQUESTS_FILENAME)
    records = [change_request_status_record(paths, request) for request in requests]
    if change_request_id:
        records = [record for record in records if str(record.get("change_request_id") or "") == change_request_id]
    if not include_all:
        records = [record for record in records if str(record.get("status") or "") not in {"applied", "rejected", "superseded"}]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "ok",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "requests_path": _path_for_record(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME),
        "responses_path": _path_for_record(project, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME),
        "pending_count": sum(1 for record in records if record.get("status") in {"pending_review", "needs_user_approval", "approved"}),
        "change_requests": records,
        "errors": [],
        "warnings": [],
    }


def change_request_status_record(paths: WorkflowPaths, request: Mapping[str, Any]) -> dict[str, Any]:
    change_request_id = str(request.get("change_request_id") or "")
    responses = [
        response
        for response in _read_jsonl(paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME)
        if str(response.get("change_request_id") or "") == change_request_id
    ]
    latest = _latest_response(responses)
    record = dict(request)
    record["change_request_id"] = change_request_id
    record["responses"] = responses
    if latest is not None:
        record["latest_response"] = latest
    status = str(request.get("status") or "pending_review")
    if latest is not None:
        latest_status = str(latest.get("change_request_status") or latest.get("status") or "")
        if latest_status == "applied":
            status = "applied"
        elif latest_status == "failed":
            status = "failed"
        elif latest.get("plan_patch"):
            approval = _approval_check(paths, latest)
            if approval.get("required"):
                if approval.get("approved"):
                    status = "approved"
                else:
                    approval_status = str(_mapping(approval.get("approval")).get("status") or "")
                    status = "superseded" if approval_status == "superseded" else "rejected" if approval_status in {"rejected", "expired"} else "needs_user_approval"
            else:
                status = "approved"
        elif latest_status in ALLOWED_CHANGE_REQUEST_STATUSES:
            status = latest_status
    record["status"] = status if status in ALLOWED_CHANGE_REQUEST_STATUSES else "failed"
    return record


def build_append_task_plan_patch(
    paths: WorkflowPaths,
    *,
    change_request_id: str,
    task_id: str,
    task_title: str,
    user_request: str,
    approval_required: bool,
) -> str:
    approval = "required: change_request_scope" if approval_required else "not_required"
    safe_request = " ".join(str(user_request or "").split())
    return f"""# LoopPlane PLAN_PATCH

- schema_version: {SCHEMA_VERSION}
- change_request_id: {change_request_id}
- type: append_tasks
- target_phase: Phase Change Requests

This patch was proposed by the change request planner. It must be applied only
by reconciler-controlled code after required approval is present.

LOOPPLANE_PLAN_APPEND_BEGIN
## Phase Change Requests: Approved Change Requests

- [ ] {task_id}: {task_title}
  - acceptance: The approved change request {change_request_id} is satisfied: {safe_request}
  - evidence: {paths.value("results_dir").rstrip("/")}/{task_id}/
  - latest: {paths.value("results_dir").rstrip("/")}/{task_id}/latest.json
  - depends_on: []
  - risk: medium
  - validation: Focused validation demonstrates the requested plan change has been implemented.
  - max_attempts: 3
  - approval: {approval}
  - deliverables: Implementation and evidence for change request {change_request_id}
LOOPPLANE_PLAN_APPEND_END
"""


def build_change_request_planner_prompt(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    run_id: str,
    request: Mapping[str, Any],
    role_output_dir: Path,
    patch_path: Path,
) -> str:
    template = _safe_read(CHANGE_REQUEST_PLANNER_TEMPLATE_PATH)
    context_manifest = _write_change_request_context_manifest(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        run_id=run_id,
        request=request,
        role_output_dir=role_output_dir,
    )
    variables = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "role_output_dir": _path_for_record(project, role_output_dir),
        "change_request_id": str(request.get("change_request_id") or ""),
        "change_request": str(request.get("user_request") or ""),
        "brief_file": paths.value("brief_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "plan_file": paths.value("plan_file"),
        "read_models_dir": paths.value("read_models_dir"),
        "results_dir": paths.value("results_dir"),
        "requests_dir": paths.value("requests_dir"),
        "plan_patch_path": _path_for_record(project, patch_path),
        "context_manifest_path": _path_for_record(project, role_output_dir / CHANGE_REQUEST_CONTEXT_MANIFEST_FILENAME),
        "context_references_json": json.dumps(prompt_reference_index(context_manifest["references"]), indent=2, sort_keys=True),
    }
    return _render_template(template, variables)


def _write_change_request_context_manifest(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    run_id: str,
    request: Mapping[str, Any],
    role_output_dir: Path,
) -> dict[str, Any]:
    role_output_dir.mkdir(parents=True, exist_ok=True)
    workflow_paths_path = role_output_dir / CHANGE_REQUEST_WORKFLOW_PATHS_FILENAME
    workflow_paths_path.write_text(path_lines(paths), encoding="utf-8")
    references: dict[str, Any] = {
        "project_brief": file_reference(project, paths.brief_file, label="Project brief"),
        "shared_context": file_reference(project, paths.shared_context_file, label="Shared workflow context"),
        "active_plan": file_reference(project, paths.plan_file, label="Active plan"),
        "workflow_paths": file_reference(project, workflow_paths_path, label="Configured workflow paths"),
        "change_requests": file_reference(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME, label="Change request ledger"),
        "prior_change_request_responses": file_reference(project, paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME, label="Prior change request responses"),
        "read_models_dir": {"label": "Read models directory", "path": paths.value("read_models_dir"), "exists": paths.read_models_dir.exists()},
        "results_dir": {"label": "Task results directory", "path": paths.value("results_dir"), "exists": paths.results_dir.exists()},
    }
    manifest = {
        "schema_version": "1.0",
        "generated_at": utc_timestamp(),
        "workflow_id": workflow_id,
        "run_id": run_id,
        "change_request_id": str(request.get("change_request_id") or ""),
        "source_authority": "prompt_context_manifest_not_source_of_truth",
        "instructions": [
            "Use the referenced files as change-request context instead of relying on prompt-inlined copies.",
            "The change request text in the prompt identifies the requested scope change; PLAN.md remains authoritative.",
        ],
        "references": references,
    }
    _atomic_write_json(role_output_dir / CHANGE_REQUEST_CONTEXT_MANIFEST_FILENAME, manifest)
    return manifest


def _run_change_request_planner_agent(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    run_id: str,
    runner: Any,
    request: Mapping[str, Any],
    prompt_path: Path,
    prompt_text: str,
    role_output_dir: Path,
    patch_path: Path,
    response_path: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "runner_id": runner.runner_id,
        "adapter": runner.adapter,
        "role": "change_request_planner",
        "ran": False,
        "ok": False,
        "prompt_path": _path_for_record(project, prompt_path),
        "role_output_dir": _path_for_record(project, role_output_dir),
        "plan_patch_path": _path_for_record(project, patch_path),
        "response_path": _path_for_record(project, response_path),
    }
    try:
        adapter = get_adapter(runner.adapter)
    except AdapterLookupError as error:
        result["error"] = str(error)
        return result

    env = {
        "LOOPPLANE_WORKFLOW_ID": workflow_id,
        "LOOPPLANE_RUN_ID": run_id,
        "LOOPPLANE_PROJECT_ROOT": project.as_posix(),
        "LOOPPLANE_PROJECT_ROOT_REL": ".",
        "LOOPPLANE_BRIEF_FILE": paths.brief_file.as_posix(),
        "LOOPPLANE_BRIEF_FILE_REL": _path_for_record(project, paths.brief_file),
        "LOOPPLANE_SHARED_CONTEXT_FILE": paths.shared_context_file.as_posix(),
        "LOOPPLANE_SHARED_CONTEXT_FILE_REL": _path_for_record(project, paths.shared_context_file),
        "LOOPPLANE_PLAN_FILE": paths.plan_file.as_posix(),
        "LOOPPLANE_PLAN_FILE_REL": _path_for_record(project, paths.plan_file),
        "LOOPPLANE_READ_MODELS_DIR": paths.read_models_dir.as_posix(),
        "LOOPPLANE_READ_MODELS_DIR_REL": _path_for_record(project, paths.read_models_dir),
        "LOOPPLANE_RESULTS_DIR": paths.results_dir.as_posix(),
        "LOOPPLANE_RESULTS_DIR_REL": _path_for_record(project, paths.results_dir),
        "LOOPPLANE_REQUESTS_DIR": paths.requests_dir.as_posix(),
        "LOOPPLANE_REQUESTS_DIR_REL": _path_for_record(project, paths.requests_dir),
        "LOOPPLANE_CHANGE_REQUEST_ID": str(request.get("change_request_id") or ""),
        "LOOPPLANE_CHANGE_REQUEST_RESPONSE_PATH": response_path.as_posix(),
        "LOOPPLANE_CHANGE_REQUEST_RESPONSE_PATH_REL": _path_for_record(project, response_path),
        "LOOPPLANE_PLAN_PATCH_PATH": patch_path.as_posix(),
        "LOOPPLANE_PLAN_PATCH_PATH_REL": _path_for_record(project, patch_path),
        "LOOPPLANE_CHANGE_REQUEST_RUN_DIR": role_output_dir.as_posix(),
        "LOOPPLANE_CHANGE_REQUEST_RUN_DIR_REL": _path_for_record(project, role_output_dir),
    }
    try:
        adapter_input = AdapterInput.from_runner_config(
            run_id=run_id,
            workflow_id=workflow_id,
            runner_config=runner,
            prompt_path=prompt_path,
            prompt_content=prompt_text,
            scheduler_run_dir=role_output_dir,
            role_output_dir=role_output_dir,
            task_id=None,
            task_evidence_run_dir=None,
            cwd=str(_resolve_cwd(project, runner.cwd)),
            env=env,
            role="change_request_planner",
        )
        adapter_input.write_json(role_output_dir / ADAPTER_INPUT_FILENAME)
        adapter_output = adapter.run(adapter_input)
        adapter_output.write_json()
    except (AdapterContractError, NotImplementedError, OSError, ValueError) as error:
        result["ran"] = True
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    except Exception as error:  # pragma: no cover - defensive adapter isolation.
        result["ran"] = True
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    result.update(
        {
            "ran": True,
            "ok": adapter_output.exit_code == 0 and not adapter_output.timed_out,
            "exit_code": adapter_output.exit_code,
            "timed_out": adapter_output.timed_out,
            "stdout_path": _path_for_record(project, adapter_output.stdout_path),
            "stderr_path": _path_for_record(project, adapter_output.stderr_path),
            "final_output_path": _path_for_record(project, adapter_output.final_output_path),
            "adapter_result_path": _path_for_record(project, adapter_output.adapter_result_path),
            "produced_files": [_path_for_record(project, path) for path in adapter_output.produced_files],
            "final_output_excerpt": _read_text_excerpt(adapter_output.final_output_path),
        }
    )
    return result


def _patch_task_ids(patch_text: str) -> list[str]:
    tasks = parse_plan_tasks(patch_text)
    ids = [task_id for task_id in tasks if task_id]
    if ids:
        return ids
    regex_ids = re.findall(r"^\s*(?:[-+]\s*)?- \[[ xX]\]\s+([A-Za-z0-9_.:-]+):", patch_text, flags=re.MULTILINE)
    return _dedupe_text(regex_ids)


def _plan_patch_type(
    patch_text: str,
    *,
    added_tasks: Sequence[str],
    modified_tasks: Sequence[str],
    declared_type: str = "",
) -> str:
    if modified_tasks and added_tasks:
        return "mixed_tasks"
    if modified_tasks:
        return "replace_tasks"
    if added_tasks:
        normalized = declared_type.strip().lower()
        if normalized in {"insert_task_into_phase", "insert_phase_before_final_objectives", "append_tasks"}:
            return normalized
        return "append_tasks"
    if PLAN_PATCH_REPLACE_BEGIN in patch_text or PLAN_PATCH_REPLACE_END in patch_text:
        return "replace_tasks"
    return declared_type.strip() or "append_tasks"


def _change_request_plan_patch_operation(
    *,
    plan_text: str,
    patch_text: str,
    declared_type: str,
) -> str | None:
    has_append_markers = PLAN_PATCH_APPEND_BEGIN in patch_text or PLAN_PATCH_APPEND_END in patch_text
    has_replace_markers = PLAN_PATCH_REPLACE_BEGIN in patch_text or PLAN_PATCH_REPLACE_END in patch_text
    if has_append_markers and has_replace_markers:
        return None
    if has_replace_markers:
        return PLAN_PATCH_OPERATION_REPLACE_TASKS
    if has_append_markers:
        return PLAN_PATCH_OPERATION_APPEND

    patch_task_ids = set(_patch_task_ids(patch_text))
    existing_task_ids = set(parse_plan_tasks(plan_text))
    added = patch_task_ids.difference(existing_task_ids)
    modified = patch_task_ids.intersection(existing_task_ids)
    if added and modified:
        return None
    explicit_replace = declared_type.strip().lower() in {"modify_tasks", "replace_task", "replace_tasks"} or re.search(
        r"(?im)^(?:\s*-\s*)?operation:\s*.*\b(?:modify|replace)\b|^##\s+(?:modify|replace)\b",
        patch_text,
    ) is not None
    if modified and not added and explicit_replace:
        return PLAN_PATCH_OPERATION_REPLACE_TASKS
    return PLAN_PATCH_OPERATION_APPEND


def _has_text(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _read_text_excerpt(path: Path, *, limit: int = 1200) -> str:
    text = _safe_read(path).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return _dedupe_text([str(item) for item in value if str(item).strip()])


def _plan_patch_apply_contract(response: Mapping[str, Any]) -> dict[str, Any]:
    response_patch = _mapping(response.get("plan_patch"))
    agent_response = _mapping(response.get("agent_response"))
    agent_patch = _mapping(agent_response.get("plan_patch"))
    response_impact = _mapping(response.get("impact"))
    agent_impact = _mapping(agent_response.get("impact"))
    operation = str(
        agent_patch.get("operation")
        or agent_patch.get("plan_patch_operation")
        or response_patch.get("operation")
        or response_patch.get("plan_patch_operation")
        or PLAN_PATCH_OPERATION_APPEND
    ).strip()
    target_phase_id = str(
        agent_patch.get("target_phase_id")
        or agent_patch.get("phase_id")
        or response_patch.get("target_phase_id")
        or response_patch.get("phase_id")
        or ""
    ).strip()
    supersede_task_ids = _text_list(
        response_impact.get("supersedes_tasks")
        if response_impact.get("supersedes_tasks") is not None
        else agent_impact.get("supersedes_tasks")
    )
    return {
        "operation": operation or PLAN_PATCH_OPERATION_APPEND,
        "target_phase_id": target_phase_id or None,
        "supersede_task_ids": list(dict.fromkeys(supersede_task_ids)),
    }


def _dedupe_text(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = str(cwd or "{{project_root}}").replace("{{project_root}}", project.as_posix())
    return Path(expanded).expanduser().resolve()


def change_request_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def format_change_request_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane change-request: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    request = result.get("change_request")
    if isinstance(request, Mapping):
        lines.append(f"change_request_id: {request.get('change_request_id')}")
        lines.append(f"request_status: {request.get('status')}")
    response = result.get("response") or result.get("planner_response") or result.get("apply_response")
    if isinstance(response, Mapping):
        lines.append(f"response_id: {response.get('response_id')}")
        patch = response.get("plan_patch")
        if isinstance(patch, Mapping) and patch.get("patch_file"):
            lines.append(f"plan_patch: {patch.get('patch_file')}")
        if response.get("approval_request_id"):
            lines.append(f"approval_id: {response.get('approval_request_id')}")
        if response.get("applied_plan_update_event_id"):
            lines.append(f"applied_event_id: {response.get('applied_plan_update_event_id')}")
    approval = result.get("approval_request")
    if isinstance(approval, Mapping):
        lines.append(f"approval_id: {approval.get('approval_id')}")
    records = result.get("change_requests")
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        lines.append("change_requests:")
        for record in records:
            if not isinstance(record, Mapping):
                continue
            lines.append(
                "  - "
                f"{record.get('change_request_id')}: {record.get('status')} "
                f"{record.get('user_request', '')}"
            )
    for key in ("warnings", "errors"):
        values = result.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines) + "\n"


def new_change_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"cr_{stamp}_{uuid.uuid4().hex[:8]}"


def new_change_request_response_id(*, prefix: str = "crr") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def new_change_request_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"change_request_{stamp}_{uuid.uuid4().hex[:8]}"


def _load_project_paths(project_root: Path | str) -> tuple[Path, WorkflowPaths, str]:
    project = Path(project_root).expanduser().resolve()
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    return project, paths, str(workflow.get("workflow_id") or "unknown_workflow")


def _change_request_planner_runner(project: Path, *, runner_id: str | None) -> Any:
    selected_runner_id = runner_id or "change_request_planner"
    runner = load_agent_runners(project).runner(selected_runner_id)
    if runner.role != "change_request_planner":
        raise AgentRunnerConfigError(
            f"runner {selected_runner_id!r} has role {runner.role!r}, expected 'change_request_planner'."
        )
    if not runner.enabled:
        raise AgentRunnerConfigError(f"runner {selected_runner_id!r} is disabled")
    return runner


def _select_request_for_review(paths: WorkflowPaths, change_request_id: str | None) -> dict[str, Any] | None:
    requests = _read_jsonl(paths.requests_dir / CHANGE_REQUESTS_FILENAME)
    if change_request_id:
        request = _request_by_id(requests, change_request_id)
        return request
    candidates = []
    for request in requests:
        status = change_request_status_record(paths, request)
        if status.get("status") == "pending_review":
            candidates.append(request)
    return sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[0] if candidates else None


def _request_by_id(requests: Sequence[Mapping[str, Any]], change_request_id: str | None) -> dict[str, Any] | None:
    if not change_request_id:
        return None
    for request in requests:
        if str(request.get("change_request_id") or "") == str(change_request_id):
            return dict(request)
    return None


def _latest_plan_patch_response(paths: WorkflowPaths, change_request_id: str) -> dict[str, Any] | None:
    responses = [
        response
        for response in _read_jsonl(paths.requests_dir / CHANGE_REQUEST_RESPONSES_FILENAME)
        if str(response.get("change_request_id") or "") == change_request_id and isinstance(response.get("plan_patch"), Mapping)
    ]
    return _latest_response(responses)


def _latest_response(responses: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not responses:
        return None
    return dict(sorted(responses, key=lambda item: str(item.get("created_at") or item.get("responded_at") or ""))[-1])


def _approval_check(paths: WorkflowPaths, response: Mapping[str, Any]) -> dict[str, Any]:
    impact = _mapping(response.get("impact"))
    required = bool(impact.get("requires_approval"))
    approval_id = str(response.get("approval_request_id") or "")
    if not required:
        return {"required": False, "approved": True, "message": "Approval is not required."}
    policy = load_approval_policy(paths)
    if policy.get("enabled") is not True:
        return {
            "required": False,
            "approved": True,
            "approval_id": approval_id or None,
            "message": "Interactive approval is disabled; unattended mode auto-authorized the change request.",
        }
    if not approval_id:
        return {
            "required": True,
            "approved": False,
            "approval_id": None,
            "message": "Approval is required, but no approval request exists.",
        }
    requests = _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME)
    responses = _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME)
    approval_request = None
    for request in requests:
        if str(request.get("approval_id") or request.get("request_id") or "") == approval_id:
            approval_request = request
            break
    if approval_request is None:
        return {
            "required": True,
            "approved": False,
            "approval_id": approval_id,
            "message": f"Approval request {approval_id} was not found.",
        }
    expected_plan_patch_sha256 = str(_mapping(response.get("plan_patch")).get("sha256") or "").strip()
    approval_plan_patch_sha256 = str(approval_request.get("plan_patch_sha256") or "").strip()
    if not expected_plan_patch_sha256 or approval_plan_patch_sha256 != expected_plan_patch_sha256:
        return {
            "required": True,
            "approved": False,
            "approval_id": approval_id,
            "message": (
                f"Approval {approval_id} is not bound to the reviewed PLAN_PATCH.md content; "
                "review the change request again."
            ),
        }
    status = approval_record_status(approval_request, responses=responses, now=utc_timestamp())
    return {
        "required": True,
        "approved": status.get("status") == "approved",
        "approval_id": approval_id,
        "approval": status,
        "message": f"Approval {approval_id} is {status.get('status')}.",
    }


def _record_change_request_approval(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    request: Mapping[str, Any],
    patch_path: Path,
    plan_patch_sha256: str | None,
) -> dict[str, Any]:
    change_request_id = str(request.get("change_request_id") or "")
    normalized_patch_sha256 = str(plan_patch_sha256 or "").strip()
    existing = _existing_pending_change_request_approval(
        paths,
        change_request_id,
        plan_patch_sha256=normalized_patch_sha256,
    )
    if existing is not None:
        return existing
    approval_id = new_approval_id()
    _supersede_stale_change_request_approvals(
        paths,
        change_request_id=change_request_id,
        plan_patch_sha256=normalized_patch_sha256,
        superseded_by_approval_id=approval_id,
    )
    approval = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "created_at": utc_timestamp(),
        "workflow_id": workflow_id,
        "type": "change_request_plan_patch",
        "change_request_id": change_request_id,
        "request_id": change_request_id,
        "message": f"Approve PLAN_PATCH.md for change request {change_request_id}.",
        "scope": f"change_request {change_request_id}",
        "status": "pending",
        "expires_at": default_expires_at(),
        "source": "change_request_planner",
        "source_plan_patch": _path_for_record(project, patch_path),
        "plan_patch_sha256": normalized_patch_sha256,
        "source_change_request": _path_for_record(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME),
    }
    _append_jsonl_locked(paths, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME, approval)
    return approval


def _existing_pending_change_request_approval(
    paths: WorkflowPaths,
    change_request_id: str,
    *,
    plan_patch_sha256: str,
) -> dict[str, Any] | None:
    responses = _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME)
    for request in _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME):
        if str(request.get("change_request_id") or request.get("request_id") or "") != change_request_id:
            continue
        status = approval_record_status(request, responses=responses, now=utc_timestamp())
        if (
            status.get("status") == "pending"
            and plan_patch_sha256
            and str(request.get("plan_patch_sha256") or "") == plan_patch_sha256
        ):
            return dict(request)
    return None


def _supersede_stale_change_request_approvals(
    paths: WorkflowPaths,
    *,
    change_request_id: str,
    plan_patch_sha256: str,
    superseded_by_approval_id: str,
) -> None:
    responses = _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME)
    for request in _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME):
        if str(request.get("type") or "") != "change_request_plan_patch":
            continue
        if str(request.get("change_request_id") or request.get("request_id") or "") != change_request_id:
            continue
        status = approval_record_status(request, responses=responses, now=utc_timestamp())
        if status.get("status") != "pending":
            continue
        if plan_patch_sha256 and str(request.get("plan_patch_sha256") or "") == plan_patch_sha256:
            continue
        response = {
            "schema_version": SCHEMA_VERSION,
            "approval_id": str(request.get("approval_id") or request.get("request_id") or ""),
            "responded_at": utc_timestamp(),
            "decision": "superseded",
            "approved_by": "loopplane",
            "scope": str(request.get("scope") or ""),
            "notes": "PLAN_PATCH.md content changed; a new content-bound approval is required.",
            "source": "change_request_content_binding",
            "workflow_id": str(request.get("workflow_id") or ""),
            "type": "change_request_plan_patch",
            "superseded_by_approval_id": superseded_by_approval_id,
        }
        _append_jsonl_locked(paths, paths.runtime_dir / APPROVAL_RESPONSES_FILENAME, response)
        responses.append(response)


def _write_plan_patch_audit(
    path: Path,
    *,
    project: Path,
    workflow_id: str,
    run_id: str,
    change_request_id: str,
    patch_path: Path,
    added_tasks: Sequence[str],
    modified_tasks: Sequence[str],
    patch_type: str,
    plan_patch_sha256: str | None,
) -> dict[str, Any]:
    operation = {
        "append_tasks": PLAN_PATCH_OPERATION_APPEND,
        "replace_tasks": PLAN_PATCH_OPERATION_REPLACE_TASKS,
    }.get(patch_type)
    if operation is None:
        validation = {
            "ok": False,
            "status": "unsupported_patch_type",
            "errors": [f"Unsupported or mixed change-request patch type: {patch_type}"],
        }
    else:
        validation = apply_approved_plan_patch(
            project,
            change_request_id=change_request_id,
            plan_patch_path=patch_path,
            plan_patch_operation=operation,
            expected_plan_patch_sha256=plan_patch_sha256,
            write=False,
        )
    blocking_findings = list(validation.get("errors") or []) if validation.get("ok") is not True else []
    passed = not blocking_findings
    report = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "change_request_id": change_request_id,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "audited_at": utc_timestamp(),
        "plan_patch_path": patch_path.as_posix(),
        "plan_patch_sha256": plan_patch_sha256,
        "patch_type": patch_type,
        "added_tasks": list(added_tasks),
        "modified_tasks": list(modified_tasks),
        "blocking_findings": blocking_findings,
        "deterministic_validation": validation,
        "auditor_boundary": "plan_patch_only",
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _planning_config(paths: WorkflowPaths) -> Mapping[str, Any]:
    try:
        workflow = load_workflow_config(paths.project_root)
    except (OSError, json.JSONDecodeError, WorkflowPathError):
        return {}
    planning = workflow.get("planning")
    return planning if isinstance(planning, Mapping) else {}


def _next_change_task_id(existing_tasks: Mapping[str, Any]) -> str:
    used = set(existing_tasks)
    for index in range(1, 10000):
        candidate = f"CR.T{index:03d}"
        if candidate not in used:
            return candidate
    return f"CR.T{uuid.uuid4().hex[:8]}"


def _change_task_title(user_request: str) -> str:
    text = " ".join(user_request.strip().split())
    text = re.sub(r"^(please\s+)?(add|change|update|modify|create)\s+", "", text, flags=re.IGNORECASE)
    text = text.rstrip(".")
    if not text:
        text = "Apply approved change request"
    return text[:1].upper() + text[1:160]


def _update_runtime_state(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    status: str,
    change_request_update: Mapping[str, Any],
    requires_attention: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_object(state_path, default={})
    if not isinstance(state, dict):
        state = {}
    state["schema_version"] = str(state.get("schema_version") or SCHEMA_VERSION)
    state["workflow_id"] = str(state.get("workflow_id") or workflow_id)
    state["status"] = status
    state["updated_at"] = utc_timestamp()
    if status == "plan_updated":
        plan_sha = _sha256_file(paths.plan_file)
        if plan_sha:
            state["active_plan_sha256"] = plan_sha
        state.pop("manual_plan_change", None)
        state["configuration_problems"] = [
            dict(problem)
            for problem in state.get("configuration_problems", [])
            if isinstance(problem, Mapping) and str(problem.get("code") or "") != "manual_plan_change_detected"
        ]
    change_requests = state.get("change_requests")
    if not isinstance(change_requests, dict):
        change_requests = {}
    change_requests.update(dict(change_request_update))
    if status == "plan_updated" and state.get("active_plan_sha256"):
        change_requests["active_plan_sha256"] = state["active_plan_sha256"]
    change_requests["heartbeat_at"] = utc_timestamp()
    state["change_requests"] = change_requests
    if requires_attention is not None:
        state["requires_attention"] = [dict(item) for item in requires_attention]
    _atomic_write_json(state_path, state)


def _mark_waiting_config(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    change_request_id: str,
    reason: str,
    checkpoint: Mapping[str, Any],
) -> None:
    _update_runtime_state(
        paths,
        workflow_id=workflow_id,
        status="waiting_config",
        change_request_update={
            "last_action": "apply_change_request",
            "last_change_request_id": change_request_id,
            "configuration_problem": reason,
            "checkpoint_errors": list(checkpoint.get("errors") or []),
        },
    )
    append_event(
        paths,
        workflow_id=workflow_id,
        event_type="change_request_apply_blocked",
        data={
            "request_id": change_request_id,
            "change_request_id": change_request_id,
            "message": reason,
            "errors": list(checkpoint.get("errors") or []),
        },
    )


def _checkpoint_id(result: Mapping[str, Any]) -> str | None:
    checkpoint = result.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    value = checkpoint.get("checkpoint_id")
    return str(value) if value else None


def _resolve_project_path(project: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowPathError("plan_patch.patch_file is missing")
    path = Path(value)
    return path if path.is_absolute() else project / value


def _failure(
    *,
    project: Path,
    workflow_id: str,
    status: str,
    message: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "errors": [message],
        "warnings": [],
    }
    if extra:
        result.update(dict(extra))
    return result


def _append_jsonl_locked(paths: WorkflowPaths, path: Path, record: Mapping[str, Any]) -> None:
    owner = f"change-request-jsonl:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "change_request_lock", owner, ttl_seconds=30)
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


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _render_template(template: str, variables: Mapping[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _path_for_record(project: Path | None, path: Path) -> str:
    if project is not None:
        try:
            return path.resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()
