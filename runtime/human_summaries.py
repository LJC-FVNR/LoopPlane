from __future__ import annotations

import json
import re
import threading
import uuid
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from runtime.adapters.base import ADAPTER_INPUT_FILENAME, AdapterContractError, AdapterInput, utc_timestamp
from runtime.adapters.registry import AdapterLookupError, get_adapter
from runtime.agent_runners import AgentRunnerConfigError, load_agent_runners
from runtime.file_discovery import discover_files_bounded
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import parse_plan_objectives
from runtime.prompt_context import data_reference, file_reference, json_summary, prompt_reference_index, write_json_file
from runtime.reconciliation import PlanTask, parse_plan_tasks


SCHEMA_VERSION = "1.7"
TASK_SUMMARY_MD = "human_summary.md"
TASK_SUMMARY_JSON = "human_summary.json"
PHASE_SUMMARY_MD = "human_summary.md"
PHASE_SUMMARY_JSON = "human_summary.json"
SUMMARY_SELECTION_FILENAME = "summary_selection.json"
SUMMARY_CONTEXT_MANIFEST_FILENAME = "summary_context_manifest.json"
SUMMARY_TARGET_RECORD_CONTEXT_FILENAME = "summary_target_record.json"
SUMMARY_EVIDENCE_CONTEXT_FILENAME = "summary_evidence.json"
SUMMARY_REPORT_CONTEXT_FILENAME = "summary_report.md"
SUMMARY_VISUAL_ARTIFACTS_CONTEXT_FILENAME = "summary_visual_artifacts.json"
TERMINAL_TASK_STATUSES = frozenset({"x", "!", "-"})
SUMMARY_READY_STATUS = "ready"
SUMMARY_SCHEDULED_STATUS = "scheduled"
SUMMARY_AGENT_INLINE_WAIT_SECONDS = 1.0
SUMMARY_AGENT_NONBLOCKING_LIMIT = 2
SUMMARY_ARTIFACT_SCAN_LIMIT = 256
SUMMARY_ARTIFACT_SCAN_ENTRY_LIMIT = 2_048
TEXT_SECTION_LIMIT = 420
VISUAL_ARTIFACT_SUFFIXES = frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"})
GENERIC_SUMMARY_PHRASES = (
    "worker says it is complete",
    "worker claims completion",
    "adapter completed and agent_status.json is available",
    "adapter completed and agent status.json is available",
    "worker adapter completed and agent_status.json is available",
    "worker adapter completed and agent status.json is available",
    "the task completed and the validator accepted the evidence",
    "reached done status",
    "no caveats were recorded",
    "no phase-level caveats were recorded",
    "no changed-file table was recorded",
)


def ensure_human_summaries(
    project_root: Path | str,
    *,
    task_ids: Sequence[str] | None = None,
    force: bool = False,
    write: bool = True,
    blocking: bool = True,
    inline_wait_seconds: float | None = None,
    max_agent_summaries: int | None = None,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
        plan_text = paths.plan_file.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(project, started_at, f"Unable to load workflow inputs: {error}")

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    tasks = parse_plan_tasks(plan_text)
    selected_ids = {str(task_id) for task_id in task_ids or [] if str(task_id).strip()}
    if not selected_ids:
        selected_ids = {task_id for task_id, task in tasks.items() if task.status in TERMINAL_TASK_STATUSES}

    task_results: list[dict[str, Any]] = []
    touched_phases: set[str] = set()
    scheduled_phases: set[str] = set()
    agent_summary_limit = _agent_summary_limit(blocking=blocking, max_agent_summaries=max_agent_summaries)
    agent_summary_count = 0
    for task_id in sorted(selected_ids):
        task = tasks.get(task_id)
        if task is None:
            task_results.append({"task_id": task_id, "status": "skipped", "reason": "task_not_found"})
            continue
        if task.status not in TERMINAL_TASK_STATUSES:
            task_results.append({"task_id": task_id, "status": "skipped", "reason": "task_not_terminal"})
            continue
        result = _ensure_task_summary(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            task=task,
            force=force,
            write=write,
            blocking=blocking,
            inline_wait_seconds=inline_wait_seconds,
            allow_agent_summary=_agent_summary_slot_available(agent_summary_count, agent_summary_limit),
        )
        task_results.append(result)
        if _result_used_summary_agent(result):
            agent_summary_count += 1
        if result.get("status") in {SUMMARY_READY_STATUS, "current", "would_write"}:
            touched_phases.add(task.phase or "Unphased")
        if result.get("status") == SUMMARY_SCHEDULED_STATUS:
            touched_phases.add(task.phase or "Unphased")
            scheduled_phases.add(task.phase or "Unphased")

    phase_results: list[dict[str, Any]] = []
    tasks_by_phase = _tasks_by_phase(tasks.values())
    for phase_title, phase_tasks in tasks_by_phase.items():
        if touched_phases and phase_title not in touched_phases:
            continue
        if not phase_tasks:
            continue
        if any(task.status not in TERMINAL_TASK_STATUSES for task in phase_tasks):
            continue
        result = _ensure_phase_summary(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            phase_title=phase_title,
            phase_tasks=phase_tasks,
            force=force,
            write=write,
            blocking=blocking,
            inline_wait_seconds=inline_wait_seconds,
            allow_agent_summary=phase_title not in scheduled_phases
            and _agent_summary_slot_available(agent_summary_count, agent_summary_limit),
        )
        phase_results.append(result)
        if _result_used_summary_agent(result):
            agent_summary_count += 1

    generated = [
        result
        for result in (*task_results, *phase_results)
        if result.get("status") in {SUMMARY_READY_STATUS, "would_write"}
    ]
    scheduled = [
        result
        for result in (*task_results, *phase_results)
        if result.get("status") == SUMMARY_SCHEDULED_STATUS or result.get("background_summary_agent")
    ]
    if write and generated:
        _append_summary_events(paths, workflow_id=workflow_id, results=generated)
    status = "summaries_updated" if generated else "summaries_scheduled" if scheduled else "current"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "task_results": task_results,
        "phase_results": phase_results,
        "written_count": len(generated) if write else 0,
        "would_write_count": len(generated) if not write else 0,
        "scheduled_count": len(scheduled),
        "errors": [],
        "warnings": [],
    }


def load_task_human_summary(project: Path, paths: WorkflowPaths, task_id: str) -> dict[str, Any]:
    return _load_summary_record(project, task_summary_json_path(paths, task_id))


def load_phase_human_summary(project: Path, paths: WorkflowPaths, phase_title: str) -> dict[str, Any]:
    return _load_summary_record(project, phase_summary_json_path(paths, phase_title))


def task_human_summary_source_hash(project: Path, paths: WorkflowPaths, task: PlanTask) -> str:
    return _task_source_hash(project, paths, task)


def phase_human_summary_source_hash(
    project: Path,
    paths: WorkflowPaths,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
) -> str:
    return _phase_source_hash(project, paths, phase_title, phase_tasks)


def task_summary_markdown_path(paths: WorkflowPaths, task_id: str) -> Path:
    return paths.results_dir / task_id / TASK_SUMMARY_MD


def task_summary_json_path(paths: WorkflowPaths, task_id: str) -> Path:
    return paths.results_dir / task_id / TASK_SUMMARY_JSON


def phase_summary_markdown_path(paths: WorkflowPaths, phase_title: str) -> Path:
    return paths.results_dir / "phases" / _phase_id(phase_title) / PHASE_SUMMARY_MD


def phase_summary_json_path(paths: WorkflowPaths, phase_title: str) -> Path:
    return paths.results_dir / "phases" / _phase_id(phase_title) / PHASE_SUMMARY_JSON


def format_human_summary_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane summarize: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"tasks: {len(result.get('task_results') or [])}",
        f"phases: {len(result.get('phase_results') or [])}",
        f"written: {result.get('written_count', 0)}",
    ]
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def human_summary_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def _ensure_task_summary(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    task: PlanTask,
    force: bool,
    write: bool,
    blocking: bool,
    inline_wait_seconds: float | None,
    allow_agent_summary: bool,
) -> dict[str, Any]:
    md_path = task_summary_markdown_path(paths, task.task_id)
    json_path = task_summary_json_path(paths, task.task_id)
    source_hash = _task_source_hash(project, paths, task)
    existing = _read_json_object(json_path, default={})
    if (
        not force
        and isinstance(existing, Mapping)
        and existing.get("status") == SUMMARY_READY_STATUS
        and existing.get("schema_version") == SCHEMA_VERSION
        and _mapping(existing.get("source_hashes")).get("summary_source") == source_hash
        and md_path.exists()
    ):
        return {
            "kind": "task",
            "task_id": task.task_id,
            "status": "current",
            "markdown_path": _path_for_record(project, md_path),
            "json_path": _path_for_record(project, json_path),
        }

    agent_result = _try_agent_task_summary(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        task=task,
        source_hash=source_hash,
        md_path=md_path,
        json_path=json_path,
        write=write,
        blocking=blocking,
        inline_wait_seconds=inline_wait_seconds,
        allow_agent_summary=allow_agent_summary,
    )
    if agent_result is not None:
        return agent_result

    record, markdown = _build_task_summary(project, paths, workflow_id, task, source_hash, md_path)
    if write:
        _atomic_write_text(md_path, markdown)
        _atomic_write_json(json_path, record)
    return {
        "kind": "task",
        "task_id": task.task_id,
        "status": SUMMARY_READY_STATUS if write else "would_write",
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
    }


