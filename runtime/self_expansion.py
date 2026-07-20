from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.adapters.base import utc_timestamp
from runtime.approval import (
    approval_record_status,
    default_expires_at,
    load_approval_policy,
    new_approval_id,
    read_approval_requests,
    read_approval_responses,
)
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import parse_plan_objectives
from runtime.prompt_context import data_reference, file_reference, json_summary, prompt_reference_index
from runtime.read_model_requests import request_read_model_rebuild
from runtime.reconciliation import (
    PLAN_PATCH_APPEND_BEGIN,
    PLAN_PATCH_APPEND_END,
    PLAN_PATCH_OPERATION_APPEND,
    PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES,
    PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE,
    apply_approved_plan_patch,
    parse_plan_tasks,
)
from runtime.schema_validation import validate_json_value_against_schema


SCHEMA_VERSION = "1.5"
EXPANSION_REGISTRY_FILENAME = "expansion_registry.json"
EXPANSION_PROPOSAL_FILENAME = "expansion_proposal.json"
EXPANSION_SELECTION_FILENAME = "expansion_selection.json"
EXPANSION_PLAN_PATCH_FILENAME = "PLAN_PATCH.md"
EXPANSION_REPORT_FILENAME = "EXPANSION_REPORT.md"
EXPANSION_CONTEXT_MANIFEST_FILENAME = "expansion_context_manifest.json"
NON_SCIENTIFIC_EXPANSION_FAILURE_CLASSES = frozenset(
    {"background_job_failed", "objective_verifier_failed", "expansion_planner_failed"}
)
SELF_EXPANSION_POLICY_CONTEXT_FILENAME = "self_expansion_policy.json"
EXPANSION_REGISTRY_CONTEXT_FILENAME = "expansion_registry_snapshot.json"
ALLOWED_RESOLUTION_STRATEGIES = frozenset(
    {
        "append_followup_only",
        "reopen_failure_after_new_evidence",
        "supersede_task_with_approval",
        "requires_human",
    }
)
ALLOWED_EXPANSION_TYPES = frozenset(
    {
        "evidence_gap",
        "failed_recovery_decomposition",
        "stale_or_partial_task",
        "missing_deliverable",
        "contradictory_results",
        "validation_gap",
        "scope_clarification_required",
        "final_verifier_retry",
        "no_executable_work",
        "objective_gap",
    }
)
ALLOWED_RISKS = frozenset({"low", "medium", "high"})
RESOLVED_FAILURE_STATUSES = frozenset({"recovered", "waived"})
UNRESOLVED_FAILURE_STATUSES = frozenset({"unrecovered", "recovering", "exhausted", "needs_human"})
DEFAULT_SELF_EXPANSION_POLICY: dict[str, Any] = {
    "enabled": True,
    "default_mode": "append_only",
    "max_cycles": 100,
    "max_tasks_added_total": 100,
    "max_tasks_per_cycle": 100,
    "max_repeated_signature_count": 100,
    "auto_apply_low_risk": True,
    "require_approval_for_medium_risk": True,
    "require_approval_for_high_risk": True,
    "allow_after_recovery_exhausted": True,
    "allow_after_final_verification_failure": True,
    "allow_when_no_executable_tasks": True,
    "allow_after_objective_gap": True,
    "auto_apply_objective_gap_low_medium_risk": True,
}


