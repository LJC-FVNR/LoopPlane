from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import utc_timestamp
from runtime.path_resolution import WORKFLOW_PATH_FIELDS, WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import is_task_block_terminator
from runtime.source_guard import read_process_template


SCHEMA_VERSION = "1.5"
PREVIOUS_FAILURE_PROMPT_LIMIT = 2
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
WORKER_TEMPLATE_PATH = TEMPLATE_DIR / "worker_prompt.template.md"
RECOVERY_TEMPLATE_PATH = TEMPLATE_DIR / "recovery_prompt.template.md"
EXPANSION_TEMPLATE_PATH = TEMPLATE_DIR / "expansion_planner_prompt.template.md"
OBJECTIVE_VERIFIER_TEMPLATE_PATH = TEMPLATE_DIR / "objective_verifier_prompt.template.md"
SUMMARY_TEMPLATE_PATH = TEMPLATE_DIR / "summary_prompt.template.md"
PROMPT_METADATA_FILENAME = "prompt_metadata.json"
PROMPT_CONTEXT_MANIFEST_FILENAME = "prompt_context_manifest.json"
TASK_LINE_RE = re.compile(r"^- \[(?P<status>[ x~!\-])\]\s+(?P<task_id>[A-Za-z0-9_.-]+):\s+.+?\s*$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


class PromptBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuiltPrompt:
    workflow_id: str
    run_id: str
    role: str
    runner_id: str
    task_id: str
    prompt_path: Path
    metadata_path: Path
    template_path: Path
    content: str
    task_block: str
    previous_failures: str
    failure_summary: str

    def to_dict(self, *, project_root: Path | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "role": self.role,
            "runner_id": self.runner_id,
            "task_id": self.task_id,
            "prompt_path": _path_for_record(project_root, self.prompt_path),
            "metadata_path": _path_for_record(project_root, self.metadata_path),
            "template_path": _path_for_record(project_root, self.template_path),
            "task_block_sha256": _sha256_text(self.task_block),
            "previous_failures_sha256": _sha256_text(self.previous_failures),
            "failure_summary_sha256": _sha256_text(self.failure_summary),
        }


def build_prompt_for_prepared_run(
    project_root: Path | str,
    prepared_run: Any,
    *,
    failure_id: str | None = None,
    write: bool = True,
) -> BuiltPrompt:
    project = Path(project_root).expanduser().resolve()
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        raise PromptBuildError(f"Unable to load workflow configuration: {error}") from error

    workflow_id = _string_attr(prepared_run, "workflow_id")
    run_id = _string_attr(prepared_run, "run_id")
    role = _string_attr(prepared_run, "role")
    runner_id = _string_attr(prepared_run, "runner_id")
    task_id = _string_attr(prepared_run, "task_id")
    if role == "expansion_planner":
        return _build_expansion_prompt(
            project,
            paths=paths,
            prepared_run=prepared_run,
            workflow_id=workflow_id,
            run_id=run_id,
            role=role,
            runner_id=runner_id,
            write=write,
        )
    if role == "objective_verifier":
        return _build_objective_verifier_prompt(
            project,
            paths=paths,
            prepared_run=prepared_run,
            workflow_id=workflow_id,
            run_id=run_id,
            role=role,
            runner_id=runner_id,
            write=write,
        )
    if role == "summary":
        return _build_summary_prompt(
            project,
            paths=paths,
            prepared_run=prepared_run,
            workflow_id=workflow_id,
            run_id=run_id,
            role=role,
            runner_id=runner_id,
            write=write,
        )
    if not task_id:
        raise PromptBuildError(f"task_id is required for {role or 'worker'} prompt generation")
    if role not in {"worker", "recovery_worker"}:
        raise PromptBuildError(f"unsupported prompt role: {role}")

    template_path = RECOVERY_TEMPLATE_PATH if role == "recovery_worker" else WORKER_TEMPLATE_PATH
    plan_text = _read_required_text(paths.plan_file, "PLAN.md")
    shared_context_text = _read_required_text(paths.shared_context_file, "shared context")
    task_block = extract_task_block(plan_text, task_id=task_id)
    failure_records = _failure_records(paths.runtime_dir / "failure_registry.json", task_id=task_id)
    selected_failure = _selected_failure(failure_records, failure_id=failure_id) if role == "recovery_worker" else None
    previous_failures = format_previous_failures(failure_records, task_id=task_id)
    failure_summary = format_failure_summary(selected_failure, failure_id=failure_id)

    prompt_path = Path(_path_attr(prepared_run, "prompt_path"))
    scheduler_run_dir = Path(_path_attr(prepared_run, "scheduler_run_dir"))
    metadata_path = scheduler_run_dir / PROMPT_METADATA_FILENAME
    context_manifest_path = scheduler_run_dir / PROMPT_CONTEXT_MANIFEST_FILENAME
    variables = _prompt_variables(
        project=project,
        paths=paths,
        prepared_run=prepared_run,
        task_id=task_id,
        task_block=task_block,
        previous_failures=previous_failures,
        failure_summary=failure_summary,
        failure_id=str(failure_id or (selected_failure or {}).get("failure_id") or ""),
        context_manifest_path=context_manifest_path,
    )
    template = _read_required_text(template_path, "prompt template")
    content = render_template(template, variables)

    built = BuiltPrompt(
        workflow_id=workflow_id,
        run_id=run_id,
        role=role,
        runner_id=runner_id,
        task_id=task_id,
        prompt_path=prompt_path,
        metadata_path=metadata_path,
        template_path=template_path,
        content=content,
        task_block=task_block,
        previous_failures=previous_failures,
        failure_summary=failure_summary,
    )
    if write:
        _atomic_write_text(prompt_path, content)
        context_manifest = _prompt_context_manifest(
            project=project,
            paths=paths,
            prepared_run=prepared_run,
            built=built,
            manifest_path=context_manifest_path,
            plan_text=plan_text,
            shared_context_text=shared_context_text,
            failure_records=failure_records,
            selected_failure=selected_failure,
        )
        _atomic_write_json(context_manifest_path, context_manifest)
        _atomic_write_json(
            metadata_path,
            _prompt_metadata(
                project=project,
                paths=paths,
                prepared_run=prepared_run,
                built=built,
                context_manifest_path=context_manifest_path,
                plan_text=plan_text,
                shared_context_text=shared_context_text,
                failure_records=failure_records,
                selected_failure=selected_failure,
            ),
        )
    return built


def _build_summary_prompt(
    project: Path,
    *,
    paths: WorkflowPaths,
    prepared_run: Any,
    workflow_id: str,
    run_id: str,
    role: str,
    runner_id: str,
    write: bool,
) -> BuiltPrompt:
    try:
        from runtime.human_summaries import build_summary_prompt_variables

        variables = build_summary_prompt_variables(project, prepared_run)
    except Exception as error:
        raise PromptBuildError(f"Unable to build summary prompt variables: {error}") from error
    template = _read_required_text(SUMMARY_TEMPLATE_PATH, "summary prompt template")
    content = render_template(template, {key: str(value) for key, value in variables.items()})
    prompt_path = Path(_path_attr(prepared_run, "prompt_path"))
    metadata_path = Path(_path_attr(prepared_run, "scheduler_run_dir")) / PROMPT_METADATA_FILENAME
    built = BuiltPrompt(
        workflow_id=workflow_id,
        run_id=run_id,
        role=role,
        runner_id=runner_id,
        task_id=str(variables.get("target_id") or ""),
        prompt_path=prompt_path,
        metadata_path=metadata_path,
        template_path=SUMMARY_TEMPLATE_PATH,
        content=content,
        task_block="",
        previous_failures="",
        failure_summary="",
    )
    if write:
        _atomic_write_text(prompt_path, content)
        _atomic_write_json(
            metadata_path,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_timestamp(),
                "source_authority": "derived_summary_prompt_not_source_of_truth",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "node_id": _string_attr(prepared_run, "node_id"),
                "role": role,
                "runner_id": runner_id,
                "target_kind": variables.get("target_kind"),
                "target_id": variables.get("target_id"),
                "prompt_path": _path_for_record(project, prompt_path),
                "template_path": _path_for_record(project, SUMMARY_TEMPLATE_PATH),
                "summary_markdown_path": variables.get("summary_markdown_path"),
                "summary_json_path": variables.get("summary_json_path"),
                "configured_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
                "run_paths": {
                    "scheduler_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "scheduler_run_dir")),
                    "role_output_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "role_output_dir")),
                    "prompt_path": _project_relative(paths.project_root, _path_attr(prepared_run, "prompt_path")),
                    "stdout_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stdout_path")),
                    "stderr_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stderr_path")),
                    "final_output_path": _project_relative(paths.project_root, _path_attr(prepared_run, "final_output_path")),
                    "adapter_result_path": _project_relative(paths.project_root, _path_attr(prepared_run, "adapter_result_path")),
                    "active_run_lease_path": _project_relative(paths.project_root, _path_attr(prepared_run, "active_run_lease_path")),
                },
            },
        )
    return built


