from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import ADAPTER_INPUT_FILENAME, AdapterContractError, AdapterInput, utc_timestamp
from runtime.adapters.registry import AdapterLookupError, get_adapter
from runtime.agent_runners import AgentRunnerConfigError, load_agent_runners
from runtime.approval import (
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
    approval_record_status,
)
from runtime.active_projections import sync_active_workflow_projections
from runtime.exit_codes import (
    EXIT_FINAL_VERIFICATION_FAILED,
    EXIT_INVALID_CONFIG,
    EXIT_PLAN_MALFORMED,
    EXIT_SUCCESS,
)
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import (
    objective_closure_fingerprint,
    objective_structure_fingerprint,
    is_task_block_terminator,
    parse_plan_objectives,
)
from runtime.read_models import rebuild_read_models
from runtime.schema_validation import validate_json_value_against_schema
from runtime.source_guard import read_process_template
from runtime.version_control import create_git_checkpoint
from runtime.workflow_lifecycle import mark_workflow_completed


SCHEMA_VERSION = "1.5"
FINAL_REPORT_FILENAME = "final_verification_report.json"
EVIDENCE_MANIFEST_FILENAME = "evidence_manifest.json"
COMPLETION_MARKER_FILENAME = "plan_loop_complete.json"
BACKGROUND_JOBS_FILENAME = "background_jobs.json"
FAILURE_REGISTRY_FILENAME = "failure_registry.json"
FINAL_REVIEWER_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "final_reviewer_prompt.template.md"
FINAL_REVIEWER_REPORT_FILENAME = "final_reviewer_report.json"
PASSING_VALIDATION_STATUSES = frozenset({"pass", "pass_with_warnings"})
RESOLVED_FAILURE_STATUSES = frozenset({"recovered", "waived"})
SAFE_BACKGROUND_JOB_STATUSES = frozenset({"completed", "cancelled"})
INACTIVE_LEASE_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "aborted", "released"})
FINAL_REVIEWER_HARD_BLOCKER_CHECKS = frozenset(
    {
        "plan_parseable",
        "no_active_pending_partial_or_blocked_tasks",
        "skipped_tasks_authorized",
        "required_final_deliverables_exist",
        "objectives_closed_by_agentic_verification",
        "no_unrecovered_failures",
        "no_active_run_leases",
        "no_active_background_jobs",
        "no_pending_approval_requests",
        "evidence_manifest_schema_valid",
        "required_git_checkpoints",
    }
)
FINAL_REVIEWER_OVERRIDABLE_CHECKS = frozenset(
    {
        "all_completed_tasks_have_latest_and_passing_validation",
    }
)
TASK_LINE_RE = re.compile(r"^- \[(?P<status>[ x~!\-])\]\s+(?P<task_id>[A-Za-z0-9_.-]+):\s+(?P<title>.+?)\s*$")
FIELD_LINE_RE = re.compile(r"^  - (?P<field>[A-Za-z0-9_ -]+):(?P<value>.*)$")


@dataclass(frozen=True)
class FinalVerifierTask:
    task_id: str
    status: str
    title: str
    fields: Mapping[str, tuple[str, ...]]
    line_index: int
    block: str

    @property
    def status_label(self) -> str:
        return f"[{self.status}]"

    @property
    def evidence_root(self) -> str:
        return _first_field(self.fields, "evidence") or ""

    @property
    def latest_path_value(self) -> str:
        return _first_field(self.fields, "latest", "latest_pointer_path") or ""

    @property
    def skip_reason(self) -> str:
        return _first_field(self.fields, "skip_reason") or ""

    @property
    def skip_authorization(self) -> str:
        return _first_field(self.fields, "skip_authorization", "approval_id") or ""

    @property
    def deliverables(self) -> str:
        return _first_field(self.fields, "deliverables", "final_deliverables") or ""