def load_self_expansion_policy(workflow_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = workflow_config.get("self_expansion") if isinstance(workflow_config, Mapping) else None
    policy = dict(DEFAULT_SELF_EXPANSION_POLICY)
    if isinstance(raw, Mapping):
        for key in policy:
            if key in raw:
                policy[key] = raw[key]
    policy["enabled"] = bool(policy.get("enabled"))
    for key in (
        "max_cycles",
        "max_tasks_added_total",
        "max_tasks_per_cycle",
        "max_repeated_signature_count",
    ):
        try:
            policy[key] = max(0, int(policy.get(key, DEFAULT_SELF_EXPANSION_POLICY[key])))
        except (TypeError, ValueError):
            policy[key] = DEFAULT_SELF_EXPANSION_POLICY[key]
    for key in (
        "auto_apply_low_risk",
        "require_approval_for_medium_risk",
        "require_approval_for_high_risk",
        "allow_after_recovery_exhausted",
        "allow_after_final_verification_failure",
        "allow_when_no_executable_tasks",
        "allow_after_objective_gap",
        "auto_apply_objective_gap_low_medium_risk",
    ):
        policy[key] = bool(policy.get(key))
    if str(policy.get("default_mode") or "") != "append_only":
        policy["default_mode"] = "append_only"
    policy["schema_version"] = SCHEMA_VERSION
    return policy


def registry_path(paths: WorkflowPaths) -> Path:
    return paths.runtime_dir / EXPANSION_REGISTRY_FILENAME


def initial_registry(workflow_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "cycle": 0,
        "events": [],
        "proposals": [],
    }


def read_expansion_registry(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    path = registry_path(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return initial_registry(workflow_id)
    if not isinstance(data, Mapping):
        return initial_registry(workflow_id)
    registry = dict(data)
    registry["schema_version"] = str(registry.get("schema_version") or SCHEMA_VERSION)
    registry["workflow_id"] = str(registry.get("workflow_id") or workflow_id)
    events = registry.get("events")
    proposals = registry.get("proposals")
    registry["events"] = [dict(item) for item in events if isinstance(item, Mapping)] if isinstance(events, list) else []
    registry["proposals"] = (
        [dict(item) for item in proposals if isinstance(item, Mapping)] if isinstance(proposals, list) else []
    )
    try:
        registry["cycle"] = max(0, int(registry.get("cycle") or 0))
    except (TypeError, ValueError):
        registry["cycle"] = len(registry["proposals"])
    return registry


def write_expansion_registry(paths: WorkflowPaths, registry: Mapping[str, Any]) -> None:
    _atomic_write_json(registry_path(paths), dict(registry))


def append_registry_event(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    event_type: str,
    proposal_id: str | None = None,
    run_id: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"exp_evt_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "event_type": event_type,
        "workflow_id": workflow_id,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "created_at": utc_timestamp(),
        "data": dict(data or {}),
    }
    registry["events"].append(event)
    registry["updated_at"] = event["created_at"]
    write_expansion_registry(paths, registry)
    return event


def load_expansion_status(project_root: Path | str) -> dict[str, Any]:
    project, paths, workflow_id, workflow_config = _load_project(project_root)
    policy = load_self_expansion_policy(workflow_config)
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    proposal_records = registry.get("proposals", [])
    objective_statuses = _current_objective_statuses(paths)
    proposals = [
        _proposal_with_current_objective_resolution(proposal, objective_statuses=objective_statuses)
        for proposal in proposal_records
        if isinstance(proposal, Mapping)
    ] if isinstance(proposal_records, list) else []
    latest = proposals[-1] if isinstance(proposals, list) and proposals else None
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "enabled" if policy.get("enabled") else "disabled",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "policy": policy,
        "registry_path": _path_for_record(project, registry_path(paths)),
        "cycle": registry.get("cycle", 0),
        "proposal_count": len(proposals),
        "latest_proposal": dict(latest) if isinstance(latest, Mapping) else None,
        "errors": [],
        "warnings": [],
    }


def expansion_candidate(snapshot: Mapping[str, Any], *, mode: str = "default") -> dict[str, Any] | None:
    workflow_config = snapshot.get("workflow_config")
    paths = snapshot.get("paths")
    if not isinstance(workflow_config, Mapping) or not isinstance(paths, WorkflowPaths):
        return None
    policy = load_self_expansion_policy(workflow_config)
    if not policy.get("enabled"):
        return None
    workflow_id = str(snapshot.get("workflow_id") or workflow_config.get("workflow_id") or "unknown_workflow")
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    budget_problem = expansion_budget_problem(policy, registry)
    if budget_problem is not None:
        return {
            "trigger": "self_expansion_budget_exhausted",
            "run_kind": "self_expansion_budget",
            "policy": policy,
            "registry": _compact_registry(registry),
            "problem": budget_problem,
        }

    if mode in {"default", "final_failure"} and policy.get("allow_after_final_verification_failure"):
        final_report = _latest_final_verifier_failure(paths)
        if final_report is not None:
            blockers = [dict(item) for item in final_report.get("blockers", []) if isinstance(item, Mapping)]
            expandable = [item for item in blockers if _final_verifier_blocker_allows_expansion(item)]
            if expandable or not blockers:
                return {
                    "trigger": "final_verification_failed",
                    "run_kind": "self_expansion",
                    "policy": policy,
                    "registry": _compact_registry(registry),
                    "final_verifier_report": _path_for_record(paths.project_root, paths.runtime_dir / "final_verification_report.json"),
                    "blockers": expandable or blockers,
                }

    if mode in {"default", "objective_gap"} and policy.get("allow_after_objective_gap"):
        objective_gap = _objective_gap_from_snapshot(snapshot)
        if objective_gap is not None:
            return {
                "trigger": "objective_gap",
                "run_kind": "self_expansion",
                "policy": policy,
                "registry": _compact_registry(registry),
                **objective_gap,
            }

    if mode in {"default", "no_executable"}:
        failure = _first_exhausted_failure(snapshot)
        if failure is not None and policy.get("allow_after_recovery_exhausted"):
            return {
                "trigger": "recovery_exhausted",
                "run_kind": "self_expansion",
                "policy": policy,
                "registry": _compact_registry(registry),
                "target_task_ids": [str(failure.get("task_id") or "")],
                "target_failure_ids": [str(failure.get("failure_id") or "")],
                "failure": dict(failure),
            }
        if policy.get("allow_when_no_executable_tasks") and _has_nonterminal_tasks(snapshot):
            return {
                "trigger": "no_executable_tasks",
                "run_kind": "self_expansion",
                "policy": policy,
                "registry": _compact_registry(registry),
                "target_task_ids": _nonterminal_task_ids(snapshot),
                "non_scientific_failures": _exhausted_non_scientific_failures(snapshot),
            }
    return None


def expansion_budget_problem(policy: Mapping[str, Any], registry: Mapping[str, Any]) -> dict[str, Any] | None:
    proposals = registry.get("proposals", [])
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        proposals = []
    applied = [proposal for proposal in proposals if isinstance(proposal, Mapping) and proposal.get("status") == "applied"]
    max_cycles = int(policy.get("max_cycles") or 0)
    if max_cycles > 0 and len(applied) >= max_cycles:
        return {"code": "max_cycles_exhausted", "max_cycles": max_cycles, "applied_cycles": len(applied)}
    added_total = 0
    for proposal in applied:
        added = proposal.get("added_task_ids", [])
        if isinstance(added, Sequence) and not isinstance(added, (str, bytes)):
            added_total += len([item for item in added if str(item)])
    max_added = int(policy.get("max_tasks_added_total") or 0)
    if max_added > 0 and added_total >= max_added:
        return {"code": "max_tasks_added_total_exhausted", "max_tasks_added_total": max_added, "added_tasks": added_total}
    return None


def expansion_resolution_candidate(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    workflow_config = snapshot.get("workflow_config")
    paths = snapshot.get("paths")
    if not isinstance(workflow_config, Mapping) or not isinstance(paths, WorkflowPaths):
        return None
    policy = load_self_expansion_policy(workflow_config)
    if not policy.get("enabled"):
        return None
    workflow_id = str(snapshot.get("workflow_id") or workflow_config.get("workflow_id") or "unknown_workflow")
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    tasks = {str(task.get("task_id") or ""): str(task.get("status") or "") for task in snapshot.get("tasks", []) if isinstance(task, Mapping)}
    deferred_failure_ids = _failures_waiting_on_expansion_evidence(snapshot, tasks)
    failures = _failure_by_id(snapshot)
    for proposal in registry.get("proposals", []):
        if not isinstance(proposal, Mapping):
            continue
        if proposal.get("status") != "applied":
            continue
        if proposal.get("resolution_strategy") != "reopen_failure_after_new_evidence":
            continue
        if proposal.get("failure_resolution_status") in {"reopened", "waived", "requires_attention"}:
            continue
        added_task_ids = [str(item) for item in proposal.get("added_task_ids", []) if str(item)]
        if not added_task_ids or any(tasks.get(task_id) not in {"x", "-"} for task_id in added_task_ids):
            continue
        target_failure_ids = [str(item) for item in proposal.get("target_failure_ids", []) if str(item)]
        reopenable = [
            failure_id
            for failure_id in target_failure_ids
            if str(failures.get(failure_id, {}).get("status") or "") == "exhausted"
            and failure_id not in deferred_failure_ids
        ]
        if reopenable:
            return {
                "run_kind": "self_expansion_resolution",
                "proposal_id": str(proposal.get("proposal_id") or ""),
                "target_failure_ids": reopenable,
                "added_task_ids": added_task_ids,
            }
    return None


def validate_expansion_proposal(
    project_root: Path | str,
    proposal_path: Path | str,
    *,
    write_registry: bool = False,
) -> dict[str, Any]:
    project, paths, workflow_id, workflow_config = _load_project(project_root)
    proposal_file = _resolve_project_path(project, proposal_path)
    proposal, read_error, proposal_sha256 = _read_json_object(proposal_file)
    if read_error is not None:
        return _validation_result(
            project=project,
            workflow_id=workflow_id,
            proposal_path=proposal_file,
            status="invalid_proposal",
            errors=[read_error],
        )
    assert proposal is not None
    assert proposal_sha256 is not None
    return _validate_proposal_mapping(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        workflow_config=workflow_config,
        proposal=proposal,
        proposal_path=proposal_file,
        proposal_sha256=proposal_sha256,
        write_registry=write_registry,
    )


def apply_expansion_proposal(
    project_root: Path | str,
    proposal_path: Path | str,
    *,
    run_id: str | None = None,
    runner_id: str | None = None,
    owner: str = "self_expansion",
) -> dict[str, Any]:
    project, paths, workflow_id, workflow_config = _load_project(project_root)
    proposal_file = _resolve_project_path(project, proposal_path)
    validation = validate_expansion_proposal(project, proposal_file)
    if not validation.get("ok"):
        record = _record_proposal(
            paths,
            workflow_id=workflow_id,
            validation=validation,
            status="validation_failed",
            run_id=run_id,
            runner_id=runner_id,
        )
        _append_scheduler_event(
            paths,
            workflow_id=workflow_id,
            run_id=run_id or str(validation.get("proposal_id") or "self_expansion_validation_failed"),
            event_type="self_expansion_proposal_validation_failed",
            data={
                "proposal_id": validation.get("proposal_id"),
                "status": "validation_failed",
                "errors": list(validation.get("errors", [])),
                "registry_record": record,
            },
        )
        read_model_result = request_read_model_rebuild(
            paths,
            workflow_id=workflow_id,
            run_id=run_id or str(validation.get("proposal_id") or "self_expansion_validation_failed"),
            reason="self_expansion_proposal_validation_failed",
            requested_by=owner,
            source_path=proposal_file,
            extra={
                "proposal_id": validation.get("proposal_id"),
                "validation_status": validation.get("status"),
                "registry_record": record,
            },
        )
        return {
            **dict(validation),
            "status": "validation_failed",
            "validation_status": validation.get("status"),
            "registry_record": record,
            "read_model_rebuild": read_model_result,
        }
    proposal = validation["proposal"]
    proposal_id = str(proposal.get("proposal_id") or validation.get("proposal_id") or f"exp_{uuid.uuid4().hex[:8]}")
    approval_required = _proposal_requires_approval(proposal, validation.get("policy", {}))
    strategy = str(proposal.get("resolution_strategy") or "")
    patch_path = Path(str(validation["plan_patch_path"])).resolve()
    approval_binding = {
        "proposal_sha256": str(validation.get("proposal_sha256") or ""),
        "plan_patch_sha256": str(validation.get("plan_patch_sha256") or ""),
    }
    current_binding = _expansion_approval_binding(
        proposal_path=proposal_file,
        plan_patch_path=patch_path,
    )
    if (
        not approval_binding["proposal_sha256"]
        or current_binding["proposal_sha256"] != approval_binding["proposal_sha256"]
        or (
            approval_binding["plan_patch_sha256"]
            and current_binding["plan_patch_sha256"] != approval_binding["plan_patch_sha256"]
        )
    ):
        return _apply_no_mutation_result(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            proposal=proposal,
            proposal_path=proposal_file,
            status="proposal_content_changed",
            message="Expansion proposal or PLAN_PATCH.md changed during validation; validate the current content again.",
            run_id=run_id,
            runner_id=runner_id,
            owner=owner,
        )

    if strategy == "requires_human":
        approval_policy = load_approval_policy(paths)
        if _autonomous_recovery_enabled(approval_policy):
            return _apply_no_mutation_result(
                project=project,
                paths=paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                status="autonomous_resolution_required",
                message=(
                    "Fully autonomous workflow rejected a human handoff; the expansion planner must produce "
                    "an executable repair or new-evidence recovery path."
                ),
                run_id=run_id,
                runner_id=runner_id,
                owner=owner,
            )
        result = _apply_no_mutation_result(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            proposal=proposal,
            proposal_path=proposal_file,
            status="requires_attention",
            message="Expansion proposal requires human input and did not mutate PLAN.md.",
            run_id=run_id,
            runner_id=runner_id,
            owner=owner,
        )
        return result

    approval_resolution: Mapping[str, Any] | None = None
    if approval_required:
        if not all(approval_binding.values()):
            return _apply_no_mutation_result(
                project=project,
                paths=paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                status="requires_attention",
                message="Expansion approval could not be bound because the proposal or PLAN_PATCH.md was unavailable.",
                run_id=run_id,
                runner_id=runner_id,
                owner=owner,
            )
        approval_policy = load_approval_policy(paths)
        approval_resolution = _expansion_approval_record(
            paths,
            proposal_id=proposal_id,
            binding=approval_binding,
        )
        if approval_resolution is not None and approval_resolution.get("status") == "approved":
            pass
        elif approval_resolution is not None and approval_resolution.get("status") == "pending":
            policy_enabled = approval_policy.get("enabled") is True
            return _apply_no_mutation_result(
                project=project,
                paths=paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                status="approval_required" if policy_enabled else "requires_attention",
                message=(
                    "Expansion proposal is waiting for approval."
                    if policy_enabled
                    else (
                        "Expansion proposal is waiting for an explicit approval response; interactive approval "
                        "is disabled, so an operator must use the approval override control path."
                    )
                ),
                run_id=run_id,
                runner_id=runner_id,
                owner=owner,
                approval=approval_resolution,
            )
        elif approval_resolution is not None:
            return _apply_no_mutation_result(
                project=project,
                paths=paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                status="requires_attention",
                message=(
                    "Expansion proposal approval is "
                    f"{approval_resolution.get('status')}; PLAN.md was not mutated."
                ),
                run_id=run_id,
                runner_id=runner_id,
                owner=owner,
                approval=approval_resolution,
            )
        else:
            approval_resolution = _record_expansion_approval_request(
                paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                plan_patch_path=patch_path,
                binding=approval_binding,
                run_id=run_id,
            )
            policy_enabled = approval_policy.get("enabled") is True
            return _apply_no_mutation_result(
                project=project,
                paths=paths,
                workflow_id=workflow_id,
                proposal=proposal,
                proposal_path=proposal_file,
                status="approval_required" if policy_enabled else "requires_attention",
                message=(
                    "Expansion proposal requires approval and did not mutate PLAN.md."
                    if policy_enabled
                    else (
                        "Expansion proposal requires approval and did not mutate PLAN.md; interactive approval "
                        "is disabled, so an operator must use the approval override control path."
                    )
                ),
                run_id=run_id,
                runner_id=runner_id,
                owner=owner,
                approval=approval_resolution,
            )

    plan_patch_operation = str(validation.get("plan_patch_operation") or _plan_patch_operation_for_proposal(proposal))
    target_phase_id = str(validation.get("target_phase_id") or proposal.get("target_phase_id") or proposal.get("phase_id") or "").strip() or None
    apply_result = apply_approved_plan_patch(
        project,
        change_request_id=f"self_expansion:{proposal_id}",
        plan_patch_path=patch_path,
        response_id=_expansion_approval_response_id(approval_resolution) or str(run_id or ""),
        approval_request_id=(
            str(approval_resolution.get("approval_id") or "") if isinstance(approval_resolution, Mapping) else ""
        ),
        plan_patch_operation=plan_patch_operation,
        target_phase_id=target_phase_id,
        expected_plan_patch_sha256=approval_binding.get("plan_patch_sha256") or None,
        write=True,
    )
    if isinstance(approval_resolution, Mapping):
        apply_result = {
            **dict(apply_result),
            "approval": dict(approval_resolution),
            "warnings": [
                *list(apply_result.get("warnings", [])),
                "The expansion plan patch was applied under an explicit recorded approval response.",
            ],
        }
    objective_followup_result: Mapping[str, Any] | None = None
    if apply_result.get("ok") and str(proposal.get("expansion_type") or "") == "objective_gap":
        from runtime.objective_verification import apply_objective_expansion_followups

        objective_followup_result = apply_objective_expansion_followups(
            project,
            proposal,
            added_task_ids=[str(item) for item in apply_result.get("added_tasks", []) if str(item)],
            added_phase_ids=[str(item) for item in apply_result.get("added_phase_ids", []) if str(item)],
            owner=owner,
        )
        apply_result = {
            **dict(apply_result),
            "objective_followup_update": dict(objective_followup_result),
        }
        if not objective_followup_result.get("ok"):
            apply_result["warnings"] = [
                *list(apply_result.get("warnings", [])),
                *[str(error) for error in objective_followup_result.get("errors", [])],
            ]
    status = "applied" if apply_result.get("ok") else str(apply_result.get("status") or "apply_failed")
    record = _record_proposal(
        paths,
        workflow_id=workflow_id,
        validation=validation,
        status=status,
        run_id=run_id,
        runner_id=runner_id,
        apply_result=apply_result,
    )
    _append_scheduler_event(
        paths,
        workflow_id=workflow_id,
        run_id=run_id or proposal_id,
        event_type="self_expansion_plan_patch_applied" if apply_result.get("ok") else "self_expansion_proposal_rejected",
        data={
            "proposal_id": proposal_id,
            "status": status,
            "plan_patch_path": _path_for_record(project, patch_path),
            "added_task_ids": apply_result.get("added_tasks", []),
            "added_phase_ids": apply_result.get("added_phase_ids", []),
            "plan_patch_operation": apply_result.get("plan_patch_operation"),
            "resolution_strategy": strategy,
            "target_objective_ids": [str(item) for item in proposal.get("target_objective_ids", []) if str(item)],
            "objective_followup_update": dict(objective_followup_result) if isinstance(objective_followup_result, Mapping) else None,
            "registry_record": record,
        },
    )
    read_model_result = request_read_model_rebuild(
        paths,
        workflow_id=workflow_id,
        run_id=run_id or proposal_id,
        reason="self_expansion_plan_patch_applied",
        requested_by=owner,
        source_path=patch_path,
        extra={
            "proposal_id": proposal_id,
            "proposal_status": status,
            "added_task_ids": list(apply_result.get("added_tasks", [])),
            "added_phase_ids": list(apply_result.get("added_phase_ids", [])),
            "plan_patch_operation": apply_result.get("plan_patch_operation"),
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(apply_result.get("ok")),
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "runner_id": runner_id,
        "resolution_strategy": strategy,
        "approval_required": False,
        "approval": dict(approval_resolution) if isinstance(approval_resolution, Mapping) else None,
        "target_objective_ids": [str(item) for item in proposal.get("target_objective_ids", []) if str(item)],
        "objective_verification_report": proposal.get("objective_verification_report"),
        "objective_followup_update": dict(objective_followup_result) if isinstance(objective_followup_result, Mapping) else None,
        "plan_patch_apply": apply_result,
        "added_task_ids": list(apply_result.get("added_tasks", [])),
        "added_phase_ids": list(apply_result.get("added_phase_ids", [])),
        "plan_patch_operation": apply_result.get("plan_patch_operation"),
        "registry_record": record,
        "read_model_rebuild": read_model_result,
        "errors": list(apply_result.get("errors", [])),
        "warnings": list(apply_result.get("warnings", [])),
    }


def reopen_expansion_failures(
    project_root: Path | str,
    *,
    proposal_id: str,
    target_failure_ids: Sequence[str],
    added_task_ids: Sequence[str],
    owner: str = "self_expansion",
) -> dict[str, Any]:
    project, paths, workflow_id, _workflow_config = _load_project(project_root)
    now = utc_timestamp()
    target_ids = [str(item) for item in target_failure_ids if str(item)]
    changed: list[dict[str, Any]] = []
    registry_path_failure = paths.runtime_dir / "failure_registry.json"
    failure_registry = _read_json_object_default(registry_path_failure, {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "failures": []})
    failures = failure_registry.get("failures", [])
    if not isinstance(failures, list):
        failures = []
        failure_registry["failures"] = failures
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("failure_id") or "") not in target_ids:
            continue
        if str(failure.get("status") or "") != "exhausted":
            continue
        failure["status"] = "unrecovered"
        failure["recoverable"] = True
        failure["budget_remaining"] = True
        failure["recovery_attempts"] = 0
        failure["max_recovery_attempts"] = 1
        failure["reopened_reason"] = "self_expansion_new_evidence"
        failure["reopened_at"] = now
        failure["reopened_by"] = owner
        failure["reopened_by_expansion_proposal_id"] = proposal_id
        failure["reopened_after_task_ids"] = [str(item) for item in added_task_ids if str(item)]
        failure.pop("exhausted_reason", None)
        changed.append(dict(failure))
    _atomic_write_json(registry_path_failure, failure_registry)
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    for proposal in registry.get("proposals", []):
        if not isinstance(proposal, dict) or str(proposal.get("proposal_id") or "") != proposal_id:
            continue
        proposal["failure_resolution_status"] = "reopened" if changed else "not_reopened"
        proposal["reopened_failure_ids"] = [str(item.get("failure_id") or "") for item in changed]
        proposal["failure_resolution_at"] = now
    write_expansion_registry(paths, registry)
    _append_scheduler_event(
        paths,
        workflow_id=workflow_id,
        run_id=proposal_id,
        event_type="self_expansion_failure_reopened",
        data={
            "proposal_id": proposal_id,
            "target_failure_ids": target_ids,
            "reopened_failure_ids": [str(item.get("failure_id") or "") for item in changed],
            "added_task_ids": [str(item) for item in added_task_ids if str(item)],
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(changed),
        "status": "reopened" if changed else "not_reopened",
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "proposal_id": proposal_id,
        "target_failure_ids": target_ids,
        "reopened_failures": changed,
        "errors": [] if changed else ["No exhausted target failures were reopened."],
        "warnings": [],
    }


def build_expansion_prompt_variables(project_root: Path | str, prepared_run: Any) -> dict[str, str]:
    project, paths, workflow_id, workflow_config = _load_project(project_root)
    proposal_schema_path = Path(__file__).resolve().parent / "schemas" / "expansion_proposal.schema.json"
    policy = load_self_expansion_policy(workflow_config)
    approval_policy = load_approval_policy(paths)
    autonomous_recovery = _autonomous_recovery_enabled(approval_policy)
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    role_output_dir = _path_attr(prepared_run, "role_output_dir")
    selection = _read_json_object_default(role_output_dir / EXPANSION_SELECTION_FILENAME, {})
    objective_report_path = _resolve_project_path(project, str(selection.get("objective_verification_report") or "")) if isinstance(selection, Mapping) and selection.get("objective_verification_report") else None
    context_manifest = _write_expansion_context_manifest(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        run_id=_string_attr(prepared_run, "run_id"),
        role_output_dir=role_output_dir,
        policy=policy,
        registry=registry,
        selection=selection if isinstance(selection, Mapping) else {},
        objective_report_path=objective_report_path,
    )
    references = context_manifest.get("references") if isinstance(context_manifest, Mapping) else {}
    brief_reference = references.get("project_brief") if isinstance(references, Mapping) else {}
    shared_reference = references.get("shared_context") if isinstance(references, Mapping) else {}
    schema_reference = references.get("proposal_schema") if isinstance(references, Mapping) else {}
    run_paths = {
        "workflow_id": workflow_id,
        "run_id": _string_attr(prepared_run, "run_id"),
        "node_id": _string_attr(prepared_run, "node_id"),
        "role": _string_attr(prepared_run, "role"),
        "runner_id": _string_attr(prepared_run, "runner_id"),
        "scheduler_run_dir": _project_relative(project, _path_attr(prepared_run, "scheduler_run_dir")),
        "role_output_dir": _project_relative(project, role_output_dir),
        "prompt_path": _project_relative(project, _path_attr(prepared_run, "prompt_path")),
        "stdout_path": _project_relative(project, _path_attr(prepared_run, "stdout_path")),
        "stderr_path": _project_relative(project, _path_attr(prepared_run, "stderr_path")),
        "final_output_path": _project_relative(project, _path_attr(prepared_run, "final_output_path")),
        "adapter_result_path": _project_relative(project, _path_attr(prepared_run, "adapter_result_path")),
        "active_run_lease_path": _project_relative(project, _path_attr(prepared_run, "active_run_lease_path")),
        "expansion_proposal_path": _project_relative(project, role_output_dir / EXPANSION_PROPOSAL_FILENAME),
        "plan_patch_path": _project_relative(project, role_output_dir / EXPANSION_PLAN_PATCH_FILENAME),
        "expansion_report_path": _project_relative(project, role_output_dir / EXPANSION_REPORT_FILENAME),
        "agent_status_path": _project_relative(project, role_output_dir / "agent_status.json"),
    }
    return {
        **run_paths,
        "schema_version": SCHEMA_VERSION,
        "brief_file": paths.value("brief_file"),
        "plan_file": paths.value("plan_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "binding_project_brief_sha256": str(brief_reference.get("sha256") or "unavailable"),
        "binding_shared_context_sha256": str(shared_reference.get("sha256") or "unavailable"),
        "proposal_schema_path": proposal_schema_path.as_posix(),
        "proposal_schema_sha256": str(schema_reference.get("sha256") or "unavailable"),
        "runtime_dir": paths.value("runtime_dir"),
        "results_dir": paths.value("results_dir"),
        "context_manifest_path": _project_relative(project, role_output_dir / EXPANSION_CONTEXT_MANIFEST_FILENAME),
        "context_references_json": json.dumps(prompt_reference_index(context_manifest["references"]), indent=2, sort_keys=True),
        "selected_expansion_candidate": json_summary(selection if isinstance(selection, Mapping) else {}, max_chars=1800),
        "autonomous_recovery_policy": (
            "This workflow is fully autonomous: human handoff and approval-only resolution strategies are forbidden. "
            "Your first priority is to solve the blocker with the available worker, recovery_worker, scheduler, and "
            "self-expansion capabilities. Use an executable follow-up or new-evidence recovery path; never emit "
            "requires_human or supersede_task_with_approval. If the ordinary worker lacks host/control-plane authority, "
            "design evidence work that allows the configured dedicated recovery_worker to retry the exhausted task."
            if autonomous_recovery
            else "Human-assisted resolution strategies remain available only when no safe executable repair exists."
        ),
    }


def _autonomous_recovery_enabled(approval_policy: Mapping[str, Any]) -> bool:
    return (
        approval_policy.get("enabled") is not True
        and str(approval_policy.get("default_action_when_disabled") or "") == "auto_authorize"
    )


def _write_expansion_context_manifest(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    run_id: str,
    role_output_dir: Path,
    policy: Mapping[str, Any],
    registry: Mapping[str, Any],
    selection: Mapping[str, Any],
    objective_report_path: Path | None,
) -> dict[str, Any]:
    references: dict[str, Any] = {
        "project_brief": file_reference(project, paths.brief_file, label="Project brief"),
        "active_plan": file_reference(project, paths.plan_file, label="Active plan"),
        "shared_context": file_reference(project, paths.shared_context_file, label="Shared workflow context"),
        "proposal_schema": file_reference(
            project,
            Path(__file__).resolve().parent / "schemas" / "expansion_proposal.schema.json",
            label="Expansion proposal schema",
        ),
        "final_verifier_report": file_reference(project, paths.runtime_dir / "final_verification_report.json", label="Latest final verifier report"),
        "failure_registry": file_reference(project, paths.runtime_dir / "failure_registry.json", label="Failure registry"),
        "expansion_selection": file_reference(project, role_output_dir / EXPANSION_SELECTION_FILENAME, label="Selected expansion candidate"),
        "self_expansion_policy": data_reference(project, role_output_dir / SELF_EXPANSION_POLICY_CONTEXT_FILENAME, policy, label="Self-expansion policy"),
        "expansion_registry": data_reference(project, role_output_dir / EXPANSION_REGISTRY_CONTEXT_FILENAME, registry, label="Expansion registry snapshot"),
    }
    if objective_report_path is not None:
        references["objective_verification_report"] = file_reference(project, objective_report_path, label="Selected objective verification report")
    manifest = {
        "schema_version": "1.0",
        "generated_at": utc_timestamp(),
        "workflow_id": workflow_id,
        "run_id": run_id,
        "source_authority": "prompt_context_manifest_not_source_of_truth",
        "instructions": [
            "The project brief and shared context are binding. Read them from their referenced paths and verify their hashes before proposing work.",
            "Use the remaining referenced files as expansion context instead of relying on summaries.",
            "The selected expansion candidate identifies the target; read referenced reports before proposing work.",
        ],
        "selection_summary": {
            "trigger": selection.get("trigger"),
            "expansion_type": selection.get("expansion_type"),
            "target_task_ids": selection.get("target_task_ids"),
            "target_failure_ids": selection.get("target_failure_ids"),
            "target_objective_ids": selection.get("target_objective_ids"),
            "objective_verification_report": selection.get("objective_verification_report"),
        },
        "references": references,
    }
    _atomic_write_json(role_output_dir / EXPANSION_CONTEXT_MANIFEST_FILENAME, manifest)
    return manifest


def _validate_proposal_mapping(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    workflow_config: Mapping[str, Any],
    proposal: Mapping[str, Any],
    proposal_path: Path,
    proposal_sha256: str,
    write_registry: bool,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    schema_errors = validate_json_value_against_schema(
        proposal,
        "expansion_proposal.schema.json",
        _path_for_record(project, proposal_path),
    )
    errors.extend(schema_errors)
    policy = load_self_expansion_policy(workflow_config)
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if str(proposal.get("workflow_id") or "") != workflow_id:
        errors.append(f"workflow_id must be {workflow_id!r}.")
    strategy = str(proposal.get("resolution_strategy") or "")
    if strategy not in ALLOWED_RESOLUTION_STRATEGIES:
        errors.append(f"resolution_strategy must be one of: {', '.join(sorted(ALLOWED_RESOLUTION_STRATEGIES))}.")
    approval_policy = load_approval_policy(paths)
    if _autonomous_recovery_enabled(approval_policy) and strategy in {
        "requires_human",
        "supersede_task_with_approval",
    }:
        errors.append(
            "Fully autonomous workflows cannot use human-handoff or approval-only resolution strategies; "
            "propose executable repair/evidence tasks and use append_followup_only or "
            "reopen_failure_after_new_evidence."
        )
    expansion_type = str(proposal.get("expansion_type") or "")
    if expansion_type not in ALLOWED_EXPANSION_TYPES:
        errors.append(f"expansion_type must be one of: {', '.join(sorted(ALLOWED_EXPANSION_TYPES))}.")
    new_tasks = proposal.get("new_tasks")
    task_count = len(new_tasks) if isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)) else 0
    if task_count < 1 and strategy not in {"requires_human", "supersede_task_with_approval"}:
        errors.append("new_tasks must contain at least one task unless the proposal requires human input or supersession approval.")
    if expansion_type == "objective_gap":
        if strategy != "append_followup_only":
            errors.append("objective_gap proposals must use append_followup_only and add structural follow-up work.")
        if task_count < 1:
            errors.append("objective_gap proposals must add structural follow-up tasks; they cannot complete as taskless human handoff proposals.")
    if policy.get("max_tasks_per_cycle") and task_count > int(policy["max_tasks_per_cycle"]):
        errors.append(f"new_tasks exceeds max_tasks_per_cycle ({policy['max_tasks_per_cycle']}).")
    target_failure_ids = [str(item) for item in proposal.get("target_failure_ids", []) if str(item)]
    target_task_ids = [str(item) for item in proposal.get("target_task_ids", []) if str(item)]
    target_objective_ids = [str(item) for item in proposal.get("target_objective_ids", []) if str(item)]
    target_phase_ids: set[str] = set()
    target_objective_scopes: set[str] = set()
    if expansion_type == "objective_gap":
        objectives, objective_errors = parse_plan_objectives(_safe_read(paths.plan_file))
        errors.extend(objective_errors)
        objective_by_id = {objective.objective_id: objective for objective in objectives}
        known_objective_ids = set(objective_by_id)
        if not target_objective_ids:
            errors.append("objective_gap proposals must include target_objective_ids.")
        for objective_id in target_objective_ids:
            if objective_id not in known_objective_ids:
                errors.append(f"target_objective_ids references missing objective: {objective_id}.")
                continue
            objective = objective_by_id[objective_id]
            target_objective_scopes.add(objective.scope)
            if objective.scope == "phase" and objective.phase_id:
                target_phase_ids.add(objective.phase_id)
        if len(target_objective_scopes) > 1:
            errors.append("objective_gap proposals must not mix phase and workflow target objectives in one expansion.")
        declared_target_phase = str(proposal.get("target_phase_id") or proposal.get("phase_id") or "").strip()
        if declared_target_phase and target_phase_ids and declared_target_phase not in target_phase_ids:
            errors.append(
                "target_phase_id must match the phase that owns target_objective_ids for phase_objective_gap proposals."
            )
        elif declared_target_phase:
            target_phase_ids.add(declared_target_phase)
        if strategy != "requires_human" and isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)):
            covered_objective_ids: set[str] = set()
            for task in new_tasks:
                if not isinstance(task, Mapping):
                    continue
                task_id = str(task.get("task_id") or "")
                objective_links = task.get("objective_links")
                links = [str(item) for item in objective_links if str(item)] if isinstance(objective_links, Sequence) and not isinstance(objective_links, (str, bytes)) else []
                if not links:
                    errors.append(f"objective_gap new task {task_id or '<unknown>'} must include objective_links.")
                    continue
                for objective_id in links:
                    if objective_id not in known_objective_ids:
                        errors.append(f"new task {task_id or '<unknown>'} objective_links references missing objective: {objective_id}.")
                    if objective_id not in target_objective_ids:
                        errors.append(f"new task {task_id or '<unknown>'} objective_links must be within target_objective_ids: {objective_id}.")
                    covered_objective_ids.add(objective_id)
            missing_linked_objectives = sorted(set(target_objective_ids) - covered_objective_ids)
            if missing_linked_objectives:
                errors.append("objective_gap new_tasks must cover every target_objective_id via objective_links: " + ", ".join(missing_linked_objectives))
        if strategy != "append_followup_only":
            warnings.append("objective_gap proposals should use append_followup_only.")
    failure_registry = _read_json_object_default(paths.runtime_dir / "failure_registry.json", {"failures": []})
    failures = _failure_by_id({"failure_registry": failure_registry})
    target_failures = [failures.get(failure_id) for failure_id in target_failure_ids]
    for failure_id, failure in zip(target_failure_ids, target_failures, strict=False):
        if not isinstance(failure, Mapping):
            errors.append(f"target_failure_ids references missing failure: {failure_id}.")
    unresolved_targets = [
        failure
        for failure in target_failures
        if isinstance(failure, Mapping) and str(failure.get("status") or "") not in RESOLVED_FAILURE_STATUSES
    ]
    exhausted_targets = [
        failure
        for failure in target_failures
        if isinstance(failure, Mapping) and str(failure.get("status") or "") == "exhausted"
    ]
    if unresolved_targets and strategy == "append_followup_only":
        errors.append("append_followup_only cannot target unresolved failures.")
    if exhausted_targets and strategy not in {
        "reopen_failure_after_new_evidence",
        "supersede_task_with_approval",
        "requires_human",
    }:
        errors.append("exhausted target failures require a failure-resolution strategy.")
    existing_plan = _safe_read(paths.plan_file)
    existing_tasks = parse_plan_tasks(existing_plan)
    target_task_phase_ids = _target_task_phase_ids(
        existing_tasks=existing_tasks,
        target_task_ids=target_task_ids,
        target_failures=[failure for failure in target_failures if isinstance(failure, Mapping)],
    )
    patch_path = _proposal_plan_patch_path(project, proposal_path, proposal)
    plan_patch_sha256 = _sha256_path(patch_path)
    declared_task_ids = [str(task.get("task_id") or "") for task in new_tasks if isinstance(task, Mapping)] if isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)) else []
    no_mutation_human_resolution = task_count == 0 and strategy in {"requires_human", "supersede_task_with_approval"}
    plan_patch_operation = _plan_patch_operation_for_proposal(
        proposal,
        target_objective_scopes=target_objective_scopes,
        target_task_phase_ids=target_task_phase_ids,
    )
    if expansion_type == "objective_gap" and plan_patch_operation == PLAN_PATCH_OPERATION_APPEND:
        errors.append("objective_gap proposals must target either phase objectives or workflow objectives for structural expansion.")
    declared_operation = str(proposal.get("plan_patch_operation") or "").strip()
    if declared_operation and declared_operation != plan_patch_operation:
        errors.append(
            "plan_patch_operation does not match objective expansion semantics: "
            f"expected {plan_patch_operation}, got {declared_operation}."
        )
    structural_target_phase_id = ""
    if plan_patch_operation == PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE:
        structural_phase_ids = set(target_phase_ids or target_task_phase_ids)
        if not structural_phase_ids:
            errors.append("phase_objective_gap proposals must target exactly one phase objective group.")
        elif len(structural_phase_ids) > 1:
            errors.append("self-expansion proposals that insert into a phase must target exactly one phase.")
        else:
            structural_target_phase_id = next(iter(structural_phase_ids))
    elif plan_patch_operation == PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES:
        if "phase" in target_objective_scopes:
            errors.append("workflow objective expansion must not target phase objectives.")

    if no_mutation_human_resolution:
        patch_tasks: list[str] = []
        patch_task_records: dict[str, Any] = {}
        added_phase_ids: list[str] = []
    else:
        patch_validation = apply_approved_plan_patch(
            project,
            change_request_id=f"self_expansion:{proposal_id or 'proposal'}",
            plan_patch_path=patch_path,
            plan_patch_operation=plan_patch_operation,
            target_phase_id=structural_target_phase_id or None,
            expected_plan_patch_sha256=plan_patch_sha256,
            write=False,
        )
        if not patch_validation.get("ok"):
            errors.extend(str(error) for error in patch_validation.get("errors", []))
        patch_tasks = [str(item) for item in patch_validation.get("added_tasks", [])]
        added_phase_ids = [str(item) for item in patch_validation.get("added_phase_ids", []) if str(item)]
        patch_task_records, patch_parse_error = _parse_plan_patch_task_records(
            patch_path,
            expected_sha256=plan_patch_sha256,
        )
        if patch_parse_error:
            errors.append(patch_parse_error)
    if declared_task_ids and sorted(declared_task_ids) != sorted(str(item) for item in patch_tasks):
        errors.append("new_tasks task_id values must match PLAN_PATCH.md appended task ids.")
    if expansion_type == "objective_gap" and target_phase_ids and str(proposal.get("trigger") or "") == "phase_objective_gap":
        phase_mismatch_errors = _objective_gap_phase_mismatch_errors(
            patch_tasks=patch_task_records.values(),
            allowed_phase_ids=target_phase_ids,
        )
        errors.extend(phase_mismatch_errors)
    if expansion_type != "objective_gap" and target_task_phase_ids:
        errors.extend(
            _target_task_phase_mismatch_errors(
                patch_tasks=patch_task_records.values(),
                allowed_phase_ids=target_task_phase_ids,
            )
        )
    if expansion_type == "objective_gap" and plan_patch_operation == PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES:
        if not added_phase_ids and not no_mutation_human_resolution:
            errors.append("workflow objective_gap proposals must add a new phase before the Final Objective Checklist.")
        declared_new_phase_id = str(proposal.get("new_phase_id") or proposal.get("followup_phase_id") or "").strip()
        if declared_new_phase_id and added_phase_ids and declared_new_phase_id not in added_phase_ids:
            errors.append(f"new_phase_id must match the phase added by PLAN_PATCH.md: {', '.join(added_phase_ids)}.")
    if expansion_type == "objective_gap" and not no_mutation_human_resolution:
        errors.extend(
            _objective_followup_schedulability_errors(
                patch_tasks=patch_task_records.values(),
                existing_tasks=existing_tasks,
                patch_task_ids={str(item) for item in patch_tasks},
            )
        )
    dependency_errors = _dependency_errors(
        new_tasks=new_tasks if isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)) else [],
        existing_task_ids=set(existing_tasks),
        patch_task_ids={str(item) for item in patch_tasks},
        unresolved_target_task_ids={
            str(failure.get("task_id") or "")
            for failure in unresolved_targets
            if isinstance(failure, Mapping) and str(failure.get("task_id") or "")
        }.union(set(target_task_ids) if unresolved_targets else set()),
        existing_tasks=existing_tasks,
    )
    errors.extend(dependency_errors)
    risk = _proposal_risk(proposal)
    if risk not in ALLOWED_RISKS:
        errors.append("proposal risk must be low, medium, or high.")
    loop_signature = str(proposal.get("loop_signature") or "") or compute_loop_signature(proposal)
    repeated = _applied_loop_signature_count(
        read_expansion_registry(paths, workflow_id=workflow_id),
        loop_signature,
    )
    max_repeated = int(policy.get("max_repeated_signature_count") or 0)
    if max_repeated > 0 and repeated >= max_repeated:
        errors.append(f"loop_signature was already applied {repeated} time(s), reaching policy limit.")
    budget_problem = expansion_budget_problem(policy, read_expansion_registry(paths, workflow_id=workflow_id))
    if budget_problem is not None:
        errors.append(f"self-expansion budget exhausted: {budget_problem['code']}.")

    result = _validation_result(
        project=project,
        workflow_id=workflow_id,
        proposal_path=proposal_path,
        status="valid" if not errors else "invalid_proposal",
        errors=errors,
        warnings=warnings,
        extra={
            "proposal": dict(proposal),
            "proposal_id": proposal_id,
            "proposal_sha256": proposal_sha256,
            "policy": policy,
            "plan_patch_path": patch_path.as_posix(),
            "plan_patch_sha256": plan_patch_sha256,
            "plan_patch_operation": plan_patch_operation,
            "target_phase_id": structural_target_phase_id or None,
            "added_task_ids": list(patch_tasks),
            "added_phase_ids": list(added_phase_ids),
            "loop_signature": loop_signature,
            "risk": risk,
            "approval_required": _proposal_requires_approval(proposal, policy),
        },
    )
    if write_registry:
        _record_proposal(
            paths,
            workflow_id=workflow_id,
            validation=result,
            status="validated" if result.get("ok") else "validation_failed",
        )
    return result