def _ensure_phase_summary(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
    force: bool,
    write: bool,
    blocking: bool,
    inline_wait_seconds: float | None,
    allow_agent_summary: bool,
) -> dict[str, Any]:
    md_path = phase_summary_markdown_path(paths, phase_title)
    json_path = phase_summary_json_path(paths, phase_title)
    source_hash = _phase_source_hash(project, paths, phase_title, phase_tasks)
    existing = _read_json_object(json_path, default={})
    if (
        not force
        and isinstance(existing, Mapping)
        and existing.get("status") == SUMMARY_READY_STATUS
        and existing.get("schema_version") == SCHEMA_VERSION
        and _mapping(existing.get("source_hashes")).get("summary_source") == source_hash
        and md_path.exists()
    ):
        return {
            "kind": "phase",
            "phase_id": _phase_id(phase_title),
            "phase_title": phase_title,
            "status": "current",
            "markdown_path": _path_for_record(project, md_path),
            "json_path": _path_for_record(project, json_path),
        }

    agent_result = _try_agent_phase_summary(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        phase_title=phase_title,
        phase_tasks=phase_tasks,
        source_hash=source_hash,
        md_path=md_path,
        json_path=json_path,
        write=write,
        blocking=blocking,
        inline_wait_seconds=inline_wait_seconds,
        allow_agent_summary=allow_agent_summary,
    )
    scheduled_agent_result: dict[str, Any] | None = None
    if agent_result is not None:
        if agent_result.get("status") != SUMMARY_SCHEDULED_STATUS:
            return agent_result
        scheduled_agent_result = agent_result

    record, markdown = _build_phase_summary(project, paths, workflow_id, phase_title, phase_tasks, source_hash, md_path)
    if write:
        _atomic_write_text(md_path, markdown)
        _atomic_write_json(json_path, record)
    result = {
        "kind": "phase",
        "phase_id": _phase_id(phase_title),
        "phase_title": phase_title,
        "status": SUMMARY_READY_STATUS if write else "would_write",
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
    }
    if scheduled_agent_result is not None:
        result["background_summary_agent"] = dict(scheduled_agent_result.get("summary_agent") or {})
        result["warnings"] = list(scheduled_agent_result.get("warnings") or [])
    return result


def _build_task_summary(
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    task: PlanTask,
    source_hash: str,
    md_path: Path,
) -> tuple[dict[str, Any], str]:
    latest_path = _latest_path(project, paths, task)
    latest = _read_json_object(latest_path, default={})
    validation_path = _path_from_value(project, _mapping(latest).get("validation_path"))
    validation = _read_json_object(validation_path, default={}) if validation_path is not None else {}
    run_dir = _path_from_value(project, _mapping(latest).get("latest_run_dir")) or _path_from_value(project, _mapping(validation).get("run_dir"))
    agent_status = _read_json_object(run_dir / "agent_status.json", default={}) if run_dir is not None else {}
    run_execution = _read_json_object(run_dir / "run_execution.json", default={}) if run_dir is not None else {}
    report_path = run_dir / "report.md" if run_dir is not None else None
    report_text = _read_text(report_path)
    changed_files = _read_changed_files(run_dir)
    artifacts = _artifact_list(run_dir)
    visual_evidence = _visual_evidence_markdown(project, run_dir)

    status_label = _human_task_status(task.status)
    validation_status = str(_mapping(validation).get("status") or _mapping(latest).get("validation_status") or "unknown")
    executive_summary = _task_executive_summary(
        task=task,
        status_label=status_label,
        validation_status=validation_status,
        validation=validation,
        agent_status=agent_status,
        run_execution=run_execution,
        report_text=report_text,
    )
    progress_items = _task_progress_items(task, report_text, agent_status, changed_files, artifacts)
    project_meaning = _task_project_meaning(task, status_label, validation_status)
    checks = _validation_checks(validation)
    confidence_items = _task_confidence_items(
        task=task,
        validation_status=validation_status,
        validation=validation,
        agent_status=agent_status,
        checks=checks,
        artifacts=artifacts,
    )
    attention_items = _task_attention_items(task, validation, agent_status, run_execution)
    key_data = [
        ("Focus", _display_task_title(task)),
        ("Phase", _display_phase_title(task.phase or "Unphased")),
        ("Outcome", _leadership_outcome_label(status_label)),
        ("Strategic increment", _excerpt(project_meaning, 180)),
    ]
    if visual_evidence:
        key_data.append(("Visual signal", f"{len(_visual_artifact_records(project, run_dir))} leadership-facing figure(s)"))

    markdown_blocks = [
        f"# {_task_report_title(task, status_label)}",
        executive_summary,
        _prose_from_items(progress_items),
        project_meaning,
        _attention_paragraph(attention_items),
        _visual_paragraph(visual_evidence) if visual_evidence else "",
    ]
    markdown = _join_markdown_blocks(markdown_blocks)
    if not markdown.endswith("\n"):
        markdown += "\n"
    tables = _leadership_tables(
        {
            "strategic_signals": [
                {"Signal": key, "Reading": value}
                for key, value in key_data
                if key in {"Outcome", "Strategic increment", "Visual signal"}
            ]
        }
    )
    excerpt = _excerpt(executive_summary)
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": "task",
        "status": SUMMARY_READY_STATUS,
        "workflow_id": workflow_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "phase_title": task.phase or "Unphased",
        "generated_at": utc_timestamp(),
        "summary_title": f"Task {task.task_id}: {task.title}",
        "summary_excerpt": excerpt,
        "executive_summary": executive_summary,
        "delivered_progress": progress_items,
        "project_meaning": project_meaning,
        "evidence_and_confidence": confidence_items,
        "leadership_attention": attention_items,
        "markdown_path": _path_for_record(project, md_path),
        "content": markdown,
        "source_hashes": {
            "summary_source": source_hash,
            "latest": _sha256_file(latest_path),
            "validation": _sha256_file(validation_path) if validation_path is not None else None,
            "report": _sha256_file(report_path),
            "artifacts": _artifact_tree_hash(run_dir),
        },
        "key_data": [{"label": key, "value": value} for key, value in key_data],
        "tables": tables,
        "figures": _visual_artifact_records(project, run_dir),
    }
    return record, markdown


def _build_phase_summary(
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
    source_hash: str,
    md_path: Path,
) -> tuple[dict[str, Any], str]:
    task_records = [load_task_human_summary(project, paths, task.task_id) for task in phase_tasks]
    done = sum(1 for task in phase_tasks if task.status == "x")
    blocked = sum(1 for task in phase_tasks if task.status == "!")
    skipped = sum(1 for task in phase_tasks if task.status == "-")
    task_rows = []
    progress_items: list[str] = []
    attention_items: list[str] = []
    for task in phase_tasks:
        summary = _summary_for_task_id(task_records, task.task_id)
        excerpt = str(summary.get("summary_excerpt") or summary.get("excerpt") or _human_task_status(task.status))
        task_rows.append({"Task": task.task_id, "Status": _human_task_status(task.status), "Summary": excerpt})
        progress = summary.get("delivered_progress")
        _append_unique(progress_items, summary.get("executive_summary") or excerpt, limit=320)
        if isinstance(progress, Sequence) and not isinstance(progress, (str, bytes)):
            for progress_item in progress[:2]:
                _append_unique(progress_items, progress_item, limit=280)
        task_label = _display_task_title(task)
        if task.status == "!":
            attention_items.append(f"{task_label} still constrains phase-level confidence.")
        if task.status == "-":
            attention_items.append(f"{task_label} was treated as out of scope for this phase; leadership should read the phase impact accordingly.")
        if summary.get("status") != SUMMARY_READY_STATUS:
            attention_items.append(f"{task_label} lacks a ready leadership summary, so the phase narrative is less complete than the evidence record.")
        summary_attention = summary.get("leadership_attention")
        if isinstance(summary_attention, Sequence) and not isinstance(summary_attention, (str, bytes)):
            for item in summary_attention[:2]:
                text = str(item)
                if "No blocker or decision request" not in text:
                    _append_unique(attention_items, text, limit=260)
    if not progress_items:
        progress_items.append("The phase reached a terminal state, but the available reports were too thin to describe a strong strategic increment.")
    phase_objective_rows = _objective_closure_rows(project, paths, phase_title=phase_title, scope="phase")
    workflow_objective_rows = _objective_closure_rows(project, paths, phase_title=None, scope="workflow")
    for row in [*phase_objective_rows, *workflow_objective_rows]:
        if str(row.get("Closed") or "").lower() != "yes":
            _append_unique(
                attention_items,
                f"A declared objective remains below closure confidence, which limits how strongly this phase can support broader project decisions.",
                limit=260,
            )
    executive_summary = _phase_executive_summary(
        phase_title=phase_title,
        total=len(phase_tasks),
        done=done,
        blocked=blocked,
        skipped=skipped,
        progress_items=progress_items,
    )
    control_items = _phase_control_items(len(phase_tasks), done, blocked, skipped)
    project_meaning = _phase_project_meaning(
        phase_title=phase_title,
        total=len(phase_tasks),
        done=done,
        blocked=blocked,
        skipped=skipped,
        phase_objective_rows=phase_objective_rows,
        workflow_objective_rows=workflow_objective_rows,
    )
    markdown_blocks = [
        f"# {_phase_report_title(phase_title, blocked=blocked, skipped=skipped)}",
        executive_summary,
        _prose_from_items(progress_items),
        project_meaning,
        _attention_paragraph(attention_items),
    ]
    markdown = _join_markdown_blocks(markdown_blocks)
    if not markdown.endswith("\n"):
        markdown += "\n"
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": "phase",
        "status": SUMMARY_READY_STATUS,
        "workflow_id": workflow_id,
        "phase_id": _phase_id(phase_title),
        "phase_title": phase_title,
        "generated_at": utc_timestamp(),
        "summary_title": phase_title,
        "summary_excerpt": _excerpt(executive_summary),
        "executive_summary": executive_summary,
        "delivered_progress": progress_items,
        "project_meaning": project_meaning,
        "control_points": control_items,
        "leadership_attention": attention_items,
        "markdown_path": _path_for_record(project, md_path),
        "content": markdown,
        "source_hashes": {"summary_source": source_hash},
        "key_data": [
            {"label": "Focus", "value": _display_phase_title(phase_title)},
            {"label": "Outcome", "value": _phase_outcome_label(done=done, blocked=blocked, skipped=skipped, total=len(phase_tasks))},
            {"label": "Strategic increment", "value": _excerpt(project_meaning, 180)},
        ],
        "tables": {
            "task_outcomes": task_rows,
            "phase_objective_closure": phase_objective_rows,
            "workflow_objective_closure": workflow_objective_rows,
        },
        "figures": _phase_figures_from_task_records(task_records),
    }
    return record, markdown