def final_verification_input_fingerprint(
    paths: WorkflowPaths,
    *,
    tasks: Sequence[FinalVerifierTask] | None = None,
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic digest of the inputs that can change a final verdict.

    The plan digest captures task/objective structure and terminal markers.  The
    per-task digest additionally binds each latest pointer, its authoritative
    validation, and the selected evidence-run tree.  Timestamps and filesystem
    metadata are deliberately excluded from the fingerprint.
    """

    resolved_plan_sha256 = plan_sha256 or _sha256_file(paths.plan_file)
    if tasks is None:
        try:
            plan_text = paths.plan_file.read_text(encoding="utf-8")
            parsed_tasks, _errors = _parse_plan_tasks(plan_text)
        except OSError:
            parsed_tasks = []
    else:
        parsed_tasks = list(tasks)

    state = _read_json_object(paths.runtime_dir / "state.json", default={})
    recorded_active_plan_sha256 = str(state.get("active_plan_sha256") or "") or None
    task_records = [
        _final_verification_task_input_record(paths, task)
        for task in parsed_tasks
    ]
    task_payload = json.dumps(task_records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    task_validation_evidence_sha256 = "sha256:" + sha256(task_payload).hexdigest()
    payload = {
        "schema_version": "1.0",
        "active_plan_sha256": resolved_plan_sha256,
        "recorded_active_plan_sha256": recorded_active_plan_sha256,
        "task_validation_evidence_sha256": task_validation_evidence_sha256,
        "tasks": task_records,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": "1.0",
        "algorithm": "sha256",
        "fingerprint": "sha256:" + sha256(encoded).hexdigest(),
        "active_plan_sha256": resolved_plan_sha256,
        "recorded_active_plan_sha256": recorded_active_plan_sha256,
        "task_validation_evidence_sha256": task_validation_evidence_sha256,
        "task_count": len(task_records),
        "terminal_task_count": sum(1 for record in task_records if record.get("status") in {"x", "-"}),
    }


def final_verification_report_freshness(
    paths: WorkflowPaths,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a final report with current authoritative final-verifier inputs."""

    current = final_verification_input_fingerprint(paths)
    raw_report_fingerprint = report.get("input_fingerprint")
    if isinstance(raw_report_fingerprint, Mapping):
        report_fingerprint = str(
            raw_report_fingerprint.get("fingerprint")
            or raw_report_fingerprint.get("sha256")
            or ""
        )
    else:
        report_fingerprint = str(raw_report_fingerprint or "")
    if report_fingerprint:
        current_fingerprint = str(current.get("fingerprint") or "")
        stale_reasons = [] if report_fingerprint == current_fingerprint else ["input_fingerprint_mismatch"]
        components = report.get("input_fingerprint_components")
        if isinstance(components, Mapping):
            report_plan_sha256 = str(components.get("active_plan_sha256") or "")
            if report_plan_sha256 and report_plan_sha256 != str(current.get("active_plan_sha256") or ""):
                stale_reasons.append("active_plan_sha256_mismatch")
            report_evidence_sha256 = str(components.get("task_validation_evidence_sha256") or "")
            if report_evidence_sha256 and report_evidence_sha256 != str(current.get("task_validation_evidence_sha256") or ""):
                stale_reasons.append("task_validation_evidence_sha256_mismatch")
        return {
            "fresh": not stale_reasons,
            "mode": "input_fingerprint",
            "stale_reasons": _dedupe_strings(stale_reasons),
            "report_input_fingerprint": report_fingerprint,
            "current_input_fingerprint": current_fingerprint,
            "current_components": current,
        }

    return _legacy_final_verification_report_freshness(paths, report, current=current)


def run_final_verifier(
    project_root: Path | str,
    *,
    owner: str = "final_verifier",
    write: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    requested_write = bool(write)
    checked_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
        workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _configuration_failure(project=project, checked_at=checked_at, owner=owner, error=error)

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    tasks: list[FinalVerifierTask] = []
    final_reviewer_result: dict[str, Any] | None = None
    runner_availability: dict[str, Any] | None = None
    objectives = []
    objective_parse_errors: list[str] = []
    plan_sha = _sha256_file(paths.plan_file)
    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
        tasks, parse_errors = _parse_plan_tasks(plan_text)
        objectives, objective_parse_errors = parse_plan_objectives(plan_text)
    except OSError as error:
        parse_errors = [f"Unable to read PLAN.md: {error}"]

    checks.append(
        _check(
            "plan_parseable",
            "fail" if parse_errors else "pass",
            "PLAN.md is parseable." if not parse_errors else "PLAN.md is not parseable.",
            details={"errors": parse_errors, "task_count": len(tasks)},
        )
    )

    completion_marker_before = _completion_marker_status(paths)
    archived_markers: list[str] = []
    if completion_marker_before.get("exists") and not completion_marker_before.get("fresh"):
        archived = _archive_completion_marker(paths, completion_marker_before.get("path")) if write else None
        if archived:
            archived_markers.append(_path_for_record(project, archived))
            warnings.append(f"Archived stale completion marker at {_path_for_record(project, archived)}.")
        checks.append(
            _check(
                "completion_marker_fresh_or_ignored",
                "pass",
                "Existing completion marker is stale and was ignored.",
                details={
                    "stale_reasons": completion_marker_before.get("stale_reasons", []),
                    "archived_paths": archived_markers,
                },
            )
        )
    elif completion_marker_before.get("exists"):
        checks.append(
            _check(
                "completion_marker_fresh_or_ignored",
                "pass",
                "Existing completion marker is fresh for the current runtime state.",
                details={"path": completion_marker_before.get("path")},
            )
        )
        if write and not force:
            write = False
            warnings.append("Existing completion marker is fresh; final verification ran read-only. Use --force to write a new verification.")
    else:
        checks.append(_check("completion_marker_fresh_or_ignored", "pass", "No completion marker exists yet."))

    task_state_check = _check_task_states(tasks)
    checks.append(task_state_check)
    latest_check, manifest_task_records = _check_latest_and_validations(project, paths, tasks)
    checks.append(latest_check)
    input_fingerprint = final_verification_input_fingerprint(
        paths,
        tasks=tasks,
        plan_sha256=plan_sha,
    )
    latest_details = latest_check.get("details") if isinstance(latest_check.get("details"), Mapping) else {}
    warnings.extend(str(warning) for warning in latest_details.get("warnings", []) if str(warning))
    checks.append(_check_skipped_tasks(tasks))
    checks.append(_check_final_deliverables(project, paths, tasks))
    checks.append(_check_objective_closure(paths, plan_sha256=plan_sha, objectives=objectives, parse_errors=objective_parse_errors))
    checks.append(_check_failures(paths))
    checks.append(_check_active_leases(paths))
    checks.append(_check_background_jobs(paths))
    checks.append(_check_pending_approvals(paths))

    evidence_manifest = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "generated_at": checked_at,
        "tasks": _manifest_tasks_by_id(manifest_task_records),
    }
    evidence_manifest_path = paths.runtime_dir / EVIDENCE_MANIFEST_FILENAME
    manifest_schema_errors = validate_json_value_against_schema(
        evidence_manifest,
        "evidence_manifest.schema.json",
        paths.value("runtime_dir") + "/" + EVIDENCE_MANIFEST_FILENAME,
    )
    checks.append(
        _check(
            "evidence_manifest_schema_valid",
            "fail" if manifest_schema_errors else "pass",
            "Evidence manifest matches the published schema."
            if not manifest_schema_errors
            else "Evidence manifest does not match the published schema.",
            details={"errors": manifest_schema_errors},
        )
    )
    if write:
        _atomic_write_json(evidence_manifest_path, evidence_manifest)

    git_checkpoint_results: list[dict[str, Any]] = []
    git_check = _check_git_checkpoint_requirement(project, paths, workflow_id)
    checks.append(git_check)

    hard_blockers = _final_reviewer_hard_blockers(checks)
    if hard_blockers:
        final_reviewer_result = _skipped_final_reviewer_result(
            project=project,
            workflow_id=workflow_id,
            owner=owner,
            reason="deterministic_hard_blockers",
            blockers=hard_blockers,
        )
    else:
        final_reviewer_result = _run_final_reviewer_agent(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            evidence_manifest_path=evidence_manifest_path,
            tasks=tasks,
            deterministic_checks=checks,
            deterministic_warnings=warnings,
            write=write,
        )
    if final_reviewer_result is not None:
        availability = final_reviewer_result.get("runner_availability")
        if isinstance(availability, Mapping):
            runner_availability = _json_safe(availability)
        checks.append(_final_reviewer_check(final_reviewer_result))
        if final_reviewer_result.get("warning"):
            warnings.append(str(final_reviewer_result["warning"]))
        checks, final_review_warnings = _apply_final_reviewer_semantic_overrides(checks, final_reviewer_result)
        warnings.extend(final_review_warnings)

    prelim_status = _status_from_checks(checks)
    if prelim_status == "pass" and write and _version_control_enabled(paths):
        before = create_git_checkpoint(project, reason="before_final_completion")
        git_checkpoint_results.append(before)
        if not before.get("ok"):
            git_check = _check(
                "required_git_checkpoints",
                "fail",
                "Unable to create before-final-completion Git checkpoint.",
                details={"errors": before.get("errors", []), "warnings": before.get("warnings", [])},
            )
        else:
            git_check = _check(
                "required_git_checkpoints",
                "pass",
                "Before-final-completion Git checkpoint was created.",
                details={"checkpoint_id": before.get("checkpoint", {}).get("checkpoint_id")},
            )
        for index in range(len(checks) - 1, -1, -1):
            if checks[index].get("check") == "required_git_checkpoints":
                checks[index] = git_check
                break

    status_before_read_models = _status_from_checks(checks)
    read_model_result: dict[str, Any] | None = None
    final_git_checkpoint_id: str | None = _latest_git_checkpoint_id(paths.runtime_dir / "git_checkpoints.jsonl")

    if status_before_read_models == "pass" and write:
        _update_runtime_state(
            paths,
            status="completed",
            scheduler_update={
                "last_action": "run_final_verification",
                "owner": owner,
                "running": False,
                "paused": False,
                "stop_requested": False,
                "active_run_id": None,
                "active_node_id": None,
                "active_task_id": None,
            },
        )
        _append_final_events(paths, workflow_id=workflow_id, owner=owner)
        if _version_control_enabled(paths):
            after = create_git_checkpoint(project, reason="after_final_completion")
            git_checkpoint_results.append(after)
            if not after.get("ok"):
                checks.append(
                    _check(
                        "final_git_checkpoint_created",
                        "fail",
                        "Unable to create after-final-completion Git checkpoint.",
                        details={"errors": after.get("errors", []), "warnings": after.get("warnings", [])},
                    )
                )
            else:
                final_git_checkpoint_id = after.get("checkpoint", {}).get("checkpoint_id")
                checks.append(
                    _check(
                        "final_git_checkpoint_created",
                        "pass",
                        "After-final-completion Git checkpoint was created.",
                        details={"checkpoint_id": final_git_checkpoint_id},
                    )
                )
        read_model_result = rebuild_read_models(project, write=True)
        checks.append(
            _check(
                "read_models_fresh_or_rebuildable",
                "pass" if read_model_result.get("ok") else "fail",
                "Read models were rebuilt from authoritative runtime files."
                if read_model_result.get("ok")
                else "Read models could not be rebuilt from authoritative runtime files.",
                details={
                    "status": read_model_result.get("status"),
                    "errors": read_model_result.get("errors", []),
                    "warnings": read_model_result.get("warnings", []),
                },
            )
        )
        projection_sync = sync_active_workflow_projections(
            project,
            workflow_config,
            paths,
            reason="final_verification",
        )
        if projection_sync.get("warnings"):
            warnings.extend(str(warning) for warning in projection_sync.get("warnings", []))
        checks.append(
            _check(
                "active_workflow_projections_current",
                "pass" if projection_sync.get("ok") else "fail",
                "Active workflow projections were synchronized."
                if projection_sync.get("ok")
                else "Active workflow projections could not be synchronized.",
                details={
                    "status": projection_sync.get("status"),
                    "changed": projection_sync.get("changed"),
                    "errors": projection_sync.get("errors", []),
                    "warnings": projection_sync.get("warnings", []),
                },
            )
        )
    elif status_before_read_models == "pass":
        projection_sync = None
        read_model_result = rebuild_read_models(project, write=False)
        checks.append(
            _check(
                "read_models_fresh_or_rebuildable",
                "pass" if read_model_result.get("ok") else "fail",
                "Read models are rebuildable from authoritative runtime files."
                if read_model_result.get("ok")
                else "Read models could not be rebuilt from authoritative runtime files.",
                details={
                    "status": read_model_result.get("status"),
                    "errors": read_model_result.get("errors", []),
                    "warnings": read_model_result.get("warnings", []),
                },
            )
        )
    else:
        projection_sync = None
        checks.append(
            _check(
                "read_models_rebuild_deferred",
                "pass",
                "Read model rebuild was deferred until deterministic final-verification blockers are resolved.",
                details={"blocked_status": status_before_read_models},
            )
        )

    verification_status = _status_from_checks(checks)
    status = "waiting_runner_availability" if runner_availability is not None else verification_status
    blockers = _blockers(checks)
    if status != "pass" and write:
        _update_runtime_state(
            paths,
            status=status if runner_availability is not None else "final_verification_failed",
            scheduler_update={"last_action": "run_final_verification", "owner": owner},
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "status": status,
        "ok": status == "pass",
        "pass": status == "pass",
        "checked_at": checked_at,
        "scheduler_owner": owner,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "write_requested": requested_write,
        "write_performed": bool(write),
        "force": bool(force),
        "input_fingerprint": input_fingerprint["fingerprint"],
        "input_fingerprint_schema_version": input_fingerprint["schema_version"],
        "input_fingerprint_components": {
            key: value
            for key, value in input_fingerprint.items()
            if key not in {"fingerprint", "algorithm", "schema_version"}
        },
        "evidence_manifest": _path_for_record(project, evidence_manifest_path),
        "evidence_manifest_sha256": _sha256_file(evidence_manifest_path),
        "archived_completion_markers": archived_markers,
        "git_checkpoint_results": _compact_checkpoint_results(git_checkpoint_results),
        "read_model_rebuild": _compact_read_model_result(read_model_result),
        "projection_sync": projection_sync,
        "final_reviewer": final_reviewer_result,
        "runner_availability": runner_availability,
        "next_step": "runner_availability_wait" if runner_availability is not None else None,
    }
    report_path = paths.runtime_dir / FINAL_REPORT_FILENAME
    if write:
        _atomic_write_json(report_path, report)
    report["final_verification_report"] = _path_for_record(project, report_path)
    report["final_verification_report_sha256"] = _sha256_file(report_path)

    if status == "pass" and write:
        completion_values = {
            "plan_sha256": plan_sha,
            "event_log_head": _event_log_head(paths.runtime_dir / "events"),
            "evidence_manifest_sha256": _sha256_file(evidence_manifest_path),
            "final_verification_report_sha256": _sha256_file(report_path),
            "final_git_checkpoint_id": final_git_checkpoint_id,
        }
        objective_closure_sha = _objective_completion_fingerprint(paths, marker={})
        if objective_closure_sha is not None:
            completion_values["objective_closure_sha256"] = objective_closure_sha
        completion_values["state_fingerprint"] = _state_fingerprint(completion_values)
        marker = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "completed_at": utc_timestamp(),
            "status": "completed",
            **completion_values,
            "final_verification_report": _path_for_record(project, report_path),
        }
        marker_path = paths.runtime_dir / COMPLETION_MARKER_FILENAME
        _atomic_write_json(marker_path, marker)
        report["completion_marker_path"] = _path_for_record(project, marker_path)
        report["completion_marker"] = _completion_marker_status(paths)
        report["workflow_registry_update"] = mark_workflow_completed(
            project,
            workflow_id,
            completion_marker=_path_for_record(project, marker_path),
            final_verification_report=_path_for_record(project, report_path),
            summary=_workflow_completion_summary(tasks),
            updated_by=owner,
        )

    return report