def _parse_plan_patch_task_records(
    patch_path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], str | None]:
    try:
        patch_bytes = patch_path.read_bytes()
        patch_text = patch_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return {}, f"Unable to parse PLAN_PATCH.md task records: {error}"
    actual_sha256 = "sha256:" + sha256(patch_bytes).hexdigest()
    if not expected_sha256 or actual_sha256 != expected_sha256:
        return (
            {},
            "PLAN_PATCH.md content changed during self-expansion validation; "
            f"expected {expected_sha256 or 'unavailable'}, observed {actual_sha256}.",
        )
    start = patch_text.find(PLAN_PATCH_APPEND_BEGIN)
    end = patch_text.find(PLAN_PATCH_APPEND_END)
    if start < 0 or end < 0 or end <= start:
        return {}, None
    append_block = patch_text[start + len(PLAN_PATCH_APPEND_BEGIN) : end].strip()
    if not append_block:
        return {}, None
    return parse_plan_tasks(append_block + "\n"), None


def _target_task_phase_ids(
    *,
    existing_tasks: Mapping[str, Any],
    target_task_ids: Sequence[str],
    target_failures: Sequence[Mapping[str, Any]],
) -> set[str]:
    task_ids = {str(item).strip() for item in target_task_ids if str(item).strip()}
    for failure in target_failures:
        task_id = str(failure.get("task_id") or "").strip()
        if task_id:
            task_ids.add(task_id)
    phase_ids: set[str] = set()
    for task_id in task_ids:
        task = existing_tasks.get(task_id)
        if task is None:
            continue
        phase_id = _phase_id_from_phase_heading(str(getattr(task, "phase", "") or ""))
        if phase_id:
            phase_ids.add(phase_id)
    return phase_ids