def build_summary_prompt_variables(project_root: Path | str, prepared_run: Any) -> dict[str, str]:
    project = Path(project_root).expanduser().resolve()
    role_output_dir = Path(getattr(prepared_run, "role_output_dir"))
    selection = _read_json_object(role_output_dir / SUMMARY_SELECTION_FILENAME, default={})
    if not isinstance(selection, Mapping):
        selection = {}
    brief_path = project / str(selection.get("brief_file") or "PROJECT_BRIEF.md")
    plan_path = project / str(selection.get("plan_file") or "PLAN.md")
    context_manifest = _write_summary_context_manifest(
        project=project,
        role_output_dir=role_output_dir,
        workflow_id=str(selection.get("workflow_id") or getattr(prepared_run, "workflow_id", "")),
        run_id=str(selection.get("run_id") or getattr(prepared_run, "run_id", "")),
        brief_path=brief_path,
        plan_path=plan_path,
        selection=selection,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(selection.get("workflow_id") or getattr(prepared_run, "workflow_id", "")),
        "run_id": str(selection.get("run_id") or getattr(prepared_run, "run_id", "")),
        "target_kind": str(selection.get("target_kind") or ""),
        "target_id": str(selection.get("target_id") or ""),
        "summary_markdown_path": str(selection.get("summary_markdown_path") or ""),
        "summary_json_path": str(selection.get("summary_json_path") or ""),
        "agent_status_path": str(selection.get("agent_status_path") or _path_for_record(project, role_output_dir / "agent_status.json") or ""),
        "context_manifest_path": _path_for_record(project, role_output_dir / SUMMARY_CONTEXT_MANIFEST_FILENAME) or "",
        "context_references_json": json.dumps(prompt_reference_index(context_manifest["references"]), indent=2, sort_keys=True),
        "target_record_summary_json": json_summary(selection.get("target_record") or {}, max_chars=1200),
        "visual_artifacts_summary_json": json_summary(selection.get("visual_artifacts") or [], max_chars=1200),
    }


def _write_summary_context_manifest(
    *,
    project: Path,
    role_output_dir: Path,
    workflow_id: str,
    run_id: str,
    brief_path: Path,
    plan_path: Path,
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    report_path = role_output_dir / SUMMARY_REPORT_CONTEXT_FILENAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(str(selection.get("report_markdown") or ""), encoding="utf-8")
    references: dict[str, Any] = {
        "summary_selection": file_reference(project, role_output_dir / SUMMARY_SELECTION_FILENAME, label="Summary selection"),
        "project_brief": file_reference(project, brief_path, label="Project brief"),
        "active_plan": file_reference(project, plan_path, label="Active plan"),
        "target_record": data_reference(project, role_output_dir / SUMMARY_TARGET_RECORD_CONTEXT_FILENAME, selection.get("target_record") or {}, label="Summary target record"),
        "runtime_evidence": data_reference(project, role_output_dir / SUMMARY_EVIDENCE_CONTEXT_FILENAME, selection.get("evidence") or {}, label="Runtime evidence"),
        "worker_or_phase_reports": file_reference(project, report_path, label="Worker or phase reports"),
        "visual_artifacts": data_reference(project, role_output_dir / SUMMARY_VISUAL_ARTIFACTS_CONTEXT_FILENAME, selection.get("visual_artifacts") or [], label="Visual artifacts"),
    }
    manifest = {
        "schema_version": "1.0",
        "generated_at": utc_timestamp(),
        "workflow_id": workflow_id,
        "run_id": run_id,
        "source_authority": "prompt_context_manifest_not_source_of_truth",
        "instructions": [
            "Use the referenced files as summary evidence instead of relying on prompt-inlined copies.",
            "Do not claim completion beyond evidence available in the referenced files.",
        ],
        "references": references,
    }
    write_json_file(role_output_dir / SUMMARY_CONTEXT_MANIFEST_FILENAME, manifest)
    return manifest


def _try_agent_task_summary(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    task: PlanTask,
    source_hash: str,
    md_path: Path,
    json_path: Path,
    write: bool,
    blocking: bool,
    inline_wait_seconds: float | None,
    allow_agent_summary: bool,
) -> dict[str, Any] | None:
    if not write or not allow_agent_summary:
        return None
    context = _task_summary_agent_context(project, paths, task)

    def finalize(outcome: Mapping[str, Any]) -> None:
        markdown = _read_text(md_path).strip()
        if not markdown:
            return
        agent_json = _read_json_object(json_path, default={})
        record = _agent_task_summary_record(
            project=project,
            workflow_id=workflow_id,
            task=task,
            source_hash=source_hash,
            md_path=md_path,
            json_path=json_path,
            markdown=markdown,
            agent_json=agent_json if isinstance(agent_json, Mapping) else {},
            outcome=outcome,
            context=context,
        )
        _atomic_write_json(json_path, record)

    outcome = _run_summary_agent(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        target_kind="task",
        target_id=task.task_id,
        md_path=md_path,
        json_path=json_path,
        target_record={
            "task_id": task.task_id,
            "task_title": task.title,
            "phase_title": task.phase or "Unphased",
            "status": task.status,
            "fields": {key: list(value) for key, value in task.fields.items()},
        },
        evidence=context["evidence"],
        report_markdown=context["report_markdown"],
        visual_artifacts=context["visual_artifacts"],
        blocking=blocking,
        inline_wait_seconds=inline_wait_seconds,
        on_complete=finalize if not blocking else None,
    )
    if outcome is None:
        return None
    if outcome.get("status") == SUMMARY_SCHEDULED_STATUS:
        return _summary_scheduled_result(
            "task",
            task.task_id,
            project=project,
            md_path=md_path,
            json_path=json_path,
            outcome=outcome,
        )
    if not blocking:
        markdown = _read_text(md_path).strip()
        if not markdown:
            return None
        return _summary_result("task", task.task_id, project=project, md_path=md_path, json_path=json_path, outcome=outcome)
    markdown = _read_text(md_path).strip()
    if not markdown:
        return None
    agent_json = _read_json_object(json_path, default={})
    record = _agent_task_summary_record(
        project=project,
        workflow_id=workflow_id,
        task=task,
        source_hash=source_hash,
        md_path=md_path,
        json_path=json_path,
        markdown=markdown,
        agent_json=agent_json if isinstance(agent_json, Mapping) else {},
        outcome=outcome,
        context=context,
    )
    _atomic_write_json(json_path, record)
    return _summary_result("task", task.task_id, project=project, md_path=md_path, json_path=json_path, outcome=outcome)


def _try_agent_phase_summary(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
    source_hash: str,
    md_path: Path,
    json_path: Path,
    write: bool,
    blocking: bool,
    inline_wait_seconds: float | None,
    allow_agent_summary: bool,
) -> dict[str, Any] | None:
    if not write or not allow_agent_summary:
        return None
    context = _phase_summary_agent_context(project, paths, phase_title, phase_tasks)
    phase_id = _phase_id(phase_title)

    def finalize(outcome: Mapping[str, Any]) -> None:
        markdown = _read_text(md_path).strip()
        if not markdown:
            return
        agent_json = _read_json_object(json_path, default={})
        record = _agent_phase_summary_record(
            project=project,
            workflow_id=workflow_id,
            phase_title=phase_title,
            phase_tasks=phase_tasks,
            source_hash=source_hash,
            md_path=md_path,
            json_path=json_path,
            markdown=markdown,
            agent_json=agent_json if isinstance(agent_json, Mapping) else {},
            outcome=outcome,
            context=context,
        )
        _atomic_write_json(json_path, record)

    outcome = _run_summary_agent(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        target_kind="phase",
        target_id=phase_id,
        md_path=md_path,
        json_path=json_path,
        target_record={
            "phase_id": phase_id,
            "phase_title": phase_title,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status,
                    "fields": {key: list(value) for key, value in task.fields.items()},
                }
                for task in phase_tasks
            ],
        },
        evidence=context["evidence"],
        report_markdown=context["report_markdown"],
        visual_artifacts=context["visual_artifacts"],
        blocking=blocking,
        inline_wait_seconds=inline_wait_seconds,
        on_complete=finalize if not blocking else None,
    )
    if outcome is None:
        return None
    if outcome.get("status") == SUMMARY_SCHEDULED_STATUS:
        return _summary_scheduled_result(
            "phase",
            phase_id,
            project=project,
            md_path=md_path,
            json_path=json_path,
            outcome=outcome,
        )
    if not blocking:
        markdown = _read_text(md_path).strip()
        if not markdown:
            return None
        return _summary_result("phase", phase_id, project=project, md_path=md_path, json_path=json_path, outcome=outcome)
    markdown = _read_text(md_path).strip()
    if not markdown:
        return None
    agent_json = _read_json_object(json_path, default={})
    record = _agent_phase_summary_record(
        project=project,
        workflow_id=workflow_id,
        phase_title=phase_title,
        phase_tasks=phase_tasks,
        source_hash=source_hash,
        md_path=md_path,
        json_path=json_path,
        markdown=markdown,
        agent_json=agent_json if isinstance(agent_json, Mapping) else {},
        outcome=outcome,
        context=context,
    )
    _atomic_write_json(json_path, record)
    return _summary_result("phase", phase_id, project=project, md_path=md_path, json_path=json_path, outcome=outcome)