def _run_final_reviewer_agent(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    owner: str,
    evidence_manifest_path: Path,
    tasks: Sequence[FinalVerifierTask],
    deterministic_checks: Sequence[Mapping[str, Any]],
    deterministic_warnings: Sequence[str],
    write: bool,
) -> dict[str, Any] | None:
    if not write:
        return None
    try:
        runner = load_agent_runners(project).runner("final_reviewer")
        if runner.role != "final_reviewer" or not runner.enabled:
            return None
        adapter = get_adapter(runner.adapter)
    except (AgentRunnerConfigError, AdapterLookupError, OSError, json.JSONDecodeError):
        return None

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_id = f"final_reviewer_{stamp}_{uuid.uuid4().hex[:8]}"
    role_output_dir = paths.runtime_dir / "final_review" / run_id
    role_output_dir.mkdir(parents=True, exist_ok=True)
    review_path = role_output_dir / FINAL_REVIEWER_REPORT_FILENAME
    deterministic_report_path = role_output_dir / "deterministic_final_verification_input.json"
    prompt_path = role_output_dir / "final_reviewer_prompt.md"
    deterministic_report = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "owner": owner,
        "status": _status_from_checks(deterministic_checks),
        "checks": list(deterministic_checks),
        "blockers": _blockers(deterministic_checks),
        "warnings": list(deterministic_warnings),
        "evidence_manifest": _path_for_record(project, evidence_manifest_path),
    }
    _atomic_write_json(deterministic_report_path, deterministic_report)
    prompt_text = _build_final_reviewer_prompt(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        run_id=run_id,
        evidence_manifest_path=evidence_manifest_path,
        deterministic_report_path=deterministic_report_path,
        review_path=review_path,
        tasks=tasks,
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")
    base = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "role": "final_reviewer",
        "runner_id": runner.runner_id,
        "adapter": runner.adapter,
        "role_output_dir": _path_for_record(project, role_output_dir),
        "prompt_path": _path_for_record(project, prompt_path),
        "final_reviewer_report_path": _path_for_record(project, review_path),
        "deterministic_final_verification_input": _path_for_record(project, deterministic_report_path),
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
            env={
                "LOOPPLANE_PROJECT_ROOT": project.as_posix(),
                "LOOPPLANE_PROJECT_ROOT_REL": ".",
                "LOOPPLANE_WORKFLOW_ID": workflow_id,
                "LOOPPLANE_RUN_ID": run_id,
                "LOOPPLANE_PLAN_FILE": paths.plan_file.as_posix(),
                "LOOPPLANE_PLAN_FILE_REL": _path_for_record(project, paths.plan_file),
                "LOOPPLANE_SHARED_CONTEXT_FILE": paths.shared_context_file.as_posix(),
                "LOOPPLANE_SHARED_CONTEXT_FILE_REL": _path_for_record(project, paths.shared_context_file),
                "LOOPPLANE_RESULTS_DIR": paths.results_dir.as_posix(),
                "LOOPPLANE_RESULTS_DIR_REL": _path_for_record(project, paths.results_dir),
                "LOOPPLANE_RUNTIME_DIR": paths.runtime_dir.as_posix(),
                "LOOPPLANE_RUNTIME_DIR_REL": _path_for_record(project, paths.runtime_dir),
                "LOOPPLANE_SCHEDULER_RUN_DIR": role_output_dir.as_posix(),
                "LOOPPLANE_SCHEDULER_RUN_DIR_REL": _path_for_record(project, role_output_dir),
                "LOOPPLANE_ROLE_OUTPUT_DIR": role_output_dir.as_posix(),
                "LOOPPLANE_ROLE_OUTPUT_DIR_REL": _path_for_record(project, role_output_dir),
                "LOOPPLANE_FINAL_REVIEWER_REPORT_PATH": review_path.as_posix(),
                "LOOPPLANE_DETERMINISTIC_FINAL_VERIFICATION_INPUT": deterministic_report_path.as_posix(),
            },
            role="final_reviewer",
        )
        adapter_input.write_json(role_output_dir / ADAPTER_INPUT_FILENAME)
        output = adapter.run(adapter_input)
        output.write_json()
    except (AdapterContractError, NotImplementedError, OSError, ValueError) as error:
        return {
            **base,
            "status": "agent_failed",
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }
    except Exception as error:  # pragma: no cover - defensive adapter isolation.
        return {
            **base,
            "status": "agent_failed",
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }

    review = _read_json_object(review_path, default={})
    ok = output.exit_code == 0 and not output.timed_out and isinstance(review, Mapping) and bool(review)
    result = {
        **base,
        "status": "agent_reviewed" if ok else "agent_failed",
        "ok": ok,
        "exit_code": output.exit_code,
        "timed_out": output.timed_out,
        "stdout_path": _path_for_record(project, output.stdout_path),
        "stderr_path": _path_for_record(project, output.stderr_path),
        "final_output_path": _path_for_record(project, output.final_output_path),
        "adapter_result_path": _path_for_record(project, output.adapter_result_path),
        "review": _json_safe(review if isinstance(review, Mapping) else {}),
        "error": None if ok else "final reviewer agent did not produce a readable final_reviewer_report.json",
    }
    availability = output.adapter_metadata.get("runner_availability")
    if isinstance(availability, Mapping) and availability.get("status") == "unavailable":
        result["runner_availability"] = _json_safe(availability)
    return result