def _build_objective_verifier_prompt(
    project: Path,
    *,
    paths: WorkflowPaths,
    prepared_run: Any,
    workflow_id: str,
    run_id: str,
    role: str,
    runner_id: str,
    write: bool,
) -> BuiltPrompt:
    try:
        from runtime.objective_verification import build_objective_verifier_prompt_variables

        variables = build_objective_verifier_prompt_variables(project, prepared_run)
    except Exception as error:
        raise PromptBuildError(f"Unable to build objective verifier variables: {error}") from error
    template = _read_required_text(OBJECTIVE_VERIFIER_TEMPLATE_PATH, "objective verifier prompt template")
    content = render_template(template, {key: str(value) for key, value in variables.items()})
    prompt_path = Path(_path_attr(prepared_run, "prompt_path"))
    metadata_path = Path(_path_attr(prepared_run, "scheduler_run_dir")) / PROMPT_METADATA_FILENAME
    built = BuiltPrompt(
        workflow_id=workflow_id,
        run_id=run_id,
        role=role,
        runner_id=runner_id,
        task_id="",
        prompt_path=prompt_path,
        metadata_path=metadata_path,
        template_path=OBJECTIVE_VERIFIER_TEMPLATE_PATH,
        content=content,
        task_block="",
        previous_failures="",
        failure_summary="",
    )
    if write:
        _atomic_write_text(prompt_path, content)
        _atomic_write_json(
            metadata_path,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_timestamp(),
                "source_authority": "derived_objective_verifier_prompt_not_source_of_truth",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "node_id": _string_attr(prepared_run, "node_id"),
                "role": role,
                "runner_id": runner_id,
                "task_id": None,
                "prompt_path": _path_for_record(project, prompt_path),
                "template_path": _path_for_record(project, OBJECTIVE_VERIFIER_TEMPLATE_PATH),
                "configured_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
                "run_paths": {
                    "scheduler_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "scheduler_run_dir")),
                    "role_output_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "role_output_dir")),
                    "prompt_path": _project_relative(paths.project_root, _path_attr(prepared_run, "prompt_path")),
                    "stdout_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stdout_path")),
                    "stderr_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stderr_path")),
                    "final_output_path": _project_relative(paths.project_root, _path_attr(prepared_run, "final_output_path")),
                    "adapter_result_path": _project_relative(paths.project_root, _path_attr(prepared_run, "adapter_result_path")),
                    "active_run_lease_path": _project_relative(paths.project_root, _path_attr(prepared_run, "active_run_lease_path")),
                },
                "objective_scope": variables.get("objective_scope"),
                "objective_phase_id": variables.get("objective_phase_id"),
                "objective_verification_report_path": variables.get("objective_verification_report_path"),
            },
        )
    return built