def _run_summary_agent(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    target_kind: str,
    target_id: str,
    md_path: Path,
    json_path: Path,
    target_record: Mapping[str, Any],
    evidence: Mapping[str, Any],
    report_markdown: str,
    visual_artifacts: Sequence[Mapping[str, Any]],
    blocking: bool,
    inline_wait_seconds: float | None,
    on_complete: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    try:
        runner = load_agent_runners(project).runner("summary")
        if runner.role != "summary" or not runner.enabled:
            return None
        adapter = get_adapter(runner.adapter)
    except (AgentRunnerConfigError, AdapterLookupError, OSError, json.JSONDecodeError):
        return None
    try:
        from runtime.prompt_builder import build_prompt_for_prepared_run
        from runtime.scheduler import prepare_run

        prepared = prepare_run(
            project,
            role="summary",
            runner_id=runner.runner_id,
            scheduler_owner=f"summary:{target_kind}:{target_id}",
            blocks_scheduler=False,
            updates_runtime_state=False,
        )
        selection = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "run_id": prepared.run_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "summary_markdown_path": _path_for_record(project, md_path),
            "summary_json_path": _path_for_record(project, json_path),
            "agent_status_path": _path_for_record(project, prepared.role_output_dir / "agent_status.json"),
            "brief_file": paths.value("brief_file"),
            "plan_file": paths.value("plan_file"),
            "target_record": _json_safe(target_record),
            "evidence": _json_safe(evidence),
            "report_markdown": report_markdown,
            "visual_artifacts": _json_safe(list(visual_artifacts)),
        }
        _atomic_write_json(prepared.role_output_dir / SUMMARY_SELECTION_FILENAME, selection)
        _atomic_write_json(prepared.scheduler_run_dir / SUMMARY_SELECTION_FILENAME, selection)
        built_prompt = build_prompt_for_prepared_run(project, prepared)
        adapter_input = AdapterInput.from_runner_config(
            run_id=prepared.run_id,
            workflow_id=prepared.workflow_id,
            runner_config=runner,
            prompt_path=prepared.prompt_path,
            prompt_content=built_prompt.content,
            scheduler_run_dir=prepared.scheduler_run_dir,
            role_output_dir=prepared.role_output_dir,
            task_id=None,
            task_evidence_run_dir=None,
            cwd=str(_resolve_cwd(project, runner.cwd)),
            env={
                "LOOPPLANE_SUMMARY_TARGET_KIND": target_kind,
                "LOOPPLANE_SUMMARY_TARGET_ID": target_id,
                "LOOPPLANE_SUMMARY_MARKDOWN_PATH": md_path.as_posix(),
                "LOOPPLANE_SUMMARY_JSON_PATH": json_path.as_posix(),
            },
            role="summary",
        )
        adapter_input.write_json(prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME)
        if blocking:
            return _execute_summary_agent(
                project=project,
                prepared=prepared,
                runner_id=runner.runner_id,
                adapter_name=runner.adapter,
                adapter=adapter,
                adapter_input=adapter_input,
                on_complete=on_complete,
            )

        result: dict[str, Any] = {"done": False, "outcome": None}

        def run_background() -> None:
            result["outcome"] = _execute_summary_agent(
                project=project,
                prepared=prepared,
                runner_id=runner.runner_id,
                adapter_name=runner.adapter,
                adapter=adapter,
                adapter_input=adapter_input,
                on_complete=on_complete,
            )
            result["done"] = True

        thread = threading.Thread(
            target=run_background,
            name=f"loopplane-summary-{prepared.run_id}",
            daemon=True,
        )
        thread.start()
        thread.join(_summary_inline_wait_seconds(inline_wait_seconds))
        if result.get("done"):
            outcome = result.get("outcome")
            return outcome if isinstance(outcome, Mapping) else None
        return _summary_agent_scheduled_outcome(
            project=project,
            prepared=prepared,
            runner_id=runner.runner_id,
            adapter_name=runner.adapter,
            adapter_input=adapter_input,
        )
    except (AdapterContractError, OSError, RuntimeError, ValueError):
        return None


def _execute_summary_agent(
    *,
    project: Path,
    prepared: Any,
    runner_id: str,
    adapter_name: str,
    adapter: Any,
    adapter_input: AdapterInput,
    on_complete: Callable[[Mapping[str, Any]], None] | None,
) -> dict[str, Any] | None:
    try:
        output = adapter.run(adapter_input)
        output.write_json()
        ok = output.exit_code == 0 and not output.timed_out
        _update_summary_lease(prepared.active_run_lease_path, status="completed" if ok else "failed")
        if not ok:
            return None
        outcome = _summary_agent_completed_outcome(
            project=project,
            prepared=prepared,
            runner_id=runner_id,
            adapter_name=adapter_name,
            output=output,
        )
        if on_complete is not None:
            on_complete(outcome)
        return outcome
    except (AdapterContractError, OSError, RuntimeError, ValueError):
        _update_summary_lease(prepared.active_run_lease_path, status="failed")
        return None


def _summary_agent_completed_outcome(
    *,
    project: Path,
    prepared: Any,
    runner_id: str,
    adapter_name: str,
    output: Any,
) -> dict[str, Any]:
    return {
        "run_id": prepared.run_id,
        "node_id": prepared.node_id,
        "runner_id": runner_id,
        "adapter": adapter_name,
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "adapter_result_path": _path_for_record(project, output.adapter_result_path),
        "final_output_path": _path_for_record(project, output.final_output_path),
    }


def _summary_agent_scheduled_outcome(
    *,
    project: Path,
    prepared: Any,
    runner_id: str,
    adapter_name: str,
    adapter_input: AdapterInput,
) -> dict[str, Any]:
    paths = adapter_input.output_paths()
    return {
        "status": SUMMARY_SCHEDULED_STATUS,
        "run_id": prepared.run_id,
        "node_id": prepared.node_id,
        "runner_id": runner_id,
        "adapter": adapter_name,
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "adapter_result_path": _path_for_record(project, paths.adapter_result_path),
        "final_output_path": _path_for_record(project, paths.final_output_path),
    }


def _summary_inline_wait_seconds(value: float | None) -> float:
    if value is None:
        return SUMMARY_AGENT_INLINE_WAIT_SECONDS
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SUMMARY_AGENT_INLINE_WAIT_SECONDS
    return max(0.0, parsed)


def _agent_summary_limit(*, blocking: bool, max_agent_summaries: int | None) -> int | None:
    if blocking:
        return None
    if max_agent_summaries is None:
        return SUMMARY_AGENT_NONBLOCKING_LIMIT
    if isinstance(max_agent_summaries, bool):
        return SUMMARY_AGENT_NONBLOCKING_LIMIT
    try:
        return max(0, int(max_agent_summaries))
    except (TypeError, ValueError):
        return SUMMARY_AGENT_NONBLOCKING_LIMIT


def _agent_summary_slot_available(count: int, limit: int | None) -> bool:
    return limit is None or count < limit


def _result_used_summary_agent(result: Mapping[str, Any]) -> bool:
    if result.get("background_summary_agent"):
        return True
    return result.get("generated_by") == "summary_agent" and result.get("status") in {
        SUMMARY_READY_STATUS,
        SUMMARY_SCHEDULED_STATUS,
    }


def _task_summary_agent_context(project: Path, paths: WorkflowPaths, task: PlanTask) -> dict[str, Any]:
    latest_path = _latest_path(project, paths, task)
    latest = _read_json_object(latest_path, default={})
    validation_path = _path_from_value(project, _mapping(latest).get("validation_path"))
    validation = _read_json_object(validation_path, default={}) if validation_path is not None else {}
    run_dir = _path_from_value(project, _mapping(latest).get("latest_run_dir")) or _path_from_value(project, _mapping(validation).get("run_dir"))
    agent_status = _read_json_object(run_dir / "agent_status.json", default={}) if run_dir is not None else {}
    run_execution = _read_json_object(run_dir / "run_execution.json", default={}) if run_dir is not None else {}
    report_path = run_dir / "report.md" if run_dir is not None else None
    artifacts = _artifact_paths(run_dir)
    return {
        "evidence": {
            "latest_path": _path_for_record(project, latest_path),
            "latest": latest,
            "validation_path": _path_for_record(project, validation_path),
            "validation": validation,
            "run_dir": _path_for_record(project, run_dir),
            "agent_status": agent_status,
            "run_execution": run_execution,
            "changed_files": list(_read_changed_files(run_dir)),
            "artifacts": [_path_for_record(project, path) for path in artifacts],
        },
        "report_markdown": _read_text(report_path),
        "visual_artifacts": _visual_artifact_records(project, run_dir),
    }


def _phase_summary_agent_context(
    project: Path,
    paths: WorkflowPaths,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
) -> dict[str, Any]:
    task_records = [load_task_human_summary(project, paths, task.task_id) for task in phase_tasks]
    phase_objective_rows = _objective_closure_rows(project, paths, phase_title=phase_title, scope="phase")
    workflow_objective_rows = _objective_closure_rows(project, paths, phase_title=None, scope="workflow")
    reports = []
    for record in task_records:
        if record.get("content"):
            reports.append(str(record.get("content")))
    return {
        "evidence": {
            "phase_title": phase_title,
            "task_summaries": task_records,
            "phase_objective_closure": phase_objective_rows,
            "workflow_objective_closure": workflow_objective_rows,
        },
        "report_markdown": "\n\n".join(reports),
        "visual_artifacts": [],
    }


def _agent_task_summary_record(
    *,
    project: Path,
    workflow_id: str,
    task: PlanTask,
    source_hash: str,
    md_path: Path,
    json_path: Path,
    markdown: str,
    agent_json: Mapping[str, Any],
    outcome: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    title = str(agent_json.get("summary_title") or f"Task {task.task_id}: {task.title}")
    excerpt = _excerpt(str(agent_json.get("summary_excerpt") or markdown))
    progress = _report_bullets(markdown, limit=4) or _report_paragraphs(markdown, limit=4) or [excerpt]
    attention = _summary_caveats(_mapping(_mapping(context.get("evidence")).get("validation")), _mapping(_mapping(context.get("evidence")).get("agent_status")), _mapping(_mapping(context.get("evidence")).get("run_execution")))
    tables = _agent_summary_tables(agent_json)
    figures = _agent_summary_figures(agent_json, context)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "task",
        "status": SUMMARY_READY_STATUS,
        "workflow_id": workflow_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "phase_title": task.phase or "Unphased",
        "generated_at": utc_timestamp(),
        "generated_by": "summary_agent",
        "summary_title": title,
        "summary_excerpt": excerpt,
        "executive_summary": excerpt,
        "delivered_progress": progress,
        "project_meaning": str(agent_json.get("project_meaning") or ""),
        "evidence_and_confidence": _list_strings(agent_json.get("evidence_and_confidence")),
        "leadership_attention": attention or _list_strings(agent_json.get("leadership_attention")),
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
        "content": markdown,
        "source_hashes": {"summary_source": source_hash, "summary_markdown": _sha256_file(md_path)},
        "summary_agent": dict(outcome),
        "key_data": [
            {"label": "Task", "value": f"{task.task_id} - {task.title}"},
            {"label": "Phase", "value": task.phase or "Unphased"},
            {"label": "Generated by", "value": "summary_agent"},
        ],
        "tables": tables,
        "figures": figures,
    }