def _objective_gap_phase_mismatch_errors(
    *,
    patch_tasks: Sequence[Any],
    allowed_phase_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    for task in patch_tasks:
        task_id = getattr(task, "task_id", None)
        phase_title = str(getattr(task, "phase", "") or "")
        phase_id = _phase_id_from_phase_heading(phase_title)
        if phase_id not in allowed_phase_ids:
            errors.append(
                "phase_objective_gap follow-up task "
                f"{task_id or '<unknown>'} must be inserted into target phase "
                f"{', '.join(sorted(allowed_phase_ids))}; PLAN_PATCH.md placed it in {phase_id or phase_title or '<unknown>'}."
            )
    return errors


def _target_task_phase_mismatch_errors(
    *,
    patch_tasks: Sequence[Any],
    allowed_phase_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    for task in patch_tasks:
        task_id = getattr(task, "task_id", None)
        phase_title = str(getattr(task, "phase", "") or "")
        phase_id = _phase_id_from_phase_heading(phase_title)
        if phase_id not in allowed_phase_ids:
            errors.append(
                "target-task self-expansion follow-up task "
                f"{task_id or '<unknown>'} must be inserted into target phase "
                f"{', '.join(sorted(allowed_phase_ids))}; PLAN_PATCH.md placed it in {phase_id or phase_title or '<unknown>'}."
            )
    return errors


def _phase_id_from_phase_heading(phase_title: str) -> str:
    match = re.match(r"^Phase\s+([^:]+)", phase_title.strip())
    if match:
        return match.group(1).strip()
    return phase_title.strip()


def _proposal_requires_approval(proposal: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    expansion_type = str(proposal.get("expansion_type") or "")
    risk = _proposal_risk(proposal)
    if expansion_type == "objective_gap" and risk in {"low", "medium"} and policy.get("auto_apply_objective_gap_low_medium_risk") is True:
        return False
    if proposal.get("approval_required") is True:
        return True
    strategy = str(proposal.get("resolution_strategy") or "")
    if strategy == "supersede_task_with_approval":
        return True
    if risk == "high":
        return bool(policy.get("require_approval_for_high_risk", True))
    if risk == "medium":
        return bool(policy.get("require_approval_for_medium_risk", True))
    if risk == "low":
        return not bool(policy.get("auto_apply_low_risk", True))
    return True


def _proposal_risk(proposal: Mapping[str, Any]) -> str:
    risks: list[str] = []
    if isinstance(proposal.get("risk"), str):
        risks.append(str(proposal.get("risk")).lower())
    new_tasks = proposal.get("new_tasks")
    if isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)):
        for task in new_tasks:
            if isinstance(task, Mapping) and isinstance(task.get("risk"), str):
                risks.append(str(task.get("risk")).lower())
    if "high" in risks:
        return "high"
    if "medium" in risks:
        return "medium"
    return "low"


def compute_loop_signature(proposal: Mapping[str, Any]) -> str:
    task_titles = []
    new_tasks = proposal.get("new_tasks", [])
    if isinstance(new_tasks, Sequence) and not isinstance(new_tasks, (str, bytes)):
        for task in new_tasks:
            if isinstance(task, Mapping):
                task_titles.append(str(task.get("title") or task.get("task_id") or "").strip().lower())
    payload = {
        "trigger": proposal.get("trigger"),
        "target_task_ids": sorted(str(item) for item in proposal.get("target_task_ids", []) if str(item)),
        "target_failure_ids": sorted(str(item) for item in proposal.get("target_failure_ids", []) if str(item)),
        "target_objective_ids": sorted(str(item) for item in proposal.get("target_objective_ids", []) if str(item)),
        "objective_gap_signature": proposal.get("objective_gap_signature"),
        "expansion_type": proposal.get("expansion_type"),
        "resolution_strategy": proposal.get("resolution_strategy"),
        "tasks": sorted(task_titles),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _record_proposal(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    validation: Mapping[str, Any],
    status: str,
    run_id: str | None = None,
    runner_id: str | None = None,
    apply_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    proposal = validation.get("proposal") if isinstance(validation.get("proposal"), Mapping) else {}
    proposal_id = str(validation.get("proposal_id") or proposal.get("proposal_id") or f"exp_{uuid.uuid4().hex[:8]}")
    record = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "runner_id": runner_id,
        "status": status,
        "created_at": utc_timestamp(),
        "validated_at": utc_timestamp(),
        "trigger": proposal.get("trigger"),
        "expansion_type": proposal.get("expansion_type"),
        "resolution_strategy": proposal.get("resolution_strategy"),
        "target_task_ids": [str(item) for item in proposal.get("target_task_ids", []) if str(item)],
        "target_failure_ids": [str(item) for item in proposal.get("target_failure_ids", []) if str(item)],
        "target_objective_ids": [str(item) for item in proposal.get("target_objective_ids", []) if str(item)],
        "target_phase_id": proposal.get("target_phase_id") or proposal.get("phase_id") or validation.get("target_phase_id"),
        "new_phase_id": proposal.get("new_phase_id") or proposal.get("followup_phase_id"),
        "plan_patch_operation": proposal.get("plan_patch_operation") or validation.get("plan_patch_operation"),
        "objective_verification_report": proposal.get("objective_verification_report"),
        "objective_gap_signature": proposal.get("objective_gap_signature"),
        "added_task_ids": list(apply_result.get("added_tasks", [])) if isinstance(apply_result, Mapping) else list(validation.get("added_task_ids", [])),
        "added_phase_ids": list(apply_result.get("added_phase_ids", [])) if isinstance(apply_result, Mapping) else list(validation.get("added_phase_ids", [])),
        "plan_patch_path": validation.get("plan_patch_path"),
        "loop_signature": validation.get("loop_signature"),
        "approval_required": validation.get("approval_required"),
        "risk": validation.get("risk"),
        "errors": list(validation.get("errors", [])),
        "warnings": list(validation.get("warnings", [])),
    }
    if isinstance(apply_result, Mapping):
        record["apply_status"] = apply_result.get("status")
        record["apply_event_id"] = apply_result.get("event_id")
        objective_update = apply_result.get("objective_followup_update")
        if isinstance(objective_update, Mapping):
            record["objective_followup_update"] = dict(objective_update)
    if record["resolution_strategy"] == "reopen_failure_after_new_evidence" and status == "applied":
        record["failure_resolution_status"] = "pending_evidence"
    if record["expansion_type"] == "objective_gap" and status == "applied":
        record["objective_resolution_status"] = "followup_pending"
    proposals = registry.setdefault("proposals", [])
    if not isinstance(proposals, list):
        proposals = []
        registry["proposals"] = proposals
    replaced = False
    for index, existing in enumerate(proposals):
        if isinstance(existing, Mapping) and str(existing.get("proposal_id") or "") == proposal_id:
            proposals[index] = {**dict(existing), **record}
            replaced = True
            break
    if not replaced:
        proposals.append(record)
        registry["cycle"] = int(registry.get("cycle") or 0) + (1 if status == "applied" else 0)
    registry["updated_at"] = utc_timestamp()
    registry.setdefault("events", []).append(
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"exp_evt_{uuid.uuid4().hex[:12]}",
            "event_type": f"self_expansion_{status}",
            "workflow_id": workflow_id,
            "proposal_id": proposal_id,
            "run_id": run_id,
            "created_at": registry["updated_at"],
            "data": {
                "status": status,
                "added_task_ids": record["added_task_ids"],
                "added_phase_ids": record["added_phase_ids"],
                "plan_patch_operation": record.get("plan_patch_operation"),
            },
        }
    )
    write_expansion_registry(paths, registry)
    return record


def _apply_no_mutation_result(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    proposal: Mapping[str, Any],
    proposal_path: Path,
    status: str,
    message: str,
    run_id: str | None,
    runner_id: str | None,
    owner: str,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validate_expansion_proposal(project, proposal_path)
    record = _record_proposal(
        paths,
        workflow_id=workflow_id,
        validation=validation if validation.get("proposal") else {"proposal": proposal, "proposal_id": proposal.get("proposal_id")},
        status=status,
        run_id=run_id,
        runner_id=runner_id,
    )
    _append_scheduler_event(
        paths,
        workflow_id=workflow_id,
        run_id=run_id or str(proposal.get("proposal_id") or ""),
        event_type="self_expansion_approval_required" if status == "approval_required" else "self_expansion_requires_attention",
        data={"proposal_id": proposal.get("proposal_id"), "status": status, "approval": dict(approval or {})},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "approval_required",
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "proposal_id": proposal.get("proposal_id"),
        "run_id": run_id,
        "runner_id": runner_id,
        "resolution_strategy": proposal.get("resolution_strategy"),
        "message": message,
        "approval": dict(approval) if isinstance(approval, Mapping) else None,
        "registry_record": record,
        "errors": [] if status == "approval_required" else [message],
        "warnings": [],
    }


def _record_expansion_approval_request(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    proposal: Mapping[str, Any],
    proposal_path: Path,
    plan_patch_path: Path,
    binding: Mapping[str, str],
    run_id: str | None,
) -> dict[str, Any]:
    policy = load_approval_policy(paths)
    approval_id = new_approval_id()
    proposal_id = str(proposal.get("proposal_id") or "")
    new_task_ids = [
        str(task.get("task_id") or "")
        for task in proposal.get("new_tasks", [])
        if isinstance(task, Mapping) and str(task.get("task_id") or "")
    ]
    approval_scope = " ".join(["self_expansion", proposal_id, *new_task_ids]).strip()
    request = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "request_id": approval_id,
        "workflow_id": workflow_id,
        "created_at": utc_timestamp(),
        "expires_at": default_expires_at(),
        "status": "pending",
        "type": "self_expansion",
        "scope": approval_scope,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "proposal_path": _path_for_record(paths.project_root, proposal_path),
        "proposal_sha256": binding["proposal_sha256"],
        "plan_patch_path": _path_for_record(paths.project_root, plan_patch_path),
        "plan_patch_sha256": binding["plan_patch_sha256"],
        "resolution_strategy": proposal.get("resolution_strategy"),
        "target_task_ids": list(proposal.get("target_task_ids", [])),
        "target_failure_ids": list(proposal.get("target_failure_ids", [])),
        "target_objective_ids": list(proposal.get("target_objective_ids", [])),
        "new_task_ids": new_task_ids,
        "objective_verification_report": proposal.get("objective_verification_report"),
        "message": "Self-expansion proposal requires human approval.",
        "approval_policy_enabled": policy.get("enabled") is True,
    }
    _supersede_stale_expansion_approval_requests(
        paths,
        proposal_id=proposal_id,
        binding=binding,
        superseded_by_approval_id=approval_id,
    )
    _append_jsonl(paths.runtime_dir / "human_approval_requests.jsonl", request)
    return request


def _expansion_approval_record(
    paths: WorkflowPaths,
    *,
    proposal_id: str,
    binding: Mapping[str, str],
) -> dict[str, Any] | None:
    responses = read_approval_responses(paths)
    records = [
        approval_record_status(request, responses=responses, now=utc_timestamp())
        for request in read_approval_requests(paths)
        if str(request.get("type") or "") == "self_expansion"
        and str(request.get("proposal_id") or "") == proposal_id
    ]
    if not records:
        return None
    latest = sorted(records, key=lambda item: str(item.get("created_at") or ""))[-1]
    if not _expansion_approval_binding_matches(latest, binding):
        return None
    return latest


def _expansion_approval_binding(*, proposal_path: Path, plan_patch_path: Path) -> dict[str, str]:
    return {
        "proposal_sha256": _sha256_path(proposal_path),
        "plan_patch_sha256": _sha256_path(plan_patch_path),
    }


def _expansion_approval_binding_matches(
    approval: Mapping[str, Any],
    binding: Mapping[str, str],
) -> bool:
    return all(
        str(binding.get(field) or "")
        and str(approval.get(field) or "") == str(binding.get(field) or "")
        for field in ("proposal_sha256", "plan_patch_sha256")
    )


def _supersede_stale_expansion_approval_requests(
    paths: WorkflowPaths,
    *,
    proposal_id: str,
    binding: Mapping[str, str],
    superseded_by_approval_id: str,
) -> None:
    responses = read_approval_responses(paths)
    response_path = paths.runtime_dir / "human_approval_responses.jsonl"
    for request in read_approval_requests(paths):
        if str(request.get("type") or "") != "self_expansion":
            continue
        if str(request.get("proposal_id") or "") != proposal_id:
            continue
        status = approval_record_status(request, responses=responses, now=utc_timestamp())
        if status.get("status") != "pending" or _expansion_approval_binding_matches(request, binding):
            continue
        response = {
            "schema_version": SCHEMA_VERSION,
            "approval_id": str(request.get("approval_id") or request.get("request_id") or ""),
            "responded_at": utc_timestamp(),
            "decision": "superseded",
            "approved_by": "loopplane",
            "scope": str(request.get("scope") or ""),
            "notes": "Proposal or PLAN_PATCH.md content changed; a new content-bound approval is required.",
            "source": "self_expansion_content_binding",
            "workflow_id": str(request.get("workflow_id") or ""),
            "type": "self_expansion",
            "superseded_by_approval_id": superseded_by_approval_id,
        }
        _append_jsonl(response_path, response)
        responses.append(response)


def _sha256_path(path: Path) -> str:
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _expansion_approval_response_id(approval: Mapping[str, Any] | None) -> str:
    if not isinstance(approval, Mapping):
        return ""
    response = approval.get("response")
    if isinstance(response, Mapping):
        return str(response.get("response_id") or response.get("approval_id") or "")
    return ""


def _validation_result(
    *,
    project: Path,
    workflow_id: str,
    proposal_path: Path,
    status: str,
    errors: Sequence[str] | None = None,
    warnings: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "valid",
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "proposal_path": _path_for_record(project, proposal_path),
        "errors": list(errors or []),
        "warnings": list(warnings or []),
    }
    payload.update(dict(extra or {}))
    return payload


def _proposal_plan_patch_path(project: Path, proposal_path: Path, proposal: Mapping[str, Any]) -> Path:
    value = str(proposal.get("plan_patch_path") or EXPANSION_PLAN_PATCH_FILENAME)
    path = Path(value)
    if not path.is_absolute():
        candidate = (proposal_path.parent / path).resolve()
        if candidate.exists():
            return candidate
        path = (project / path).resolve()
    return path.resolve()


def _plan_patch_operation_for_proposal(
    proposal: Mapping[str, Any],
    *,
    target_objective_scopes: set[str] | None = None,
    target_task_phase_ids: set[str] | None = None,
) -> str:
    if str(proposal.get("expansion_type") or "") != "objective_gap":
        if len(target_task_phase_ids or set()) == 1:
            return PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE
        return PLAN_PATCH_OPERATION_APPEND
    trigger = str(proposal.get("trigger") or "").strip().lower()
    scope = str(proposal.get("scope") or "").strip().lower()
    scopes = {str(item).strip().lower() for item in target_objective_scopes or set() if str(item).strip()}
    if trigger == "phase_objective_gap" or scope == "phase" or scopes == {"phase"}:
        return PLAN_PATCH_OPERATION_INSERT_TASK_INTO_PHASE
    if trigger in {"final_objective_gap", "workflow_objective_gap", "plan_objective_gap"} or scope in {"workflow", "plan", "final"} or scopes == {"workflow"}:
        return PLAN_PATCH_OPERATION_INSERT_PHASE_BEFORE_FINAL_OBJECTIVES
    return PLAN_PATCH_OPERATION_APPEND


def _dependency_errors(
    *,
    new_tasks: Sequence[Any],
    existing_task_ids: set[str],
    patch_task_ids: set[str],
    unresolved_target_task_ids: set[str],
    existing_tasks: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    all_task_ids = set(existing_task_ids).union(patch_task_ids)
    for task in new_tasks:
        if not isinstance(task, Mapping):
            errors.append("new_tasks items must be objects.")
            continue
        task_id = str(task.get("task_id") or "")
        deps = _dependency_list(task.get("depends_on") if "depends_on" in task else task.get("dependencies"))
        for dep in deps:
            if dep not in all_task_ids:
                errors.append(f"{task_id}: dependency {dep!r} is not an existing or newly added task.")
            if dep in unresolved_target_task_ids:
                existing = existing_tasks.get(dep)
                existing_status = str(getattr(existing, "status", "") or "")
                if existing_status not in {"x", "-"}:
                    errors.append(f"{task_id}: diagnostic/decomposition task must not depend on unresolved failed task {dep}.")
    return errors


def _objective_followup_schedulability_errors(
    *,
    patch_tasks: Sequence[Any],
    existing_tasks: Mapping[str, Any],
    patch_task_ids: set[str],
) -> list[str]:
    ordered_patch_tasks = [task for task in patch_tasks if str(getattr(task, "task_id", "") or "") in patch_task_ids]
    if not ordered_patch_tasks:
        return ["objective_gap proposals must add at least one structural follow-up task."]

    completed = {
        task_id
        for task_id, task in existing_tasks.items()
        if str(getattr(task, "status", "") or "") in {"x", "-"}
    }
    remaining = {str(getattr(task, "task_id", "") or "") for task in ordered_patch_tasks}
    first_round_ready: list[str] = []
    progress = True
    while remaining and progress:
        progress = False
        for task in ordered_patch_tasks:
            task_id = str(getattr(task, "task_id", "") or "")
            if task_id not in remaining:
                continue
            deps = _patch_task_dependencies(task)
            if all(dep in completed for dep in deps):
                if len(completed.intersection(patch_task_ids)) == 0:
                    first_round_ready.append(task_id)
                completed.add(task_id)
                remaining.remove(task_id)
                progress = True
    if not remaining and first_round_ready:
        return []

    errors: list[str] = []
    if not first_round_ready:
        errors.append("objective_gap follow-up tasks are not immediately executable as the next scheduled work.")
    unresolved_details: list[str] = []
    for task in ordered_patch_tasks:
        task_id = str(getattr(task, "task_id", "") or "")
        if task_id not in remaining:
            continue
        deps = _patch_task_dependencies(task)
        blocked_deps = [dep for dep in deps if dep not in completed]
        if blocked_deps:
            unresolved_details.append(f"{task_id}: " + ", ".join(_dependency_status_label(dep, existing_tasks, patch_task_ids) for dep in blocked_deps))
    if unresolved_details:
        errors.append("objective_gap follow-up tasks have unresolved dependencies: " + "; ".join(unresolved_details))
    return errors


def _patch_task_dependencies(task: Any) -> list[str]:
    fields = getattr(task, "fields", {})
    values = fields.get("depends_on") if isinstance(fields, Mapping) else None
    deps: list[str] = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            deps.extend(_dependency_list(value))
    return deps


def _dependency_status_label(dep: str, existing_tasks: Mapping[str, Any], patch_task_ids: set[str]) -> str:
    if dep in patch_task_ids:
        return f"{dep} (new task is not schedulable yet)"
    existing = existing_tasks.get(dep)
    if existing is not None:
        status = str(getattr(existing, "status", "") or "")
        return f"{dep} (existing task status {status!r})"
    return f"{dep} (missing)"


def _dependency_list(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    stripped = value.strip()
    if not stripped:
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return [part.strip().strip('"').strip("'") for part in stripped.split(",") if part.strip()]


def _applied_loop_signature_count(registry: Mapping[str, Any], loop_signature: str) -> int:
    """Count semantic plan transitions, not failed or approval-pending attempts.

    Proposal records are also the audit trail for validation failures and
    approval hand-offs. Counting those records against the semantic repeat
    budget can make an otherwise valid proposal impossible to approve: merely
    revalidating the same, unapplied proposal changes no workflow state. Only
    an ``applied`` proposal has crossed the state-transition boundary that this
    guard is intended to bound.
    """
    if not loop_signature:
        return 0
    proposals = registry.get("proposals", [])
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        return 0
    return sum(
        1
        for proposal in proposals
        if isinstance(proposal, Mapping)
        and str(proposal.get("status") or "") == "applied"
        and str(proposal.get("loop_signature") or "") == loop_signature
    )


def _first_exhausted_failure(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    task_records = {
        str(task.get("task_id") or ""): task
        for task in snapshot.get("tasks", [])
        if isinstance(task, Mapping) and str(task.get("task_id") or "")
    }
    task_statuses = {
        task_id: str(task.get("status") or "")
        for task_id, task in task_records.items()
    }
    completed_task_ids = {
        task_id for task_id, status in task_statuses.items() if status in {"x", "-"}
    }
    deferred_failure_ids = _failures_waiting_on_expansion_evidence(snapshot, task_statuses)
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("status") or "") != "exhausted":
            continue
        if str(failure.get("failure_class") or "") in NON_SCIENTIFIC_EXPANSION_FAILURE_CLASSES:
            continue
        if str(failure.get("failure_id") or "") in deferred_failure_ids:
            continue
        task_id = str(failure.get("task_id") or "")
        task = task_records.get(task_id)
        if task is None:
            continue
        dependencies = [str(dependency) for dependency in task.get("depends_on", []) if str(dependency)]
        if any(dependency not in completed_task_ids for dependency in dependencies):
            continue
        return dict(failure)
    return None


def _exhausted_non_scientific_failures(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return operational blockers without letting them suppress unrelated expansion.

    Infrastructure failures are not scientific evidence, but a stale operational
    failure must not turn into a global no-work latch. The expansion planner gets
    these records explicitly so it can propose operational recovery or independent
    work without interpreting the failure as a scientific result.
    """
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    return [
        {
            key: failure.get(key)
            for key in (
                "failure_id",
                "task_id",
                "failure_class",
                "failure_signature",
                "exhausted_reason",
            )
        }
        for failure in failures
        if isinstance(failure, Mapping)
        and str(failure.get("status") or "") == "exhausted"
        and str(failure.get("failure_class") or "") in NON_SCIENTIFIC_EXPANSION_FAILURE_CLASSES
    ]


def _failures_waiting_on_expansion_evidence(snapshot: Mapping[str, Any], tasks: Mapping[str, str]) -> set[str]:
    workflow_config = snapshot.get("workflow_config")
    paths = snapshot.get("paths")
    if not isinstance(workflow_config, Mapping) or not isinstance(paths, WorkflowPaths):
        return set()
    workflow_id = str(snapshot.get("workflow_id") or workflow_config.get("workflow_id") or "unknown_workflow")
    registry = read_expansion_registry(paths, workflow_id=workflow_id)
    deferred: set[str] = set()
    for proposal in registry.get("proposals", []):
        if not isinstance(proposal, Mapping):
            continue
        if proposal.get("status") != "applied":
            continue
        if proposal.get("resolution_strategy") != "reopen_failure_after_new_evidence":
            continue
        if proposal.get("failure_resolution_status") != "pending_evidence":
            continue
        added_task_ids = [str(item) for item in proposal.get("added_task_ids", []) if str(item)]
        if not added_task_ids:
            continue
        if all(tasks.get(task_id) in {"x", "-"} for task_id in added_task_ids):
            continue
        deferred.update(str(item) for item in proposal.get("target_failure_ids", []) if str(item))
    return deferred


def _has_nonterminal_tasks(snapshot: Mapping[str, Any]) -> bool:
    return bool(_nonterminal_task_ids(snapshot))


def _nonterminal_task_ids(snapshot: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for task in snapshot.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        if str(task.get("status") or "") in {" ", "~", "!"}:
            task_id = str(task.get("task_id") or "")
            if task_id:
                ids.append(task_id)
    return ids


def _latest_final_verifier_failure(paths: WorkflowPaths) -> dict[str, Any] | None:
    path = paths.runtime_dir / "final_verification_report.json"
    report = _read_json_object_default(path, None)
    if not isinstance(report, Mapping):
        return None
    if report.get("pass") is True or report.get("status") == "pass" or report.get("ok") is True:
        return None
    return dict(report)


def _final_verifier_blocker_allows_expansion(blocker: Mapping[str, Any]) -> bool:
    if blocker.get("expandable") is not False:
        return True
    details = blocker.get("details") if isinstance(blocker.get("details"), Mapping) else {}
    return str(details.get("recommended_action") or "").strip().lower() == "self_expand"


def _objective_gap_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    reports_root = snapshot.get("objective_reports")
    reports = reports_root.get("reports", []) if isinstance(reports_root, Mapping) else []
    if not isinstance(reports, Sequence) or isinstance(reports, (str, bytes)):
        return None
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        if str(report.get("status") or "") != "needs_expansion":
            continue
        target_ids = [str(item) for item in report.get("expandable_objective_ids", []) if str(item)]
        if not target_ids:
            target_ids = [str(item) for item in report.get("unresolved_objective_ids", []) if str(item)]
        if not target_ids:
            continue
        scope = str(report.get("scope") or "")
        payload = {
            "trigger": "phase_objective_gap" if scope == "phase" else "final_objective_gap",
            "expansion_type": "objective_gap",
            "scope": report.get("scope"),
            "phase_id": report.get("phase_id"),
            "phase_title": report.get("phase_title"),
            "target_objective_ids": target_ids,
            "objective_verification_report": report.get("path"),
            "objective_report": dict(report),
        }
        payload["objective_gap_signature"] = _objective_gap_signature(payload)
        return payload
    return None


def _objective_gap_signature(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {
            "scope": payload.get("scope"),
            "phase_id": payload.get("phase_id"),
            "target_objective_ids": sorted(str(item) for item in payload.get("target_objective_ids", []) if str(item)),
            "objective_verification_report": payload.get("objective_verification_report"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _failure_by_id(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    registry = snapshot.get("failure_registry") if isinstance(snapshot.get("failure_registry"), Mapping) else snapshot
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    result: dict[str, dict[str, Any]] = {}
    if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)):
        for failure in failures:
            if isinstance(failure, Mapping):
                failure_id = str(failure.get("failure_id") or "")
                if failure_id:
                    result[failure_id] = dict(failure)
    return result


def _compact_registry(registry: Mapping[str, Any]) -> dict[str, Any]:
    proposals = registry.get("proposals", [])
    latest = proposals[-1] if isinstance(proposals, list) and proposals else None
    return {
        "cycle": registry.get("cycle", 0),
        "proposal_count": len(proposals) if isinstance(proposals, list) else 0,
        "latest_proposal": dict(latest) if isinstance(latest, Mapping) else None,
    }


def _current_objective_statuses(paths: WorkflowPaths) -> dict[str, str]:
    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    objectives, _errors = parse_plan_objectives(plan_text)
    statuses: dict[str, str] = {}
    for objective in objectives:
        objective_id = str(getattr(objective, "objective_id", "") or "").strip()
        status = str(getattr(objective, "status", "") or "").strip()
        if objective_id:
            statuses[objective_id] = status
    return statuses


def _proposal_with_current_objective_resolution(
    proposal: Mapping[str, Any],
    *,
    objective_statuses: Mapping[str, str],
) -> dict[str, Any]:
    record = dict(proposal)
    if record.get("status") != "applied" or record.get("expansion_type") != "objective_gap":
        return record
    objective_ids = [str(item).strip() for item in record.get("target_objective_ids") or [] if str(item).strip()]
    if objective_ids and all(objective_statuses.get(objective_id) == "x" for objective_id in objective_ids):
        record["objective_resolution_status"] = "resolved"
    elif not record.get("objective_resolution_status"):
        record["objective_resolution_status"] = "followup_pending"
    return record


def _load_project(project_root: Path | str) -> tuple[Path, WorkflowPaths, str, Mapping[str, Any]]:
    project = Path(project_root).expanduser().resolve()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        raise RuntimeError(f"Unable to load workflow configuration: {error}") from error
    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    return project, paths, workflow_id, workflow_config


def _resolve_project_path(project: Path, value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None, str | None]:
    try:
        content = path.read_bytes()
        data = json.loads(content.decode("utf-8"))
    except OSError as error:
        return None, f"{path}: {error}", None
    except UnicodeDecodeError as error:
        return None, f"{path}: invalid UTF-8: {error}", None
    except json.JSONDecodeError as error:
        return None, f"{path}: invalid JSON: {error.msg}", None
    if not isinstance(data, Mapping):
        return None, f"{path}: JSON value must be an object", None
    return dict(data), None, "sha256:" + sha256(content).hexdigest()


def _read_json_object_default(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _path_for_record(project_root: Path | None, path: Path) -> str:
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _project_relative(project: Path, path: Path) -> str:
    return _path_for_record(project, path)


def _path_attr(obj: Any, name: str) -> Path:
    value = getattr(obj, name, None)
    if isinstance(value, Path):
        return value
    if value is None:
        return Path("")
    return Path(str(value))


def _string_attr(obj: Any, name: str) -> str:
    value = getattr(obj, name, "")
    return str(value or "")


def _append_scheduler_event(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    run_id: str,
    event_type: str,
    data: Mapping[str, Any],
) -> None:
    from runtime.scheduler import append_event

    append_event(paths, workflow_id=workflow_id, run_id=run_id, event_type=event_type, data=dict(data))