def _build_expansion_prompt(
    project: Path,
    *,
    paths: WorkflowPaths,
    prepared_run: Any,
    workflow_id: str,
    run_id: str,
    role: str,
    runner_id: str,
    write: bool,
) -> BuiltPrompt:
    try:
        from runtime.self_expansion import build_expansion_prompt_variables

        variables = build_expansion_prompt_variables(project, prepared_run)
    except Exception as error:
        raise PromptBuildError(f"Unable to build expansion planner variables: {error}") from error
    template = _read_required_text(EXPANSION_TEMPLATE_PATH, "expansion planner prompt template")
    content = render_template(template, {key: str(value) for key, value in variables.items()})
    prompt_path = Path(_path_attr(prepared_run, "prompt_path"))
    metadata_path = Path(_path_attr(prepared_run, "scheduler_run_dir")) / PROMPT_METADATA_FILENAME
    built = BuiltPrompt(
        workflow_id=workflow_id,
        run_id=run_id,
        role=role,
        runner_id=runner_id,
        task_id="",
        prompt_path=prompt_path,
        metadata_path=metadata_path,
        template_path=EXPANSION_TEMPLATE_PATH,
        content=content,
        task_block="",
        previous_failures="",
        failure_summary="",
    )
    if write:
        _atomic_write_text(prompt_path, content)
        _atomic_write_json(
            metadata_path,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": utc_timestamp(),
                "source_authority": "derived_expansion_prompt_not_source_of_truth",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "node_id": _string_attr(prepared_run, "node_id"),
                "role": role,
                "runner_id": runner_id,
                "task_id": None,
                "prompt_path": _path_for_record(project, prompt_path),
                "template_path": _path_for_record(project, EXPANSION_TEMPLATE_PATH),
                "configured_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
                "run_paths": {
                    "scheduler_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "scheduler_run_dir")),
                    "role_output_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "role_output_dir")),
                    "prompt_path": _project_relative(paths.project_root, _path_attr(prepared_run, "prompt_path")),
                    "stdout_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stdout_path")),
                    "stderr_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stderr_path")),
                    "final_output_path": _project_relative(paths.project_root, _path_attr(prepared_run, "final_output_path")),
                    "adapter_result_path": _project_relative(paths.project_root, _path_attr(prepared_run, "adapter_result_path")),
                    "active_run_lease_path": _project_relative(paths.project_root, _path_attr(prepared_run, "active_run_lease_path")),
                },
                "expansion_proposal_path": variables.get("expansion_proposal_path"),
                "plan_patch_path": variables.get("plan_patch_path"),
                "expansion_report_path": variables.get("expansion_report_path"),
            },
        )
    return built