def _agent_phase_summary_record(
    *,
    project: Path,
    workflow_id: str,
    phase_title: str,
    phase_tasks: Sequence[PlanTask],
    source_hash: str,
    md_path: Path,
    json_path: Path,
    markdown: str,
    agent_json: Mapping[str, Any],
    outcome: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    phase_id = _phase_id(phase_title)
    title = str(agent_json.get("summary_title") or phase_title)
    excerpt = _excerpt(str(agent_json.get("summary_excerpt") or markdown))
    progress = _report_bullets(markdown, limit=6) or _report_paragraphs(markdown, limit=6) or [excerpt]
    done = sum(1 for task in phase_tasks if task.status == "x")
    blocked = sum(1 for task in phase_tasks if task.status == "!")
    skipped = sum(1 for task in phase_tasks if task.status == "-")
    evidence = _mapping(context.get("evidence"))
    tables = _agent_summary_tables(agent_json)
    tables.setdefault("phase_objective_closure", list(evidence.get("phase_objective_closure") or []))
    tables.setdefault("workflow_objective_closure", list(evidence.get("workflow_objective_closure") or []))
    figures = _agent_summary_figures(agent_json, context)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "phase",
        "status": SUMMARY_READY_STATUS,
        "workflow_id": workflow_id,
        "phase_id": phase_id,
        "phase_title": phase_title,
        "generated_at": utc_timestamp(),
        "generated_by": "summary_agent",
        "summary_title": title,
        "summary_excerpt": excerpt,
        "executive_summary": excerpt,
        "delivered_progress": progress,
        "control_points": _list_strings(agent_json.get("control_points")),
        "leadership_attention": _list_strings(agent_json.get("leadership_attention")),
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
        "content": markdown,
        "source_hashes": {"summary_source": source_hash, "summary_markdown": _sha256_file(md_path)},
        "summary_agent": dict(outcome),
        "key_data": [
            {"label": "Tasks", "value": str(len(phase_tasks))},
            {"label": "Completed", "value": str(done)},
            {"label": "Blocked", "value": str(blocked)},
            {"label": "Skipped", "value": str(skipped)},
        ],
        "tables": tables,
        "figures": figures,
    }