def _final_reviewer_hard_blockers(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for check in checks:
        if str(check.get("status") or "") != "fail":
            continue
        name = str(check.get("check") or "")
        if name not in FINAL_REVIEWER_HARD_BLOCKER_CHECKS:
            continue
        details = check.get("details") if isinstance(check.get("details"), Mapping) else {}
        blockers.append(
            {
                "check": name,
                "message": check.get("message"),
                "details": _json_safe(details),
            }
        )
    return blockers


def _skipped_final_reviewer_result(
    *,
    project: Path,
    workflow_id: str,
    owner: str,
    reason: str,
    blockers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "run_id": f"final_reviewer_skipped_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
        "role": "final_reviewer",
        "runner_id": None,
        "adapter": None,
        "status": "skipped",
        "ok": True,
        "skipped": True,
        "owner": owner,
        "reason": reason,
        "hard_blockers": _json_safe(list(blockers)),
        "role_output_dir": None,
        "prompt_path": None,
        "final_reviewer_report_path": None,
        "project_root": project.as_posix(),
    }


def _build_final_reviewer_prompt(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    run_id: str,
    evidence_manifest_path: Path,
    deterministic_report_path: Path,
    review_path: Path,
    tasks: Sequence[FinalVerifierTask],
) -> str:
    template = read_process_template(FINAL_REVIEWER_TEMPLATE_PATH)
    deliverables = [
        {
            "task_id": task.task_id,
            "title": task.title,
            "status": task.status_label,
            "deliverables": task.deliverables,
            "evidence_root": task.evidence_root,
            "latest": task.latest_path_value,
        }
        for task in tasks
    ]
    variables = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "brief_file": paths.value("brief_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "plan_file": paths.value("plan_file"),
        "runtime_dir": paths.value("runtime_dir"),
        "role_output_dir": _path_for_record(project, review_path.parent) or "",
        "read_models_dir": paths.value("read_models_dir"),
        "evidence_manifest_file": _path_for_record(project, evidence_manifest_path) or "",
        "final_verification_report_file": _path_for_record(project, deterministic_report_path) or "",
        "final_reviewer_report_path": _path_for_record(project, review_path) or "",
        "final_deliverables": json.dumps(deliverables, indent=2, sort_keys=True),
    }
    return _render_template(template, variables)


def _final_reviewer_check(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("skipped") is True:
        return _check(
            "semantic_final_review",
            "pass",
            "Final reviewer agent was skipped because deterministic hard blockers must be resolved first.",
            details={
                "reason": result.get("reason"),
                "hard_blockers": _json_safe(result.get("hard_blockers") or []),
            },
        )
    if result.get("ok") is not True:
        return _check(
            "semantic_final_review",
            "fail",
            "Final reviewer agent did not complete successfully.",
            details={"final_reviewer": _json_safe(result)},
        )
    review = result.get("review") if isinstance(result.get("review"), Mapping) else {}
    status = str(review.get("status") or "").strip().lower()
    if status in {"accepted", "accepted_with_warnings", "pass", "pass_with_warnings"}:
        check_status = "pass"
        message = "Final reviewer agent accepted completion semantics."
    elif status == "needs_human":
        check_status = "fail"
        message = "Final reviewer agent requested human review before completion."
    else:
        check_status = "fail"
        message = "Final reviewer agent rejected completion semantics."
    return _check(
        "semantic_final_review",
        check_status,
        message,
        details={
            "status": status or "missing",
            "confidence": review.get("confidence"),
            "rationale": review.get("rationale"),
            "findings": _list_strings(review.get("findings")),
            "residual_risks": _list_strings(review.get("residual_risks")),
            "recommended_action": review.get("recommended_action"),
            "final_reviewer_report_path": result.get("final_reviewer_report_path"),
            "adapter_result_path": result.get("adapter_result_path"),
        },
    )


def _apply_final_reviewer_semantic_overrides(
    checks: Sequence[Mapping[str, Any]],
    final_reviewer_result: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    review = final_reviewer_result.get("review") if isinstance(final_reviewer_result.get("review"), Mapping) else {}
    status = str(review.get("status") or "").strip().lower()
    if final_reviewer_result.get("ok") is not True or status not in {
        "accepted",
        "accepted_with_warnings",
        "pass",
        "pass_with_warnings",
    }:
        return [dict(check) for check in checks], []

    final_reviewer_ref = {
        "status": status,
        "confidence": review.get("confidence"),
        "rationale": review.get("rationale"),
        "recommended_action": review.get("recommended_action"),
        "final_reviewer_report_path": final_reviewer_result.get("final_reviewer_report_path"),
    }
    updated_checks: list[dict[str, Any]] = []
    overridden: list[str] = []
    for check in checks:
        updated = dict(check)
        name = str(updated.get("check") or "")
        if updated.get("status") == "fail" and name in FINAL_REVIEWER_OVERRIDABLE_CHECKS:
            details = updated.get("details") if isinstance(updated.get("details"), Mapping) else {}
            updated["status"] = "pass"
            updated["message"] = f"Final reviewer agent semantically accepted deterministic check {name!r}."
            updated["details"] = {
                **dict(details),
                "deterministic_status": "fail",
                "deterministic_message": check.get("message"),
                "overridden_by_final_reviewer": final_reviewer_ref,
            }
            overridden.append(name)
        updated_checks.append(updated)
    if not overridden:
        return updated_checks, []
    return (
        updated_checks,
        [
            "Final reviewer agent accepted completion despite deterministic final verifier blocker(s): "
            + ", ".join(sorted(set(overridden)))
            + "."
        ],
    )


def final_verifier_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("status") == "pass" or result.get("pass") is True:
        return EXIT_SUCCESS
    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        failed_checks = {
            str(check.get("check") or "")
            for check in checks
            if isinstance(check, Mapping) and check.get("status") == "fail"
        }
        if "workflow_configuration" in failed_checks:
            return EXIT_INVALID_CONFIG
        if "plan_parseable" in failed_checks:
            return EXIT_PLAN_MALFORMED
    return EXIT_FINAL_VERIFICATION_FAILED


def format_final_verifier_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane final-verify: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    report_path = result.get("final_verification_report")
    if report_path:
        lines.append(f"final_verification_report: {report_path}")
    marker_path = result.get("completion_marker_path")
    if marker_path:
        lines.append(f"completion_marker: {marker_path}")
    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        lines.append("checks:")
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            lines.append(f"  - [{check.get('status', 'unknown')}] {check.get('check', 'unknown')}: {check.get('message', '')}")
    blockers = result.get("blockers")
    if isinstance(blockers, Sequence) and blockers and not isinstance(blockers, (str, bytes)):
        lines.append("blockers:")
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(f"  - {blocker.get('check', 'unknown')}: {blocker.get('message', '')}")
            else:
                lines.append(f"  - {blocker}")
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and warnings and not isinstance(warnings, (str, bytes)):
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _configuration_failure(*, project: Path, checked_at: str, owner: str, error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": None,
        "status": "fail",
        "ok": False,
        "pass": False,
        "project_root": project.as_posix(),
        "checked_at": checked_at,
        "scheduler_owner": owner,
        "checks": [_check("workflow_configuration", "fail", f"Unable to load workflow configuration: {error}")],
        "blockers": [{"check": "workflow_configuration", "message": str(error)}],
        "warnings": [],
    }


def _parse_plan_tasks(plan_text: str) -> tuple[list[FinalVerifierTask], list[str]]:
    lines = plan_text.splitlines()
    tasks: list[FinalVerifierTask] = []
    errors: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = TASK_LINE_RE.match(line)
        if not match:
            index += 1
            continue
        task_id = match.group("task_id")
        start = index
        index += 1
        while index < len(lines):
            if TASK_LINE_RE.match(lines[index]) or lines[index].startswith("## Phase ") or is_task_block_terminator(lines[index]):
                break
            index += 1
        block_lines = lines[start:index]
        if task_id in seen:
            errors.append(f"Duplicate task id {task_id!r}.")
        seen.add(task_id)
        tasks.append(
            FinalVerifierTask(
                task_id=task_id,
                status=match.group("status"),
                title=match.group("title").strip(),
                fields=_task_fields(block_lines),
                line_index=start,
                block="\n".join(block_lines).rstrip(),
            )
        )
    if "- active: true" not in plan_text:
        errors.append("PLAN.md is missing active metadata '- active: true'.")
    if not tasks:
        errors.append("PLAN.md does not contain any task checklist entries.")
    return tasks, errors


def _task_fields(block_lines: Sequence[str]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in block_lines[1:]:
        match = FIELD_LINE_RE.match(line)
        if match:
            key = match.group("field").strip().lower().replace("-", "_").replace(" ", "_")
            fields.setdefault(key, []).append(match.group("value").strip())
            current_key = key
            continue
        if current_key and (line.startswith("    ") or line.startswith("      ")):
            continuation = line.strip()
            if continuation:
                fields[current_key][-1] = f"{fields[current_key][-1]}\n{continuation}"
    return {key: tuple(values) for key, values in fields.items()}


def _check_task_states(tasks: Sequence[FinalVerifierTask]) -> dict[str, Any]:
    pending = [task.task_id for task in tasks if task.status == " "]
    partial = [task.task_id for task in tasks if task.status == "~"]
    blocked = [task.task_id for task in tasks if task.status == "!"]
    failures = []
    if pending:
        failures.append(f"active pending tasks remain: {', '.join(pending)}")
    if partial:
        failures.append(f"partial tasks remain: {', '.join(partial)}")
    if blocked:
        failures.append(f"blocked tasks remain: {', '.join(blocked)}")
    return _check(
        "no_active_pending_partial_or_blocked_tasks",
        "fail" if failures else "pass",
        "All tasks are terminal [x] or [-]." if not failures else "; ".join(failures),
        details={"pending": pending, "partial": partial, "blocked": blocked},
    )


def _workflow_completion_summary(tasks: Sequence[FinalVerifierTask]) -> dict[str, Any]:
    completed = sum(1 for task in tasks if task.status == "x")
    skipped = sum(1 for task in tasks if task.status == "-")
    blocked = sum(1 for task in tasks if task.status == "!")
    return {
        "one_line": f"Workflow completed with {completed} completed task(s) and {skipped} skipped task(s).",
        "tasks_total": len(tasks),
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": skipped,
    }


def _check_latest_and_validations(
    project: Path,
    paths: WorkflowPaths,
    tasks: Sequence[FinalVerifierTask],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_tasks: list[dict[str, Any]] = []
    for task in tasks:
        record: dict[str, Any] = {
            "task_id": task.task_id,
            "status": task.status_label,
            "evidence_root": task.evidence_root,
        }
        if task.status == "-":
            record["skip_reason"] = task.skip_reason
            record["skip_authorization"] = task.skip_authorization
            manifest_tasks.append(record)
            continue
        if task.status != "x":
            manifest_tasks.append(record)
            continue
        latest_path = _latest_path(project, paths, task)
        latest = _read_json_object(latest_path)
        record["latest_path"] = _path_for_record(project, latest_path)
        if latest is None:
            errors.append(f"{task.task_id}: missing or malformed latest.json at {_path_for_record(project, latest_path)}")
            manifest_tasks.append(record)
            continue
        latest_task_id = str(latest.get("task_id") or "")
        if latest_task_id and latest_task_id != task.task_id:
            errors.append(f"{task.task_id}: latest.json task_id is {latest_task_id!r}")
        validation_path = _project_path(project, latest.get("validation_path"))
        validation_status = str(latest.get("validation_status") or "")
        record["latest_run_id"] = latest.get("latest_run_id")
        record["validation_path"] = _path_for_record(project, validation_path) if validation_path else None
        record["validation_status"] = validation_status
        if validation_status not in PASSING_VALIDATION_STATUSES:
            errors.append(f"{task.task_id}: latest validation_status {validation_status!r} is not passing")
        validation = _read_json_object(validation_path) if validation_path is not None else None
        if validation is None:
            errors.append(f"{task.task_id}: missing or malformed authoritative validation.json")
            manifest_tasks.append(record)
            continue
        actual_status = str(validation.get("status") or "")
        if actual_status not in PASSING_VALIDATION_STATUSES:
            errors.append(f"{task.task_id}: authoritative validation status {actual_status!r} is not passing")
        if actual_status != validation_status:
            errors.append(f"{task.task_id}: latest validation_status does not match validation.json status")
        validation_warnings = [str(warning) for warning in validation.get("warnings", []) if str(warning)] if isinstance(validation.get("warnings"), Sequence) and not isinstance(validation.get("warnings"), (str, bytes)) else []
        if actual_status == "pass_with_warnings" or validation_status == "pass_with_warnings" or validation_warnings:
            record["validation_warnings"] = validation_warnings
            suffix = f": {'; '.join(validation_warnings[:4])}" if validation_warnings else ""
            warnings.append(f"{task.task_id}: validation passed with warnings{suffix}")
        accepted = validation.get("accepted_task_ids")
        if not isinstance(accepted, Sequence) or isinstance(accepted, (str, bytes)) or task.task_id not in [str(item) for item in accepted]:
            errors.append(f"{task.task_id}: authoritative validation did not accept the task")
        manifest_tasks.append(record)
    return (
        _check(
            "all_completed_tasks_have_latest_and_passing_validation",
            "fail" if errors else "pass",
            "Every completed task has latest.json and passing authoritative validation."
            if not errors
            else "One or more completed tasks lack passing latest validation.",
            details={"errors": errors, "warnings": warnings},
        ),
        manifest_tasks,
    )


def _manifest_tasks_by_id(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            continue
        tasks[task_id] = dict(record)
    return tasks


def _final_verification_task_input_record(
    paths: WorkflowPaths,
    task: FinalVerifierTask,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "status": task.status,
        "evidence_root": task.evidence_root,
        "latest_path": task.latest_path_value or None,
    }
    if task.status != "x":
        return record

    latest_path = _latest_path(paths.project_root, paths, task)
    latest = _read_json_object(latest_path, default={})
    record["latest_path"] = _path_for_record(paths.project_root, latest_path)
    record["latest_sha256"] = _sha256_file(latest_path)
    if not latest:
        record["latest_state"] = "missing_or_malformed"
        return record

    validation_path = _project_path(paths.project_root, latest.get("validation_path"))
    latest_run_dir = _project_path(paths.project_root, latest.get("latest_run_dir"))
    if latest_run_dir is None and validation_path is not None:
        latest_run_dir = validation_path.parent
    record.update(
        {
            "latest_run_id": latest.get("latest_run_id"),
            "latest_run_dir": _path_for_record(paths.project_root, latest_run_dir),
            "validation_path": _path_for_record(paths.project_root, validation_path),
            "validation_status": latest.get("validation_status"),
            "validation_sha256": _sha256_file(validation_path),
            "evidence_run_tree": _directory_tree_fingerprint(paths.project_root, latest_run_dir),
        }
    )
    return record


def _directory_tree_fingerprint(project: Path, root: Path | None) -> dict[str, Any] | None:
    if root is None:
        return None
    root_record = _path_for_record(project, root)
    if not root.is_dir():
        return {"path": root_record, "state": "missing_or_not_directory", "file_count": 0, "tree_sha256": None}
    entries: list[dict[str, Any]] = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError:
        candidates = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                entries.append({"path": relative, "kind": "symlink", "target": path.readlink().as_posix()})
            elif path.is_file():
                entries.append({"path": relative, "kind": "file", "sha256": _sha256_file(path)})
        except OSError as error:
            entries.append({"path": relative, "kind": "unreadable", "error": error.__class__.__name__})
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "path": root_record,
        "state": "present",
        "file_count": len(entries),
        "tree_sha256": "sha256:" + sha256(encoded).hexdigest(),
    }


def _legacy_final_verification_report_freshness(
    paths: WorkflowPaths,
    report: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    stale_reasons: list[str] = []
    checked_at = _parse_utc_timestamp(report.get("checked_at"))
    if checked_at is None:
        stale_reasons.append("legacy_checked_at_missing_or_invalid")

    report_plan_sha256 = str(report.get("plan_sha256") or "")
    if report_plan_sha256 and report_plan_sha256 != str(current.get("active_plan_sha256") or ""):
        stale_reasons.append("legacy_plan_sha256_mismatch")

    report_task_count = _legacy_report_task_count(report)
    current_task_count = int(current.get("task_count") or 0)
    if report_task_count is not None and report_task_count != current_task_count:
        stale_reasons.append("legacy_task_count_mismatch")

    newer_authoritative_records: list[dict[str, str]] = []
    if checked_at is not None:
        for record in _authoritative_task_update_times(paths):
            updated_at = _parse_utc_timestamp(record.get("updated_at"))
            if updated_at is not None and updated_at > checked_at:
                newer_authoritative_records.append(
                    {
                        "task_id": str(record.get("task_id") or ""),
                        "source": str(record.get("source") or ""),
                        "updated_at": str(record.get("updated_at") or ""),
                    }
                )
        if newer_authoritative_records:
            stale_reasons.append("legacy_authoritative_task_state_newer_than_report")

    return {
        "fresh": not stale_reasons,
        "mode": "legacy_timestamp_fallback",
        "stale_reasons": _dedupe_strings(stale_reasons),
        "report_checked_at": report.get("checked_at"),
        "report_task_count": report_task_count,
        "current_task_count": current_task_count,
        "newer_authoritative_records": newer_authoritative_records,
        "current_components": dict(current),
    }


def _legacy_report_task_count(report: Mapping[str, Any]) -> int | None:
    checks = report.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
        return None
    for check in checks:
        if not isinstance(check, Mapping) or str(check.get("check") or "") != "plan_parseable":
            continue
        details = check.get("details") if isinstance(check.get("details"), Mapping) else {}
        value = details.get("task_count")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _authoritative_task_update_times(paths: WorkflowPaths) -> list[dict[str, str]]:
    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
        tasks, _errors = _parse_plan_tasks(plan_text)
    except OSError:
        return []
    records: list[dict[str, str]] = []
    for task in tasks:
        if task.status != "x":
            continue
        latest_path = _latest_path(paths.project_root, paths, task)
        latest = _read_json_object(latest_path, default={})
        latest_updated_at = str(latest.get("updated_at") or "")
        if latest_updated_at:
            records.append({"task_id": task.task_id, "source": "latest", "updated_at": latest_updated_at})
        validation_path = _project_path(paths.project_root, latest.get("validation_path"))
        validation = _read_json_object(validation_path, default={}) if validation_path is not None else {}
        for field in ("validated_at", "completed_at", "checked_at", "updated_at"):
            value = str(validation.get(field) or "")
            if value:
                records.append({"task_id": task.task_id, "source": f"validation.{field}", "updated_at": value})
    return records


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _check_skipped_tasks(tasks: Sequence[FinalVerifierTask]) -> dict[str, Any]:
    errors = []
    for task in tasks:
        if task.status != "-":
            continue
        if not task.skip_reason:
            errors.append(f"{task.task_id}: missing skip_reason")
        if not task.skip_authorization:
            errors.append(f"{task.task_id}: missing skip_authorization or approval_id")
    return _check(
        "skipped_tasks_authorized",
        "fail" if errors else "pass",
        "Every skipped task has a reason and authorization." if not errors else "One or more skipped tasks are unauthorized.",
        details={"errors": errors},
    )


def _check_final_deliverables(project: Path, paths: WorkflowPaths, tasks: Sequence[FinalVerifierTask]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for task in tasks:
        if task.status != "x":
            continue
        if not task.deliverables:
            errors.append(f"{task.task_id}: missing deliverables field")
        evidence_root = _project_path(project, task.evidence_root)
        if evidence_root is None:
            errors.append(f"{task.task_id}: missing evidence root")
        elif not evidence_root.exists():
            errors.append(f"{task.task_id}: evidence root does not exist at {_path_for_record(project, evidence_root)}")
        elif not any(evidence_root.iterdir()):
            warnings.append(f"{task.task_id}: evidence root exists but is empty")
    return _check(
        "required_final_deliverables_exist",
        "fail" if errors else "pass",
        "Completed tasks declare deliverables and have evidence roots." if not errors else "Required final deliverables are missing.",
        details={"errors": errors, "warnings": warnings},
    )


def _check_objective_closure(
    paths: WorkflowPaths,
    *,
    plan_sha256: str | None,
    objectives: Sequence[Any],
    parse_errors: Sequence[str],
) -> dict[str, Any]:
    if parse_errors:
        return _check(
            "objectives_closed_by_agentic_verification",
            "fail",
            "Objective checklist entries are malformed.",
            details={"errors": list(parse_errors)},
        )
    if not objectives:
        return _check(
            "objectives_closed_by_agentic_verification",
            "fail",
            "No objective checklist entries are declared.",
            details={
                "total": 0,
                "errors": ["PLAN.md must declare objective checklist entries before final completion."],
            },
        )
    errors: list[str] = []
    warnings: list[str] = []
    target_objective_ids: list[str] = []
    report_details: list[dict[str, Any]] = []
    try:
        from runtime.objective_verification import objective_report_path, objective_result_is_closed, objective_results_by_id
    except Exception as error:
        return _check(
            "objectives_closed_by_agentic_verification",
            "fail",
            "Objective verification helpers could not be loaded.",
            details={"errors": [str(error)]},
        )
    report_cache: dict[Path, Mapping[str, Any]] = {}
    for objective in objectives:
        report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id)
        report = report_cache.get(report_path)
        if report is None:
            report = _read_json_object(report_path, default={})
            report_cache[report_path] = report
        rel_path = _path_for_record(paths.project_root, report_path)
        if not report:
            errors.append(f"{objective.objective_id}: missing objective verification report at {rel_path}")
            target_objective_ids.append(objective.objective_id)
            continue
        same_report_objectives = [
            item
            for item in objectives
            if item.scope == objective.scope and item.phase_id == objective.phase_id
        ]
        current_structure_fingerprint = objective_structure_fingerprint(
            "",
            objectives=same_report_objectives,
        )
        report_structure_fingerprint = str(
            report.get("objective_structure_fingerprint")
            or report.get("objective_structure_sha256")
            or ""
        )
        report_is_fresh = (
            report_structure_fingerprint == current_structure_fingerprint
            if report_structure_fingerprint
            else str(report.get("plan_sha256") or "") == str(plan_sha256 or "")
        )
        if not report_is_fresh:
            errors.append(f"{objective.objective_id}: objective verification report is stale at {rel_path}")
            target_objective_ids.append(objective.objective_id)
            continue
        results = objective_results_by_id(report)
        result = results.get(objective.objective_id)
        if not isinstance(result, Mapping):
            errors.append(f"{objective.objective_id}: objective verification report has no result for this objective")
            target_objective_ids.append(objective.objective_id)
            continue
        if not objective_result_is_closed(result):
            errors.append(f"{objective.objective_id}: objective remains unresolved ({result.get('verdict') or result.get('status')})")
            target_objective_ids.append(objective.objective_id)
        if _objective_result_is_waived(result) and not _objective_waiver_reason(result):
            errors.append(f"{objective.objective_id}: waived objective is missing an explicit policy reason")
            target_objective_ids.append(objective.objective_id)
        if objective.status not in {"x", "-"}:
            warnings.append(f"{objective.objective_id}: PLAN.md checkbox is not closed ({objective.status_label})")
        report_details.append(
            {
                "objective_id": objective.objective_id,
                "scope": objective.scope,
                "phase_id": objective.phase_id,
                "report_path": rel_path,
                "report_status": report.get("status"),
                "report_plan_sha256": report.get("plan_sha256"),
                "report_accepted_plan_sha256": report.get("accepted_plan_sha256"),
                "report_objective_structure_fingerprint": report_structure_fingerprint or None,
                "current_objective_structure_fingerprint": current_structure_fingerprint,
                "result_status": result.get("status") if isinstance(result, Mapping) else None,
                "verdict": result.get("verdict") if isinstance(result, Mapping) else None,
                "waiver_reason": _objective_waiver_reason(result) if isinstance(result, Mapping) else "",
            }
        )
    return _check(
        "objectives_closed_by_agentic_verification",
        "fail" if errors else "pass",
        "All declared objectives are closed by fresh agentic objective verification reports."
        if not errors
        else "Declared objectives are not closed by fresh agentic objective verification reports.",
        details={
            "errors": errors,
            "warnings": warnings,
            "target_objective_ids": sorted(set(target_objective_ids)),
            "objectives": report_details,
        },
    )


def _objective_result_is_waived(result: Mapping[str, Any]) -> bool:
    return str(result.get("status") or "").lower() == "waived" or str(result.get("verdict") or "").lower() == "waived_by_policy"


def _objective_waiver_reason(result: Mapping[str, Any]) -> str:
    for key in ("policy_reason", "waiver_reason", "waived_reason"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return ""


def _check_failures(paths: WorkflowPaths) -> dict[str, Any]:
    registry = _read_json_object(paths.runtime_dir / FAILURE_REGISTRY_FILENAME, default={"failures": []})
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    unresolved: list[dict[str, Any]] = []
    malformed = not isinstance(failures, Sequence) or isinstance(failures, (str, bytes))
    if not malformed:
        for failure in failures:
            if not isinstance(failure, Mapping):
                malformed = True
                continue
            status = str(failure.get("status") or "unrecovered").lower()
            if status not in RESOLVED_FAILURE_STATUSES:
                unresolved.append(
                    {
                        "failure_id": failure.get("failure_id"),
                        "task_id": failure.get("task_id"),
                        "status": status,
                    }
                )
    if malformed:
        return _check("no_unrecovered_failures", "fail", "Failure registry is malformed.")
    return _check(
        "no_unrecovered_failures",
        "fail" if unresolved else "pass",
        "No unrecovered failures remain." if not unresolved else "Unrecovered failures remain.",
        details={"unresolved": unresolved},
    )


def _check_active_leases(paths: WorkflowPaths) -> dict[str, Any]:
    lease_dir = paths.runtime_dir / "active_run_leases"
    if not lease_dir.exists():
        return _check("no_active_run_leases", "pass", "Active run lease directory is absent.")
    active: list[str] = []
    malformed: list[str] = []
    for path in sorted(lease_dir.glob("*.json")):
        lease = _read_json_object(path)
        rel = _path_for_record(paths.project_root, path)
        if not isinstance(lease, Mapping):
            malformed.append(rel)
            continue
        if not _lease_blocks_scheduler(lease):
            continue
        status = str(lease.get("status") or "running").lower()
        if status not in INACTIVE_LEASE_STATUSES:
            active.append(rel)
    problems = {"active": active, "malformed": malformed}
    return _check(
        "no_active_run_leases",
        "fail" if active or malformed else "pass",
        "No active run leases remain." if not active and not malformed else "Active or malformed run leases remain.",
        details=problems,
    )


def _lease_blocks_scheduler(lease: Mapping[str, Any]) -> bool:
    if lease.get("blocks_scheduler") is False:
        return False
    return str(lease.get("role") or "").strip().lower() != "inspector"


def _check_background_jobs(paths: WorkflowPaths) -> dict[str, Any]:
    registry = _read_json_object(paths.runtime_dir / BACKGROUND_JOBS_FILENAME, default={"jobs": []})
    jobs = registry.get("jobs", []) if isinstance(registry, Mapping) else []
    unsafe: list[dict[str, Any]] = []
    malformed = not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes))
    if not malformed:
        for job in jobs:
            if not isinstance(job, Mapping):
                malformed = True
                continue
            status = str(job.get("status") or "running").lower()
            next_prompt_ready = job.get("next_prompt_ready")
            if status not in SAFE_BACKGROUND_JOB_STATUSES or next_prompt_ready is False:
                unsafe.append(
                    {
                        "job_id": job.get("job_id") or job.get("background_job_id") or job.get("run_id"),
                        "task_id": job.get("task_id"),
                        "status": status,
                        "next_prompt_ready": next_prompt_ready,
                    }
                )
    if malformed:
        return _check("no_active_background_jobs", "fail", "Background job registry is malformed.")
    return _check(
        "no_active_background_jobs",
        "fail" if unsafe else "pass",
        "No active background jobs remain." if not unsafe else "Active or unsafe background jobs remain.",
        details={"unsafe": unsafe},
    )


def _check_pending_approvals(paths: WorkflowPaths) -> dict[str, Any]:
    requests = _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME)
    responses = _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME)
    pending: list[dict[str, Any]] = []
    now = utc_timestamp()
    for request in requests:
        status = approval_record_status(request, responses=responses, now=now)
        if status.get("status") == "pending":
            pending.append(
                {
                    "approval_id": status.get("approval_id"),
                    "task_id": status.get("task_id"),
                    "type": status.get("type"),
                }
            )
    return _check(
        "no_pending_approval_requests",
        "fail" if pending else "pass",
        "No pending approval requests remain." if not pending else "Pending approval requests remain.",
        details={"pending": pending},
    )


def _check_git_checkpoint_requirement(project: Path, paths: WorkflowPaths, workflow_id: str) -> dict[str, Any]:
    if not _version_control_enabled(paths):
        override = _no_version_control_override(paths)
        if override:
            return _check("required_git_checkpoints", "pass", "No-version-control override is explicitly approved.", details=override)
        return _check("required_git_checkpoints", "fail", "Version control is disabled without an approved no-version-control override.")
    latest = _latest_git_checkpoint_id(paths.runtime_dir / "git_checkpoints.jsonl")
    return _check(
        "required_git_checkpoints",
        "pass",
        "Version control is enabled; final verifier will create required final checkpoints."
        if latest is None
        else "Version control is enabled and prior Git checkpoints exist.",
        details={"latest_checkpoint_id": latest, "workflow_id": workflow_id, "project_root": project.as_posix()},
    )


def _append_final_events(paths: WorkflowPaths, *, workflow_id: str, owner: str) -> None:
    from runtime.scheduler import append_event

    append_event(
        paths,
        workflow_id=workflow_id,
        event_type="final_verification_finished",
        data={"owner": owner, "status": "pass"},
    )
    append_event(
        paths,
        workflow_id=workflow_id,
        event_type="workflow_completed",
        data={"owner": owner, "status": "completed", "completion_marker": _path_for_record(paths.project_root, paths.runtime_dir / COMPLETION_MARKER_FILENAME)},
    )


def _completion_marker_status(paths: WorkflowPaths) -> dict[str, Any]:
    marker_path = _completion_marker_path(paths)
    if marker_path is None:
        return {"exists": False, "fresh": False, "path": None, "stale_reasons": []}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"exists": True, "fresh": False, "path": marker_path.as_posix(), "stale_reasons": ["invalid_json"]}
    except OSError as error:
        return {"exists": True, "fresh": False, "path": marker_path.as_posix(), "stale_reasons": [f"read_error:{error.__class__.__name__}"]}
    if not isinstance(marker, Mapping):
        return {"exists": True, "fresh": False, "path": marker_path.as_posix(), "stale_reasons": ["marker_not_object"]}
    current = _completion_freshness_values(paths, marker)
    stale_reasons: list[str] = []
    audit_drift_reasons: list[str] = []
    for field, current_value in current.items():
        if field not in marker:
            _append_completion_freshness_reason(field, f"{field}_missing", stale_reasons, audit_drift_reasons)
            continue
        marker_value = marker.get(field)
        if marker_value is None and current_value is None:
            continue
        if marker_value is None:
            _append_completion_freshness_reason(field, f"{field}_missing", stale_reasons, audit_drift_reasons)
        elif current_value is None:
            _append_completion_freshness_reason(field, f"{field}_current_unavailable", stale_reasons, audit_drift_reasons)
        elif str(marker_value) != str(current_value):
            _append_completion_freshness_reason(field, f"{field}_mismatch", stale_reasons, audit_drift_reasons)
    return {
        "exists": True,
        "fresh": not stale_reasons,
        "path": marker_path.as_posix(),
        "marker_name": marker_path.name,
        "stale_reasons": stale_reasons,
        "audit_drift_reasons": audit_drift_reasons,
        "current": current,
    }


def _append_completion_freshness_reason(
    field: str,
    reason: str,
    stale_reasons: list[str],
    audit_drift_reasons: list[str],
) -> None:
    if field in {"event_log_head", "state_fingerprint"}:
        audit_drift_reasons.append(reason)
        return
    stale_reasons.append(reason)


def _completion_marker_path(paths: WorkflowPaths) -> Path | None:
    for name in (COMPLETION_MARKER_FILENAME, "completion_marker.json"):
        path = paths.runtime_dir / name
        if path.exists():
            return path
    return None


def _completion_freshness_values(paths: WorkflowPaths, marker: Mapping[str, Any]) -> dict[str, str | None]:
    report_path = _project_path(paths.project_root, marker.get("final_verification_report") or marker.get("final_verification_path"))
    values = {
        "plan_sha256": _sha256_file(paths.plan_file),
        "evidence_manifest_sha256": _sha256_file(paths.runtime_dir / EVIDENCE_MANIFEST_FILENAME),
        "event_log_head": _event_log_head(paths.runtime_dir / "events"),
        "final_verification_report_sha256": _sha256_file(report_path) if report_path else None,
        "final_git_checkpoint_id": _latest_git_checkpoint_id(paths.runtime_dir / "git_checkpoints.jsonl"),
    }
    objective_closure_sha = _objective_completion_fingerprint(paths, marker)
    if objective_closure_sha is not None:
        values["objective_closure_sha256"] = objective_closure_sha
    values["state_fingerprint"] = _state_fingerprint(values)
    return values


def _objective_completion_fingerprint(paths: WorkflowPaths, marker: Mapping[str, Any]) -> str | None:
    try:
        plan_text = paths.plan_file.read_text(encoding="utf-8")
    except OSError:
        plan_text = ""
    objectives, _errors = parse_plan_objectives(plan_text)
    if not objectives and "objective_closure_sha256" not in marker:
        return None
    report_paths: dict[str, str] = {}
    try:
        from runtime.objective_verification import objective_report_path

        for objective in objectives:
            report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id)
            if report_path.exists():
                report_paths[objective.objective_id] = _path_for_record(paths.project_root, report_path)
    except Exception:
        report_paths = {}
    return objective_closure_fingerprint(plan_text, project_root=paths.project_root, report_paths=report_paths)


def _archive_completion_marker(paths: WorkflowPaths, marker_path_value: object) -> Path | None:
    if not isinstance(marker_path_value, str) or not marker_path_value:
        return None
    marker_path = Path(marker_path_value)
    if not marker_path.is_absolute():
        marker_path = paths.project_root / marker_path
    if not marker_path.exists():
        return None
    archive_dir = paths.runtime_dir / "stale_completion_markers"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        marker_digest = sha256(marker_path.read_bytes()).hexdigest()
    except OSError:
        marker_digest = "unreadable"
    archive_path = archive_dir / f"{marker_path.stem}.stale.sha256-{marker_digest}{marker_path.suffix}"
    marker_path.replace(archive_path)
    return archive_path


def _latest_path(project: Path, paths: WorkflowPaths, task: FinalVerifierTask) -> Path:
    latest = task.latest_path_value or f"{paths.value('results_dir').rstrip('/')}/{task.task_id}/latest.json"
    path = Path(latest)
    if path.is_absolute():
        return path
    return project / latest


def _version_control_enabled(paths: WorkflowPaths) -> bool:
    config = _read_json_object(paths.version_control_config_file, default={})
    if not isinstance(config, Mapping):
        return False
    return config.get("enabled") is not False


def _no_version_control_override(paths: WorkflowPaths) -> dict[str, Any] | None:
    config = _read_json_object(paths.version_control_config_file, default={})
    if not isinstance(config, Mapping):
        return None
    override = config.get("no_version_control_override")
    if not isinstance(override, Mapping):
        return None
    if override.get("approved") is True and (override.get("approval_id") or override.get("skip_authorization")):
        return dict(override)
    return None


def _read_json_object(path: Path | None, default: Any = None) -> Any:
    if path is None:
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _render_template(template: str, variables: Mapping[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


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
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            records.append(dict(record))
    return records


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = str(cwd or "{{project_root}}").replace("{{project_root}}", project.as_posix())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _update_runtime_state(
    paths: WorkflowPaths,
    *,
    status: str,
    scheduler_update: Mapping[str, Any],
) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_object(state_path, default={})
    if not isinstance(state, dict):
        state = {}
    state["status"] = status
    scheduler = state.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
    scheduler.update(dict(scheduler_update))
    scheduler["updated_at"] = utc_timestamp()
    state["scheduler"] = scheduler
    _atomic_write_json(state_path, state)


def _check(check: str, status: str, message: str, *, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    record = {"check": check, "status": status, "message": message}
    if details:
        record["details"] = _json_safe(details)
    return record


def _status_from_checks(checks: Sequence[Mapping[str, Any]]) -> str:
    return "fail" if any(check.get("status") == "fail" for check in checks) else "pass"


def _blockers(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for check in checks:
        if check.get("status") != "fail":
            continue
        blockers.append(_classified_blocker(check))
    return blockers


def _classified_blocker(check: Mapping[str, Any]) -> dict[str, Any]:
    name = str(check.get("check") or "unknown")
    message = str(check.get("message") or "")
    details = check.get("details") if isinstance(check.get("details"), Mapping) else {}
    recommended_action = str(details.get("recommended_action") or "").strip().lower()
    blocker: dict[str, Any] = {
        "check": name,
        "message": message,
        "kind": "non_expandable",
        "expandable": False,
    }
    if name == "no_active_pending_partial_or_blocked_tasks":
        target_task_ids = _task_ids_from_state_details(details)
        blocker.update(
            {
                "kind": "nonterminal_tasks",
                "expandable": bool(target_task_ids),
                "suggested_expansion_type": "stale_or_partial_task",
                "target_task_ids": target_task_ids,
            }
        )
    elif name == "all_completed_tasks_have_latest_and_passing_validation":
        target_task_ids = _task_ids_from_error_details(details)
        blocker.update(
            {
                "kind": "validation_gap",
                "expandable": bool(target_task_ids) or bool(details.get("errors")),
                "suggested_expansion_type": "validation_gap",
                "target_task_ids": target_task_ids,
            }
        )
    elif name == "required_final_deliverables_exist":
        target_task_ids = _task_ids_from_error_details(details)
        blocker.update(
            {
                "kind": "missing_deliverable",
                "expandable": bool(target_task_ids) or bool(details.get("errors")),
                "suggested_expansion_type": "missing_deliverable",
                "target_task_ids": target_task_ids,
            }
        )
    elif name == "no_unrecovered_failures":
        unresolved = details.get("unresolved")
        unresolved_records = [dict(item) for item in unresolved if isinstance(item, Mapping)] if isinstance(unresolved, Sequence) and not isinstance(unresolved, (str, bytes)) else []
        blocker.update(
            {
                "kind": "unresolved_failures",
                "expandable": bool(unresolved_records),
                "suggested_expansion_type": "failed_recovery_decomposition",
                "target_failure_ids": [str(item.get("failure_id") or "") for item in unresolved_records if str(item.get("failure_id") or "")],
                "target_task_ids": [str(item.get("task_id") or "") for item in unresolved_records if str(item.get("task_id") or "")],
            }
        )
    elif name == "objectives_closed_by_agentic_verification":
        target_objective_ids = [
            str(item)
            for item in details.get("target_objective_ids", [])
            if str(item)
        ]
        blocker.update(
            {
                "kind": "objective_gap",
                "expandable": bool(target_objective_ids),
                "suggested_expansion_type": "objective_gap",
                "target_objective_ids": target_objective_ids,
            }
        )
    elif name in {"workflow_configuration", "plan_parseable", "read_models_fresh_or_rebuildable", "active_workflow_projections_current"}:
        blocker["kind"] = "runtime_configuration"
    elif name in {"no_active_run_leases", "no_active_background_jobs", "no_pending_approval_requests"}:
        blocker["kind"] = "runtime_wait"
    elif name == "semantic_final_review" and recommended_action == "self_expand":
        blocker.update(
            {
                "kind": "semantic_final_review",
                "expandable": True,
                "suggested_expansion_type": "final_verifier_retry",
            }
        )
    if details:
        blocker["details"] = _json_safe(details)
    return blocker


def _task_ids_from_state_details(details: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for field in ("pending", "partial", "blocked"):
        value = details.get(field)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            ids.extend(str(item) for item in value if str(item))
    return _dedupe_strings(ids)


def _task_ids_from_error_details(details: Mapping[str, Any]) -> list[str]:
    errors = details.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        return []
    ids: list[str] = []
    for error in errors:
        text = str(error)
        prefix = text.split(":", 1)[0].strip()
        if prefix:
            ids.append(prefix)
    return _dedupe_strings(ids)


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = str(value).strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return result


def _compact_checkpoint_results(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for result in results:
        checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), Mapping) else {}
        compact.append(
            {
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "reason": checkpoint.get("reason"),
                "errors": list(result.get("errors") or []),
                "warnings": list(result.get("warnings") or []),
            }
        )
    return compact


def _compact_read_model_result(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "read_models_dir": result.get("read_models_dir"),
        "written_files": list(result.get("written_files") or []),
        "errors": list(result.get("errors") or []),
        "warnings": list(result.get("warnings") or []),
    }


def _first_field(fields: Mapping[str, tuple[str, ...]], *keys: str) -> str | None:
    for key in keys:
        values = fields.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def _project_path(project: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return project / value


def _path_for_record(project: Path | None, path: Path | None) -> str | None:
    if path is None:
        return None
    if project is None:
        return path.as_posix()
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _sha256_file(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _event_log_head(events_dir: Path) -> str | None:
    segments = sorted(events_dir.glob("*.jsonl"))
    if not segments:
        return None
    last_record: dict[str, Any] | None = None
    last_segment: Path | None = None
    last_line_hash: str | None = None
    for segment in segments:
        try:
            lines = segment.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            last_segment = segment
            last_line_hash = "sha256:" + sha256(line.encode("utf-8")).hexdigest()
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            last_record = dict(record) if isinstance(record, Mapping) else None
    if last_segment is None:
        return None
    if last_record is not None:
        event_id = last_record.get("event_id") or last_record.get("id")
        if isinstance(event_id, str) and event_id:
            return event_id
        sequence = last_record.get("sequence")
        if sequence is not None:
            return f"{last_segment.name}:sequence:{sequence}"
    return f"{last_segment.name}:{last_line_hash}"


def _latest_git_checkpoint_id(path: Path) -> str | None:
    records = _read_jsonl(path)
    for record in reversed(records):
        checkpoint_id = record.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            return checkpoint_id
    return None


def _state_fingerprint(values: Mapping[str, str | None]) -> str:
    state_values = {
        "plan_sha256": values.get("plan_sha256"),
        "evidence_manifest_sha256": values.get("evidence_manifest_sha256"),
        "event_log_head": values.get("event_log_head"),
        "final_verification_report_sha256": values.get("final_verification_report_sha256"),
        "final_git_checkpoint_id": values.get("final_git_checkpoint_id"),
    }
    if "objective_closure_sha256" in values:
        state_values["objective_closure_sha256"] = values.get("objective_closure_sha256")
    encoded = json.dumps(state_values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