def extract_task_block(plan_text: str, *, task_id: str) -> str:
    lines = plan_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = TASK_LINE_RE.match(line)
        if match and match.group("task_id") == task_id:
            start = index
            break
    if start is None:
        raise PromptBuildError(f"task block {task_id!r} was not found in PLAN.md")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if TASK_LINE_RE.match(lines[index]) or is_task_block_terminator(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end]).rstrip()


def render_template(template: str, variables: Mapping[str, str]) -> str:
    missing = sorted({match.group(1) for match in PLACEHOLDER_RE.finditer(template) if match.group(1) not in variables})
    if missing:
        raise PromptBuildError(f"prompt template has unresolved variables: {', '.join(missing)}")
    return PLACEHOLDER_RE.sub(lambda match: variables[match.group(1)], template)


def format_previous_failures(failures: Sequence[Mapping[str, Any]], *, task_id: str) -> str:
    if not failures:
        return f"No previous failures recorded for task {task_id}."
    selected = list(failures[:PREVIOUS_FAILURE_PROMPT_LIMIT])
    header = f"Showing {len(selected)} of {len(failures)} previous failure(s) for task {task_id}."
    if len(failures) > len(selected):
        header += " Read the failure registry for full history before making recovery decisions."
    return "\n\n".join([header, *[f"- {_format_compact_failure_lines(failure, indent='  ')}" for failure in selected]])


def format_failure_summary(failure: Mapping[str, Any] | None, *, failure_id: str | None = None) -> str:
    if failure is None:
        suffix = f" for {failure_id}" if failure_id else ""
        return f"No selected recovery failure was found{suffix}."
    return _format_failure_lines(failure, indent="")


def _prompt_variables(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared_run: Any,
    task_id: str,
    task_block: str,
    previous_failures: str,
    failure_summary: str,
    failure_id: str,
    context_manifest_path: Path,
) -> dict[str, str]:
    values = {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS}
    run_paths = {
        "workflow_id": _string_attr(prepared_run, "workflow_id"),
        "run_id": _string_attr(prepared_run, "run_id"),
        "node_id": _string_attr(prepared_run, "node_id"),
        "role": _string_attr(prepared_run, "role"),
        "runner_id": _string_attr(prepared_run, "runner_id"),
        "task_id": task_id,
        "failure_id": failure_id or "none",
        "scheduler_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "scheduler_run_dir")),
        "role_output_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "role_output_dir")),
        "task_evidence_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "task_evidence_run_dir")),
        "prompt_path": _project_relative(paths.project_root, _path_attr(prepared_run, "prompt_path")),
        "stdout_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stdout_path")),
        "stderr_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stderr_path")),
        "final_output_path": _project_relative(paths.project_root, _path_attr(prepared_run, "final_output_path")),
        "adapter_result_path": _project_relative(paths.project_root, _path_attr(prepared_run, "adapter_result_path")),
        "active_run_lease_path": _project_relative(paths.project_root, _path_attr(prepared_run, "active_run_lease_path")),
        "failure_registry_file": f"{paths.value('runtime_dir')}/failure_registry.json",
        "task_evidence_root": f"{paths.value('results_dir').rstrip('/')}/{task_id}",
        "schema_version": SCHEMA_VERSION,
        "task_block": task_block,
        "previous_failures": previous_failures,
        "failure_summary": failure_summary,
        "context_manifest_path": _project_relative(project, context_manifest_path),
    }
    return {**values, **run_paths}