def _summary_result(
    kind: str,
    target_id: str,
    *,
    project: Path,
    md_path: Path,
    json_path: Path,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    result = {
        "kind": kind,
        "status": SUMMARY_READY_STATUS,
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
        "generated_by": "summary_agent",
        "summary_agent": dict(outcome),
    }
    if kind == "task":
        result["task_id"] = target_id
    else:
        result["phase_id"] = target_id
    return result


def _summary_scheduled_result(
    kind: str,
    target_id: str,
    *,
    project: Path,
    md_path: Path,
    json_path: Path,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    result = {
        "kind": kind,
        "status": SUMMARY_SCHEDULED_STATUS,
        "markdown_path": _path_for_record(project, md_path),
        "json_path": _path_for_record(project, json_path),
        "generated_by": "summary_agent",
        "summary_agent": dict(outcome),
        "warnings": ["Summary agent is running in the background; workflow execution is not blocked."],
    }
    if kind == "task":
        result["task_id"] = target_id
    else:
        result["phase_id"] = target_id
    return result


def _visual_artifact_records(project: Path, run_dir: Path | None) -> list[dict[str, str]]:
    return [
        {"label": path.name, "path": _path_for_record(project, path) or ""}
        for path in _artifact_paths(run_dir)
        if path.suffix.lower() in VISUAL_ARTIFACT_SUFFIXES
    ]


def _phase_figures_from_task_records(task_records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    figures: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in task_records:
        for figure in record.get("figures") or []:
            if not isinstance(figure, Mapping):
                continue
            path = str(figure.get("path") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            item = {str(key): str(value) for key, value in figure.items() if value is not None}
            figures.append(item)
    return figures


def _agent_summary_tables(agent_json: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    raw = agent_json.get("tables")
    tables: dict[str, list[dict[str, str]]] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            rows = _coerce_summary_table_rows(value)
            if rows:
                tables[_safe_metadata_key(str(key))] = rows
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, table in enumerate(raw, start=1):
            if not isinstance(table, Mapping):
                continue
            name = str(table.get("name") or table.get("id") or table.get("title") or f"table_{index}")
            rows = _coerce_summary_table_rows(table)
            if rows:
                tables[_safe_metadata_key(name)] = rows
    return tables


def _coerce_summary_table_rows(value: Any) -> list[dict[str, str]]:
    source = value
    headers: list[str] = []
    if isinstance(value, Mapping):
        raw_headers = value.get("headers") or value.get("columns")
        if isinstance(raw_headers, Sequence) and not isinstance(raw_headers, (str, bytes)):
            headers = [str(item) for item in raw_headers if str(item).strip()]
        source = value.get("rows") or value.get("data") or []
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
        return []
    rows: list[dict[str, str]] = []
    for row in source:
        if isinstance(row, Mapping):
            cleaned = {str(key): str(item) for key, item in row.items() if item is not None}
            if cleaned:
                rows.append(cleaned)
            continue
        if headers and isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            cells = [str(item) for item in row]
            cleaned = {header: cells[index] if index < len(cells) else "" for index, header in enumerate(headers)}
            if any(value for value in cleaned.values()):
                rows.append(cleaned)
    return rows


def _agent_summary_figures(agent_json: Mapping[str, Any], context: Mapping[str, Any]) -> list[dict[str, str]]:
    figures: list[dict[str, str]] = []
    seen: set[str] = set()

    def append_figure(raw: Mapping[str, Any]) -> None:
        path = _figure_path(raw)
        if not path or path in seen:
            return
        seen.add(path)
        label = str(raw.get("label") or raw.get("alt") or raw.get("title") or Path(path).name)
        figure = {"label": label, "path": path}
        for source_key, target_key in (("title", "title"), ("caption", "caption"), ("alt", "alt")):
            value = raw.get(source_key)
            if value is not None and str(value).strip():
                figure[target_key] = str(value)
        figures.append(figure)

    raw_figures = agent_json.get("figures")
    if isinstance(raw_figures, Sequence) and not isinstance(raw_figures, (str, bytes)):
        for item in raw_figures:
            if isinstance(item, Mapping):
                append_figure(item)
            elif isinstance(item, str):
                append_figure({"path": item})
    elif isinstance(raw_figures, Mapping):
        append_figure(raw_figures)

    visual_artifacts = context.get("visual_artifacts")
    if isinstance(visual_artifacts, Sequence) and not isinstance(visual_artifacts, (str, bytes)):
        for item in visual_artifacts:
            if isinstance(item, Mapping):
                append_figure(item)
    return figures


def _figure_path(raw: Mapping[str, Any]) -> str:
    for key in ("path", "src", "href", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _safe_metadata_key(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip().lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "table"


def _update_summary_lease(path: Path, *, status: str) -> None:
    lease = _read_json_object(path, default={})
    if not isinstance(lease, dict):
        lease = {}
    lease["status"] = status
    lease["heartbeat_at"] = utc_timestamp()
    lease["ended_at"] = utc_timestamp()
    _atomic_write_json(path, lease)


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = str(cwd or "{{project_root}}").replace("{{project_root}}", project.as_posix())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _append_summary_events(paths: WorkflowPaths, *, workflow_id: str, results: Sequence[Mapping[str, Any]]) -> None:
    try:
        from runtime.scheduler import append_event
    except Exception:
        return
    for result in results:
        append_event(
            paths,
            workflow_id=workflow_id,
            event_type="human_summary_written",
            run_id=str(result.get("run_id") or "") or None,
            data={
                "kind": result.get("kind"),
                "task_id": result.get("task_id"),
                "phase_id": result.get("phase_id"),
                "markdown_path": result.get("markdown_path"),
                "json_path": result.get("json_path"),
            },
        )


def _load_summary_record(project: Path, path: Path) -> dict[str, Any]:
    data = _read_json_object(path, default={})
    if not isinstance(data, Mapping) or data.get("status") != SUMMARY_READY_STATUS:
        return {"status": "missing"}
    markdown_path = _path_from_value(project, data.get("markdown_path"))
    content = str(data.get("content") or "")
    if not content and markdown_path is not None:
        content = _read_text(markdown_path)
    source_hashes = data.get("source_hashes")
    return {
        "status": SUMMARY_READY_STATUS,
        "kind": data.get("kind"),
        "task_id": data.get("task_id") or data.get("target_id"),
        "phase_id": data.get("phase_id") or data.get("target_id"),
        "phase_title": data.get("phase_title"),
        "title": data.get("summary_title"),
        "excerpt": data.get("summary_excerpt"),
        "markdown_path": data.get("markdown_path"),
        "json_path": _path_for_record(project, path),
        "generated_at": data.get("generated_at"),
        "source_hashes": dict(source_hashes) if isinstance(source_hashes, Mapping) else {},
        "content": content,
        "key_data": list(data.get("key_data") or []),
        "tables": _loaded_summary_tables(data.get("tables")),
        "figures": [dict(item) for item in data.get("figures") or [] if isinstance(item, Mapping)],
        "executive_summary": data.get("executive_summary"),
        "delivered_progress": list(data.get("delivered_progress") or []),
        "project_meaning": data.get("project_meaning"),
        "evidence_and_confidence": list(data.get("evidence_and_confidence") or []),
        "leadership_attention": list(data.get("leadership_attention") or []),
        "control_points": list(data.get("control_points") or []),
    }


def _loaded_summary_tables(value: Any) -> dict[str, list[dict[str, str]]]:
    if isinstance(value, Mapping):
        return {
            str(key): [dict(row) for row in rows if isinstance(row, Mapping)]
            for key, rows in value.items()
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
        }
    return _agent_summary_tables({"tables": value})


def _task_source_hash(project: Path, paths: WorkflowPaths, task: PlanTask) -> str:
    latest_path = _latest_path(project, paths, task)
    latest = _read_json_object(latest_path, default={})
    validation_path = _path_from_value(project, _mapping(latest).get("validation_path"))
    run_dir = _path_from_value(project, _mapping(latest).get("latest_run_dir"))
    entries = {
        "task": {
            "task_id": task.task_id,
            "status": task.status,
            "title": task.title,
            "phase": task.phase,
            "fields": {key: list(value) for key, value in task.fields.items()},
        },
        "latest_sha256": _sha256_file(latest_path),
        "validation_sha256": _sha256_file(validation_path) if validation_path is not None else None,
        "agent_status_sha256": _sha256_file(run_dir / "agent_status.json") if run_dir is not None else None,
        "run_execution_sha256": _sha256_file(run_dir / "run_execution.json") if run_dir is not None else None,
        "report_sha256": _sha256_file(run_dir / "report.md") if run_dir is not None else None,
        "artifacts_sha256": _artifact_tree_hash(run_dir),
        "changed_files_sha256": _sha256_file(run_dir / "git" / "changed_files.json") if run_dir is not None else None,
    }
    return "sha256:" + sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _phase_source_hash(project: Path, paths: WorkflowPaths, phase_title: str, phase_tasks: Sequence[PlanTask]) -> str:
    entries = {
        "phase_title": phase_title,
        "tasks": [
            {
                "task_id": task.task_id,
                "status": task.status,
                "title": task.title,
                "task_summary_sha256": _sha256_file(task_summary_json_path(paths, task.task_id)),
            }
            for task in phase_tasks
        ],
        "objectives": _objective_source_hashes(project, paths, phase_title),
    }
    return "sha256:" + sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _objective_source_hashes(project: Path, paths: WorkflowPaths, phase_title: str) -> list[dict[str, Any]]:
    objectives, parse_errors = parse_plan_objectives(_read_text(paths.plan_file))
    rows: list[dict[str, Any]] = [{"parse_errors": list(parse_errors), "plan_sha256": _sha256_file(paths.plan_file)}]
    phase_id = _phase_id(phase_title)
    for objective in objectives:
        if objective.scope == "phase" and not (objective.phase_title == phase_title or objective.phase_id == phase_id):
            continue
        try:
            from runtime.objective_verification import objective_report_path

            report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id)
        except Exception:
            report_path = paths.runtime_dir / "objectives" / f"{objective.objective_id}.json"
        rows.append(
            {
                "objective_id": objective.objective_id,
                "status": objective.status,
                "report_sha256": _sha256_file(report_path),
                "report_path": _path_for_record(project, report_path),
            }
        )
    return rows


def _objective_closure_rows(
    project: Path,
    paths: WorkflowPaths,
    *,
    phase_title: str | None,
    scope: str,
) -> list[dict[str, str]]:
    objectives, _parse_errors = parse_plan_objectives(_read_text(paths.plan_file))
    phase_id = _phase_id(phase_title) if phase_title else None
    rows: list[dict[str, str]] = []
    try:
        from runtime.objective_verification import objective_report_path, objective_result_is_closed, objective_results_by_id
    except Exception:
        objective_report_path = None  # type: ignore[assignment]
        objective_result_is_closed = lambda result: False  # type: ignore[assignment]
        objective_results_by_id = lambda report: {}  # type: ignore[assignment]
    for objective in objectives:
        if objective.scope != scope:
            continue
        if scope == "phase" and not (objective.phase_title == phase_title or objective.phase_id == phase_id):
            continue
        report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id) if objective_report_path is not None else None
        report = _read_json_object(report_path, default={}) if report_path is not None else {}
        result = objective_results_by_id(report).get(objective.objective_id) if isinstance(report, Mapping) else None
        closed = bool(result and objective_result_is_closed(result)) or objective.status in {"x", "-"}
        verification = str(_mapping(result).get("verdict") or _mapping(result).get("status") or _mapping(report).get("status") or "not verified")
        followup = _objective_followup_label(objective.fields)
        rows.append(
            {
                "Objective": objective.objective_id,
                "Plan": objective.status_label,
                "Verification": verification,
                "Closed": "yes" if closed else "no",
                "Follow-up": followup or str(_mapping(result).get("suggested_followup") or ""),
            }
        )
    return rows


def _objective_followup_label(fields: Mapping[str, Sequence[str]]) -> str:
    values = []
    task_values = fields.get("followup_tasks") or ()
    phase_values = fields.get("followup_phases") or ()
    if task_values:
        values.append("tasks: " + ", ".join(str(value) for value in task_values if str(value)))
    if phase_values:
        values.append("phases: " + ", ".join(str(value) for value in phase_values if str(value)))
    if not values:
        return ""
    return "; ".join(value for value in values if value)


def _latest_path(project: Path, paths: WorkflowPaths, task: PlanTask) -> Path:
    latest_values = task.fields.get("latest") or task.fields.get("latest_pointer_path") or ()
    latest = latest_values[0] if latest_values else f"{paths.value('results_dir').rstrip('/')}/{task.task_id}/latest.json"
    return _path_from_value(project, latest) or (paths.results_dir / task.task_id / "latest.json")


def _tasks_by_phase(tasks: Sequence[PlanTask]) -> dict[str, list[PlanTask]]:
    grouped: dict[str, list[PlanTask]] = {}
    for task in tasks:
        grouped.setdefault(task.phase or "Unphased", []).append(task)
    return grouped


def _phase_id(title: str) -> str:
    parts = title.split(":", 1)[0].split()
    raw = parts[1] if len(parts) > 1 and parts[0] == "Phase" else title
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.strip()).strip("_")
    return normalized or "phase"


def _summary_for_task_id(records: Sequence[Mapping[str, Any]], task_id: str) -> Mapping[str, Any]:
    for record in records:
        if str(record.get("task_id") or "") == task_id:
            return record
    return {}


def _display_task_title(task: PlanTask) -> str:
    return _clean_markdown_text(task.title).rstrip(" .") or task.task_id


def _display_phase_title(title: str) -> str:
    clean = _clean_markdown_text(title).rstrip(" .")
    match = re.match(r"^Phase\s+[^:]+:\s*(?P<title>.+)$", clean)
    if match:
        return match.group("title").strip() or clean
    return clean or "Unphased"


def _leadership_outcome_label(status_label: str) -> str:
    return {
        "done": "accepted project increment",
        "blocked": "strategic confidence constrained",
        "skipped": "intentional scope reduction",
        "partial": "partial strategic signal",
        "pending": "not yet advanced",
    }.get(status_label, status_label or "unknown")


def _phase_outcome_label(*, done: int, blocked: int, skipped: int, total: int) -> str:
    if blocked:
        return "phase not ready for full strategic closure"
    if skipped:
        return "phase closed with explicit scope adjustment"
    if total and done == total:
        return "phase closed as a coherent project increment"
    return "phase has terminal records with limited strategic signal"


def _task_report_title(task: PlanTask, status_label: str) -> str:
    # Plain, honest titles for the mechanical fallback (no grand "Strategic" framing
    # the fallback cannot back up). The agent-written highlight report sets its own.
    title = _display_task_title(task)
    if status_label == "blocked":
        return f"{title}: status note (blocked)"
    if status_label == "skipped":
        return f"{title}: status note (skipped)"
    return f"{title}: status note"


def _phase_report_title(phase_title: str, *, blocked: int, skipped: int) -> str:
    title = _display_phase_title(phase_title)
    if blocked:
        return f"{title}: phase status note (blocked)"
    if skipped:
        return f"{title}: phase status note (scope narrowed)"
    return f"{title}: phase status note"


def _prose_from_items(items: Sequence[str]) -> str:
    cleaned = [_leadership_text(item).rstrip(" .") for item in items if _leadership_text(item)]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0] + "."
    if len(cleaned) == 2:
        return f"{cleaned[0]}. {cleaned[1]}."
    return f"{cleaned[0]}. {cleaned[1]}. Together, these signals show {cleaned[2].lower()}."


def _attention_paragraph(items: Sequence[str]) -> str:
    cleaned = [_leadership_text(item).rstrip(" .") for item in items if _leadership_text(item)]
    if not cleaned:
        return ""
    lead = cleaned[0]
    if len(cleaned) == 1:
        return f"The main leadership reading is that {lead[0].lower() + lead[1:] if lead else lead}."
    return f"The main leadership reading is that {lead[0].lower() + lead[1:] if lead else lead}. A second confidence boundary is that {cleaned[1][0].lower() + cleaned[1][1:] if cleaned[1] else cleaned[1]}."


def _visual_paragraph(visual_evidence: str) -> str:
    visual_evidence = str(visual_evidence or "").strip()
    if not visual_evidence:
        return ""
    return "The visual material below is included because it adds decision-level signal, not because the file itself is the story.\n\n" + visual_evidence


def _join_markdown_blocks(blocks: Sequence[str]) -> str:
    return "\n\n".join(str(block).strip() for block in blocks if str(block or "").strip())


def _leadership_tables(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for name, rows in tables.items():
        clean_rows = [
            {str(key): str(value) for key, value in row.items() if value is not None and str(value).strip()}
            for row in rows
            if isinstance(row, Mapping)
        ]
        clean_rows = [row for row in clean_rows if row]
        if clean_rows:
            result[_safe_metadata_key(name)] = clean_rows
    return result


def _task_executive_summary(
    *,
    task: PlanTask,
    status_label: str,
    validation_status: str,
    validation: Mapping[str, Any],
    agent_status: Mapping[str, Any],
    run_execution: Mapping[str, Any],
    report_text: str,
) -> str:
    task_title = _display_task_title(task)
    # Honest, plain status line. This is the mechanical fallback used only when no
    # summary agent produced the highlight report; it must NOT fabricate editorial
    # "leadership" narrative or claim strategic impact the evidence does not show.
    # The substantive headline comes from the worker's own reported result below,
    # when one is available; otherwise we state the bare, accurate status.
    if status_label == "done" and validation_status in {"pass", "passed", "pass_with_warnings"}:
        outcome = f"{task_title}: completed and validated."
    elif status_label == "blocked":
        outcome = f"{task_title}: blocked and unresolved."
    elif status_label == "skipped":
        outcome = f"{task_title}: skipped (removed from active scope)."
    else:
        outcome = f"{task_title}: recorded with no validated result yet."

    # Prefer the worker's own stated result as the headline; the status line is the
    # lead-in, not a substitute for the actual finding.
    candidates = [
        _first_text(agent_status.get("human_summary_seed")),
        _summary_candidate_text(agent_status),
        *_report_paragraphs(report_text, limit=2),
        _first_text(agent_status.get("summary")),
        _first_text(run_execution.get("message")),
        _first_text(validation.get("summary")),
    ]
    for candidate in candidates:
        clean = _leadership_text(candidate)
        if clean and not _is_low_information_text(clean):
            return _truncate(f"{outcome} {clean}", 900)
    return outcome


def _task_progress_items(
    task: PlanTask,
    report_text: str,
    agent_status: Mapping[str, Any],
    changed_files: Sequence[Mapping[str, Any]],
    artifacts: Sequence[str],
) -> list[str]:
    items: list[str] = []
    for bullet in _report_bullets(report_text, limit=5):
        _append_unique(items, _leadership_text(bullet))
    for paragraph in _report_paragraphs(report_text, limit=4):
        _append_unique(items, _leadership_text(paragraph))
    for highlight in _summary_candidate_highlights(agent_status):
        _append_unique(items, _leadership_text(highlight))

    claims, evidence = _acceptance_claims(agent_status, task.task_id)
    if claims:
        _append_unique(
            items,
            f"The completed slice strengthens confidence in the intended outcome: {_short_join([_leadership_text(item) for item in claims], limit=2)}.",
        )
    deliverables = _task_field_values(task, "deliverables")
    if deliverables:
        _append_unique(
            items,
            f"The planned project asset is now represented by reviewable work: {_short_join([_leadership_text(item) for item in deliverables], limit=2)}.",
        )
    if artifacts:
        _append_unique(items, "The work produced tangible review material, making the progress easier to evaluate and communicate.")
    elif evidence:
        _append_unique(items, "The work now has a clearer evidence basis for leadership review.")
    if changed_files:
        _append_unique(items, "The project moved beyond planning into a concrete implementation-backed increment.")
    if not items:
        items.append(f"{_display_task_title(task)} advanced, but the available report does not yet explain the strategic increment in depth.")
    return items[:6]


def _task_project_meaning(task: PlanTask, status_label: str, validation_status: str) -> str:
    acceptance = _task_field_values(task, "acceptance")
    deliverables = _task_field_values(task, "deliverables")
    target = _short_join([_leadership_text(item) for item in (acceptance or deliverables)], limit=3)
    if status_label == "done" and validation_status in {"pass", "passed", "pass_with_warnings"}:
        if target:
            return (
                f"This gives the project a stronger claim against the intended outcome: {target}. "
                "The meaningful change is not just completion; it is the conversion of a planned promise into something leadership can weigh when deciding what the project is ready to absorb next."
            )
        return (
            f"{_display_task_title(task)} moved from plan to accepted increment. The project now has one more concrete basis for deciding how much confidence, scope, and attention the next phase deserves."
        )
    if status_label == "blocked":
        return (
            f"{_display_task_title(task)} remains a strategic constraint: the project cannot fully treat this area as mature until the unresolved uncertainty is either retired or deliberately removed from scope."
        )
    if status_label == "skipped":
        return (
            f"{_display_task_title(task)} no longer contributes direct project value in this pass. That may be a useful scope decision, but it narrows what the completed phase can represent."
        )
    return f"{_display_task_title(task)} has a recorded state, but the available evidence does not yet support a strong strategic interpretation."


def _task_confidence_items(
    *,
    task: PlanTask,
    validation_status: str,
    validation: Mapping[str, Any],
    agent_status: Mapping[str, Any],
    checks: Sequence[Mapping[str, str]],
    artifacts: Sequence[str],
) -> list[str]:
    items: list[str] = []
    if validation_status in {"pass", "passed"}:
        items.append(f"Validation status is {validation_status}.")
    elif validation_status == "pass_with_warnings":
        items.append("Validation passed with warnings; read the warnings before relying on the output.")
    elif validation_status != "unknown":
        items.append(f"Validation ended with status {validation_status}.")

    if checks:
        status_counts = _status_counts(str(check.get("Status") or "unknown") for check in checks)
        items.append(f"Validation detail: {_format_status_counts(status_counts)} across {len(checks)} recorded check(s).")
        for check in checks[:3]:
            message = _clean_markdown_text(str(check.get("Message") or ""))
            if message and not _is_low_information_text(message):
                _append_unique(items, f"{check.get('Check')}: {message}", limit=260)
    else:
        items.append("No named validation checks were recorded; confidence depends on the overall validation status and evidence pointers.")

    claims, evidence = _acceptance_claims(agent_status, task.task_id)
    if claims:
        _append_unique(items, f"Worker evidence maps back to acceptance criteria: {_short_join(claims, limit=3)}.", limit=260)
    validation_summary = _clean_markdown_text(_first_text(validation.get("summary")))
    if validation_summary and not _is_low_information_text(validation_summary):
        _append_unique(items, validation_summary, limit=260)
    if artifacts:
        _append_unique(items, f"Review evidence includes artifact(s): {_short_join(artifacts, limit=4)}.", limit=260)
    elif evidence:
        _append_unique(items, f"Review evidence was referenced by the worker: {_short_join(evidence, limit=4)}.", limit=260)
    return items[:7]


def _task_attention_items(
    task: PlanTask,
    validation: Mapping[str, Any],
    agent_status: Mapping[str, Any],
    run_execution: Mapping[str, Any],
) -> list[str]:
    items: list[str] = []
    for caveat in _summary_caveats(validation, agent_status, run_execution):
        _append_unique(items, _leadership_text(caveat), limit=260)
    candidate = _mapping(agent_status.get("summary_candidate"))
    for key in ("warnings", "blockers"):
        for value in _list_strings(candidate.get(key)):
            _append_unique(items, _leadership_text(value), limit=260)
    validation_claim = _mapping(agent_status.get("validation_claim"))
    for value in _list_strings(validation_claim.get("limitations")):
        _append_unique(items, _leadership_text(value), limit=260)
    for value in _list_strings(agent_status.get("known_risks")):
        _append_unique(items, _leadership_text(value), limit=260)
    for value in _list_strings(agent_status.get("remaining_incomplete_items")):
        if value and value != task.task_id:
            _append_unique(items, f"Some adjacent scope remains unresolved: {_leadership_text(value)}", limit=260)
    if task.status == "!":
        _append_unique(items, "This part still limits confidence in the broader project story.", limit=260)
    return items[:6]


def _phase_project_meaning(
    *,
    phase_title: str,
    total: int,
    done: int,
    blocked: int,
    skipped: int,
    phase_objective_rows: Sequence[Mapping[str, str]],
    workflow_objective_rows: Sequence[Mapping[str, str]],
) -> str:
    # Honest, plain phase posture for the mechanical fallback (used only when no
    # summary agent produced the highlight report). No fabricated editorial framing.
    title = _display_phase_title(phase_title)
    if blocked:
        posture = f"{title}: {done}/{total} tasks done, with unresolved blockers remaining."
    elif skipped:
        posture = f"{title}: closed with some tasks intentionally left out of scope ({done}/{total} done)."
    elif total and done == total:
        posture = f"{title}: all {total} tasks completed."
    else:
        posture = f"{title}: terminal record with {done}/{total} tasks done."

    objective_rows = [*phase_objective_rows, *workflow_objective_rows]
    if objective_rows:
        closed = sum(1 for row in objective_rows if str(row.get("Closed") or "").lower() == "yes")
        posture += f" Objective gates closed: {closed}/{len(objective_rows)}."
    return posture


def _phase_executive_summary(
    *,
    phase_title: str,
    total: int,
    done: int,
    blocked: int,
    skipped: int,
    progress_items: Sequence[str],
) -> str:
    # Plain factual intro for the mechanical fallback; the substantive headline,
    # when present, comes from the phase's own task results (first_progress).
    title = _display_phase_title(phase_title)
    if blocked:
        intro = f"{title}: {done}/{total} tasks done, blockers unresolved."
    elif skipped:
        intro = f"{title}: closed with scope intentionally narrowed ({done}/{total} done)."
    else:
        intro = f"{title}: {done}/{total} tasks completed."
    first_progress = _clean_markdown_text(progress_items[0]) if progress_items else ""
    if first_progress and not _is_low_information_text(first_progress):
        return _truncate(f"{intro} {first_progress}", 900)
    return intro


def _phase_control_items(total: int, done: int, blocked: int, skipped: int) -> list[str]:
    items = [f"Completion posture: {done}/{total} work item(s) closed, {blocked} constrained, {skipped} removed from scope."]
    if blocked:
        items.append("The phase still carries a confidence boundary that leadership should keep visible.")
    elif skipped:
        items.append("The phase should be read as a deliberate scope choice rather than a full-scope completion claim.")
    else:
        items.append("The phase can be reviewed as a closed unit at the project level.")
    return items


def _validation_checks(validation: Mapping[str, Any]) -> list[dict[str, str]]:
    checks = validation.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
        return []
    rows: list[dict[str, str]] = []
    for check in checks[:12]:
        if not isinstance(check, Mapping):
            continue
        rows.append(
            {
                "Check": _truncate(str(check.get("name") or check.get("type") or "check"), 50),
                "Status": str(check.get("status") or "unknown"),
                "Message": _truncate(str(check.get("message") or check.get("summary") or ""), 120),
            }
        )
    return rows


def _summary_caveats(
    validation: Mapping[str, Any],
    agent_status: Mapping[str, Any],
    run_execution: Mapping[str, Any],
) -> list[str]:
    caveats: list[str] = []
    for container in (validation, agent_status, run_execution):
        for key in ("warnings", "errors", "failures"):
            value = container.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                caveats.extend(_truncate(str(item), 180) for item in value[:5])
    return [item for item in caveats if item]


def _summary_candidate_text(agent_status: Mapping[str, Any]) -> str:
    candidate = agent_status.get("summary_candidate")
    if not isinstance(candidate, Mapping):
        return ""
    one_line = _first_text(candidate.get("one_line"))
    highlights = candidate.get("highlights")
    highlight_text = ""
    if isinstance(highlights, Sequence) and not isinstance(highlights, (str, bytes)):
        highlight_text = " ".join(_truncate(str(item), 120) for item in highlights[:3] if str(item).strip())
    text = " ".join(part for part in (one_line, highlight_text) if part).strip()
    return "" if _is_low_information_text(text) else text


def _summary_candidate_highlights(agent_status: Mapping[str, Any]) -> list[str]:
    candidate = _mapping(agent_status.get("summary_candidate"))
    highlights = candidate.get("highlights")
    if not isinstance(highlights, Sequence) or isinstance(highlights, (str, bytes)):
        return []
    return [
        _clean_markdown_text(str(item))
        for item in highlights
        if _clean_markdown_text(str(item)) and not _is_low_information_text(str(item))
    ]


def _report_paragraphs(text: str, *, limit: int = 4) -> list[str]:
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text or ""):
        raw_block = block.strip()
        if raw_block.startswith("#") and "\n" not in raw_block:
            continue
        if raw_block.startswith("|") or raw_block.startswith("!["):
            continue
        clean = _clean_markdown_text(block)
        if not clean or _is_report_heading(clean) or _is_low_information_text(clean):
            continue
        paragraphs.append(_truncate(clean, TEXT_SECTION_LIMIT))
        if len(paragraphs) >= limit:
            break
    return paragraphs


def _report_bullets(text: str, *, limit: int = 5) -> list[str]:
    bullets: list[str] = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<value>.+?)\s*$", line)
        if not match:
            continue
        clean = _clean_markdown_text(match.group("value"))
        if not clean or _is_low_information_text(clean):
            continue
        bullets.append(_truncate(clean, TEXT_SECTION_LIMIT))
        if len(bullets) >= limit:
            break
    return bullets


def _clean_markdown_text(text: str) -> str:
    clean = str(text or "")
    clean = re.sub(r"```.*?```", " ", clean, flags=re.DOTALL)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    cleaned_lines: list[str] = []
    for line in clean.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line.strip())
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"__([^_]+)__", r"\1", line)
        line = line.replace("*", "").replace("_", " ")
        if line:
            cleaned_lines.append(line)
    return re.sub(r"\s+", " ", " ".join(cleaned_lines)).strip()


def _leadership_text(text: Any) -> str:
    clean = _clean_markdown_text(str(text or ""))
    if not clean:
        return ""
    clean = re.sub(
        r"\b(?:agent_status|run_execution|latest|validation|validator_review|summary_selection|summary_context_manifest)\.json\b",
        "project record",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\b(?:PLAN|PROJECT_BRIEF|SHARED_CONTEXT)\.md\b",
        "project plan",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"(?:^|\s)(?:\.?[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",
        " project asset",
        clean,
    )
    clean = re.sub(
        r"\b[A-Za-z0-9_.-]+\.(?:py|json|md|txt|svg|png|jpg|jpeg|gif|webp|patch|log|sh|toml|yaml|yml)\b",
        "project asset",
        clean,
        flags=re.IGNORECASE,
    )
    replacements = (
        (r"\bworker\b", "implementation work"),
        (r"\bvalidator\b", "review"),
        (r"\bvalidation\b", "review confidence"),
        (r"\badapter\b", "execution"),
        (r"\bruntime\b", "project record"),
        (r"\bchanged files?\b", "project change"),
        (r"\brun folder\b", "project record"),
        (r"\blatest pointer\b", "project record"),
    )
    for pattern, replacement in replacements:
        clean = re.sub(pattern, replacement, clean, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", clean).strip()


def _is_report_heading(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return normalized in {"report", "worker report", "summary", "task summary", "final report"}


def _is_low_information_text(text: str) -> bool:
    clean = _clean_markdown_text(text)
    if not clean:
        return True
    normalized = re.sub(r"\s+", " ", clean.lower()).strip(" .")
    if _is_report_heading(normalized):
        return True
    if any(phrase in normalized for phrase in GENERIC_SUMMARY_PHRASES):
        return True
    words = re.findall(r"[a-z0-9]+", normalized)
    if len(words) <= 5 and any(word in {"done", "complete", "completed", "pass", "passed"} for word in words):
        return True
    return False


def _append_unique(items: list[str], value: Any, *, limit: int = TEXT_SECTION_LIMIT) -> None:
    clean = _clean_markdown_text(str(value or ""))
    if not clean or _is_low_information_text(clean):
        return
    normalized = clean.lower()
    existing = {item.lower() for item in items}
    if normalized in existing:
        return
    if any(normalized in item.lower() or item.lower() in normalized for item in items):
        return
    items.append(_truncate(clean, limit))


def _task_field_values(task: PlanTask, key: str) -> list[str]:
    return [_clean_markdown_text(value) for value in task.fields.get(key, ()) if _clean_markdown_text(value)]


def _acceptance_claims(agent_status: Mapping[str, Any], task_id: str) -> tuple[list[str], list[str]]:
    claims: list[str] = []
    evidence: list[str] = []
    satisfies = agent_status.get("evidence_satisfies")
    if not isinstance(satisfies, Sequence) or isinstance(satisfies, (str, bytes)):
        return claims, evidence
    for item in satisfies:
        if not isinstance(item, Mapping):
            continue
        item_task_id = str(item.get("task_id") or "")
        relationship = str(item.get("relationship") or "")
        if item_task_id and item_task_id != task_id and relationship != "primary":
            continue
        for value in _list_strings(item.get("acceptance_claimed")):
            _append_unique(claims, value, limit=180)
        for value in _list_strings(item.get("evidence")):
            _append_unique(evidence, value, limit=180)
    return claims, evidence


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _short_join(items: Sequence[str], *, limit: int = 3) -> str:
    clean_items = []
    for item in items:
        clean = _clean_markdown_text(item).rstrip(" .;")
        if clean:
            clean_items.append(clean)
    shown = clean_items[:limit]
    text = "; ".join(shown)
    remaining = len(clean_items) - len(shown)
    if remaining > 0:
        text += f"; plus {remaining} more"
    return text


def _status_counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _format_status_counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "none"


def _read_changed_files(run_dir: Path | None) -> list[Mapping[str, Any]]:
    if run_dir is None:
        return []
    data = _read_json_object(run_dir / "git" / "changed_files.json", default={})
    changed = data.get("changed_files") if isinstance(data, Mapping) else None
    if not isinstance(changed, Sequence) or isinstance(changed, (str, bytes)):
        return []
    return [item for item in changed if isinstance(item, Mapping)]


def _artifact_list(run_dir: Path | None) -> list[str]:
    artifacts_dir = run_dir / "artifacts" if run_dir is not None else None
    artifacts = _artifact_paths(run_dir)
    labels: list[str] = []
    for path in artifacts[:12]:
        if artifacts_dir is not None:
            try:
                labels.append(path.relative_to(artifacts_dir).as_posix())
                continue
            except ValueError:
                pass
        labels.append(path.name)
    return labels


def _artifact_paths(run_dir: Path | None) -> list[Path]:
    if run_dir is None:
        return []
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.exists():
        return []
    discovery = discover_files_bounded(
        (artifacts_dir,),
        max_entries=SUMMARY_ARTIFACT_SCAN_ENTRY_LIMIT,
        max_matches=SUMMARY_ARTIFACT_SCAN_LIMIT,
        max_depth=8,
    )
    return list(discovery.paths)


def _visual_evidence_markdown(project: Path, run_dir: Path | None) -> str:
    figures = [path for path in _artifact_paths(run_dir) if path.suffix.lower() in VISUAL_ARTIFACT_SUFFIXES]
    lines: list[str] = []
    rows: list[dict[str, str]] = []
    selected: list[tuple[str, str]] = []
    for index, path in enumerate(figures[:6], start=1):
        href = _path_for_record(project, path)
        if not href:
            continue
        label = f"Project visual {index}"
        target = _markdown_link_target(href)
        safe_label = _markdown_link_label(label)
        rows.append({"Figure": safe_label, "Open": f"[Open visual]({target})"})
        selected.append((label, target))
    if rows:
        lines.append(_markdown_table(("Figure", "Open"), rows))
        lines.append("")
    for label, target in selected:
        safe_label = _markdown_link_label(label)
        lines.append(f"![{safe_label}]({target} \"{safe_label}\")")
        lines.append(f"[Open full-size visual]({target})")
        lines.append("")
    return "\n".join(lines).strip()


def _artifact_tree_hash(run_dir: Path | None) -> str | None:
    if run_dir is None:
        return None
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return None
    # Prefer an artifact producer's manifest.  Hashing one manifest preserves
    # content-addressed freshness without rereading every model/cache shard.
    for manifest_path in (run_dir / "artifact_manifest.json", artifacts_dir / "manifest.json"):
        if manifest_path.is_file():
            return _sha256_file(manifest_path)
    discovery = discover_files_bounded(
        (artifacts_dir,),
        max_entries=SUMMARY_ARTIFACT_SCAN_ENTRY_LIMIT,
        max_matches=SUMMARY_ARTIFACT_SCAN_LIMIT,
        max_depth=8,
    )
    entries: list[dict[str, Any]] = []
    for path in discovery.paths:
        try:
            stat_result = path.stat()
            relative = path.relative_to(artifacts_dir).as_posix()
        except (OSError, ValueError):
            continue
        entries.append(
            {
                "path": relative,
                "size_bytes": int(stat_result.st_size),
                "mtime_ns": int(stat_result.st_mtime_ns),
            }
        )
    try:
        root_mtime_ns = int(artifacts_dir.stat().st_mtime_ns)
    except OSError:
        root_mtime_ns = 0
    if not entries and not discovery.truncated:
        return None
    payload = {
        "entries": entries,
        "root_mtime_ns": root_mtime_ns,
        "scanned_entries": discovery.scanned_entries,
        "truncated": discovery.truncated,
        "limit_reason": discovery.limit_reason,
    }
    return "sha256:" + sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _line_delta(item: Mapping[str, Any]) -> str:
    added = _optional_int(item.get("lines_added"))
    deleted = _optional_int(item.get("lines_deleted"))
    if added is None and deleted is None:
        return ""
    return f"+{added or 0}/-{deleted or 0}"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _human_task_status(status: str) -> str:
    return {"x": "done", "!": "blocked", "-": "skipped", "~": "partial", " ": "pending"}.get(status, "unknown")


def _markdown_table(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_markdown_cell(row.get(column)) for column in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, divider, *body])


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _markdown_link_label(value: str) -> str:
    return str(value or "").replace("[", "\\[").replace("]", "\\]").replace("|", "\\|").strip()


def _markdown_link_target(value: str) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    if re.search(r"\s", target):
        return f"<{target}>"
    return target


def _safe_summary_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    parts = PurePosixPath(normalized).parts
    if ".git" in parts or any(part in {"", ".", ".."} for part in parts):
        return "[redacted path]"
    return normalized


def _bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _excerpt(text: str, limit: int = 220) -> str:
    return _truncate(re.sub(r"\s+", " ", text).strip(), limit)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _path_from_value(project: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else project / value


def _path_for_record(project: Path | None, path: Path | None) -> str | None:
    if path is None:
        return None
    if project is not None:
        try:
            return path.resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


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


def _sha256_file(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _failure(project: Path, started_at: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": "failed",
        "project_root": project.as_posix(),
        "workflow_id": None,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "task_results": [],
        "phase_results": [],
        "errors": [message],
        "warnings": [],
    }