def _prompt_context_manifest(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared_run: Any,
    built: BuiltPrompt,
    manifest_path: Path,
    plan_text: str,
    shared_context_text: str,
    failure_records: Sequence[Mapping[str, Any]],
    selected_failure: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "source_authority": "context_manifest_not_source_of_truth",
        "workflow_id": built.workflow_id,
        "run_id": built.run_id,
        "node_id": _string_attr(prepared_run, "node_id"),
        "role": built.role,
        "runner_id": built.runner_id,
        "task_id": built.task_id,
        "manifest_path": _path_for_record(project, manifest_path),
        "configured_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
        "run_paths": _prompt_run_paths(paths, prepared_run),
        "plan_file": paths.value("plan_file"),
        "plan_sha256": _sha256_text(plan_text),
        "shared_context_file": paths.value("shared_context_file"),
        "shared_context_sha256": _sha256_text(shared_context_text),
        "failure_registry_file": f"{paths.value('runtime_dir')}/failure_registry.json",
        "task_block_sha256": _sha256_text(built.task_block),
        "target_task_block": built.task_block,
        "depends_on": _task_field_values(built.task_block, "depends_on"),
        "evidence_root": _first_task_field_value(built.task_block, "evidence"),
        "latest_pointer": _first_task_field_value(built.task_block, "latest"),
        "validation_strategy": _first_task_field_value(built.task_block, "validation"),
        "risk": _first_task_field_value(built.task_block, "risk"),
        "approval": _first_task_field_value(built.task_block, "approval"),
        "previous_failure_count": len(failure_records),
        "previous_failure_ids": [str(failure.get("failure_id") or failure.get("id") or "") for failure in failure_records],
        "selected_failure_id": str((selected_failure or {}).get("failure_id") or ""),
        "read_guidance": [
            "Use this manifest, the target task block, and existing task evidence before reading broad project context.",
            "Read the full plan only when dependencies, objective scope, or unclear acceptance criteria require it.",
        ],
        "output_contract": _worker_output_contract(),
    }


def _first_task_field_value(task_block: str, field: str) -> str:
    values = _task_field_values(task_block, field)
    return values[0] if values else ""


def _task_field_values(task_block: str, field: str) -> list[str]:
    prefix = f"  - {field}:"
    values: list[str] = []
    for line in task_block.splitlines():
        if line.startswith(prefix):
            values.append(line[len(prefix):].strip())
    return values


def _prompt_metadata(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared_run: Any,
    built: BuiltPrompt,
    context_manifest_path: Path,
    plan_text: str,
    shared_context_text: str,
    failure_records: Sequence[Mapping[str, Any]],
    selected_failure: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "source_authority": "derived_prompt_not_source_of_truth",
        "workflow_id": built.workflow_id,
        "run_id": built.run_id,
        "node_id": _string_attr(prepared_run, "node_id"),
        "role": built.role,
        "runner_id": built.runner_id,
        "task_id": built.task_id,
        "prompt_path": _path_for_record(project, built.prompt_path),
        "template_path": _path_for_record(project, built.template_path),
        "context_manifest_path": _path_for_record(project, context_manifest_path),
        "plan_file": paths.value("plan_file"),
        "plan_sha256": _sha256_text(plan_text),
        "shared_context_file": paths.value("shared_context_file"),
        "shared_context_sha256": _sha256_text(shared_context_text),
        "task_block_sha256": _sha256_text(built.task_block),
        "previous_failure_count": len(failure_records),
        "previous_failure_ids": [str(failure.get("failure_id") or failure.get("id") or "") for failure in failure_records],
        "selected_failure_id": str((selected_failure or {}).get("failure_id") or ""),
        "configured_paths": {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS},
        "run_paths": _prompt_run_paths(paths, prepared_run),
    }


def _prompt_run_paths(paths: WorkflowPaths, prepared_run: Any) -> dict[str, str]:
    return {
        "scheduler_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "scheduler_run_dir")),
        "role_output_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "role_output_dir")),
        "task_evidence_run_dir": _project_relative(paths.project_root, _path_attr(prepared_run, "task_evidence_run_dir")),
        "prompt_path": _project_relative(paths.project_root, _path_attr(prepared_run, "prompt_path")),
        "stdout_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stdout_path")),
        "stderr_path": _project_relative(paths.project_root, _path_attr(prepared_run, "stderr_path")),
        "final_output_path": _project_relative(paths.project_root, _path_attr(prepared_run, "final_output_path")),
        "adapter_result_path": _project_relative(paths.project_root, _path_attr(prepared_run, "adapter_result_path")),
        "active_run_lease_path": _project_relative(paths.project_root, _path_attr(prepared_run, "active_run_lease_path")),
    }


def _worker_output_contract() -> dict[str, Any]:
    return {
        "agent_status_schema_version": SCHEMA_VERSION,
        "canonical_statuses": [
            "completed",
            "completed_with_warnings",
            "satisfied",
            "running_background",
            "recoverable_failed",
            "blocked_external",
            "blocked_needs_human",
            "blocked_by_scope",
            "failed_agent",
            "failed_system",
            "aborted",
        ],
        "required_outputs": ["metadata.json", "report.md", "agent_status.json", "commands.sh"],
        "agent_status_required_fields": [
            "schema_version",
            "run_id",
            "task_id",
            "status",
            "validation_claim",
            "evidence_satisfies",
            "summary_candidate",
        ],
        "command_evidence_guidance": [
            "For command_exit_code validation, record cmd, exit_code, purpose, and validation_check when useful.",
            "For stdout/stderr validation, record stdout/stderr text or stdout_path/stderr_path.",
        ],
        "report_guidance": (
            "Write report.md as detailed handoff evidence for future agents and validators; "
            "the dashboard human summary is generated separately."
        ),
    }


def _failure_records(path: Path, *, task_id: str) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    failures = data.get("failures", []) if isinstance(data, Mapping) else []
    if not isinstance(failures, list):
        return []
    records = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("task_id") or "") != task_id:
            continue
        records.append(dict(failure))
    return records


def _selected_failure(failures: Sequence[Mapping[str, Any]], *, failure_id: str | None) -> Mapping[str, Any] | None:
    if failure_id:
        for failure in failures:
            if str(failure.get("failure_id") or failure.get("id") or "") == failure_id:
                return failure
        return None
    return failures[0] if failures else None


def _format_failure_lines(failure: Mapping[str, Any], *, indent: str) -> str:
    fields = (
        "failure_id",
        "status",
        "task_id",
        "failure_class",
        "failure_signature",
        "first_seen_at",
        "last_seen_at",
        "run_id",
        "attempts",
        "recovery_attempts",
        "max_recovery_attempts",
        "budget_remaining",
        "last_recovery_status",
        "last_recovery_failure_signature",
        "needs_human_reason",
        "exhausted_reason",
        "created_at",
        "signature",
        "summary",
        "message",
        "agent_status_path",
        "source_validation_path",
        "validation_path",
    )
    lines: list[str] = []
    for field in fields:
        value = failure.get(field)
        if value is None or value == "":
            continue
        lines.append(f"{field}: {value}")
    if not lines:
        lines.append(json.dumps(dict(failure), sort_keys=True))
    return ("\n" + indent).join(lines)


def _format_compact_failure_lines(failure: Mapping[str, Any], *, indent: str) -> str:
    fields = (
        "failure_id",
        "status",
        "failure_signature",
        "last_seen_at",
        "run_id",
        "recovery_attempts",
        "budget_remaining",
        "summary",
        "message",
    )
    lines: list[str] = []
    for field in fields:
        value = failure.get(field)
        if value is None or value == "":
            continue
        lines.append(f"{field}: {value}")
    if not lines:
        lines.append(json.dumps(dict(failure), sort_keys=True))
    return ("\n" + indent).join(lines)


def _string_attr(value: Any, name: str) -> str:
    raw = getattr(value, name, None)
    if raw is None:
        return ""
    return str(raw)


def _path_attr(value: Any, name: str) -> Path:
    raw = getattr(value, name, None)
    if raw is None:
        raise PromptBuildError(f"prepared run is missing {name}")
    return Path(raw)


def _read_required_text(path: Path, label: str) -> str:
    try:
        if _is_runtime_template(path):
            return read_process_template(path)
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptBuildError(f"Unable to read {label} at {path}: {error}") from error


def _is_runtime_template(path: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(TEMPLATE_DIR.resolve())
    except ValueError:
        return False
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(dict(data), indent=2, sort_keys=True) + "\n")


def _sha256_text(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _project_relative(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _path_for_record(project_root: Path | None, path: Path) -> str:
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()
