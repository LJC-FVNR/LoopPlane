from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.adapters.base import ADAPTER_INPUT_FILENAME, AdapterContractError, AdapterInput, utc_timestamp
from runtime.adapters.registry import AdapterLookupError, get_adapter
from runtime.agent_runners import AgentRunnerConfigError, RunnerConfig, load_agent_runners
from runtime.file_discovery import discover_files_bounded
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.reconciliation import parse_plan_tasks
from runtime.scheduler import SCHEMA_VERSION, SchedulerError, append_event, load_event_log_projection, prepare_run
from runtime.source_guard import read_process_template


CHAT_REQUESTS_FILENAME = "chat_requests.jsonl"
CHAT_RESPONSES_FILENAME = "chat_responses.jsonl"
CHANGE_REQUESTS_FILENAME = "change_requests.jsonl"
INSPECTION_MODE = "agent_inspection"
PROHIBITED_ACTIONS: tuple[str, ...] = ()
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
INSPECTOR_TEMPLATE_PATH = TEMPLATE_DIR / "inspector_prompt.template.md"
READ_MODEL_JSON_FILES = (
    "workflow_status.json",
    "plan_index.json",
    "metrics.json",
    "version_control_status.json",
)
READ_MODEL_JSONL_FILES = (
    "dashboard_feed.jsonl",
    "run_summaries.jsonl",
)


def answer_inspection(
    project_root: Path | str,
    user_message: str,
    *,
    runner_id: str = "inspector",
    allowed_paths: Sequence[str] | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    message = str(user_message or "").strip()
    if not message:
        return _failure(
            project=project,
            workflow_id=None,
            started_at=started_at,
            status="invalid_request",
            message="Inspection question is required.",
        )

    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _failure(
            project=project,
            workflow_id=None,
            started_at=started_at,
            status="invalid_workflow",
            message=f"Unable to load workflow configuration: {error}",
        )

    workflow_id = str(workflow_config.get("workflow_id") or "unknown_workflow")
    runner_problem = _inspector_runner_problem(project, runner_id or "inspector")
    if runner_problem:
        return _failure(
            project=project,
            workflow_id=workflow_id,
            started_at=started_at,
            status="waiting_config",
            message=runner_problem,
        )
    context_paths = normalize_allowed_paths(allowed_paths, default_allowlist=default_allowed_paths(paths))

    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": new_chat_request_id(),
        "ts": utc_timestamp(),
        "user_message": message,
        "mode": INSPECTION_MODE,
        "role": "inspector",
        "runner_id": runner_id or "inspector",
        "allowed_paths": context_paths,
        "access_policy": "full_agent_access",
        "source": source or "cli",
        "workflow_id": workflow_id,
    }
    _append_jsonl(paths.requests_dir / CHAT_REQUESTS_FILENAME, request)

    result = _run_inspector_agent(
        project=project,
        paths=paths,
        workflow_id=workflow_id,
        request=request,
        runner_id=runner_id or "inspector",
        started_at=started_at,
        context_paths=context_paths,
    )
    return result


def _run_inspector_agent(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    request: Mapping[str, Any],
    runner_id: str,
    started_at: str,
    context_paths: Sequence[str],
) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "")
    message = str(request.get("user_message") or "").strip()
    response_path = paths.requests_dir / CHAT_RESPONSES_FILENAME
    files_written = [
        _path_for_record(project, paths.requests_dir / CHAT_REQUESTS_FILENAME),
        _path_for_record(project, response_path),
    ]
    warnings: list[Any] = []
    errors: list[str] = []
    change_request: dict[str, Any] | None = None
    if _is_workflow_change_request(message):
        change_request = _append_change_request(
            paths,
            workflow_id=workflow_id,
            user_request=message,
            originating_chat_request_id=request_id,
            source="inspector_chat",
        )
        files_written.append(_path_for_record(project, paths.requests_dir / CHANGE_REQUESTS_FILENAME))

    response_status = "answered"
    ok = True
    adapter_output: Mapping[str, Any] = {}
    prompt_path: Path | None = None
    prepared_record: Mapping[str, Any] = {}
    answer = ""
    details: dict[str, Any] = {}
    refs = _agent_context_refs(paths, change_request=change_request)
    try:
        runner = _full_access_runner(load_agent_runners(project).runner(runner_id))
        adapter = get_adapter(runner.adapter)
        prepared = prepare_run(
            project,
            role="inspector",
            runner_id=runner.runner_id,
            scheduler_owner=f"inspector:{request_id}",
            blocks_scheduler=False,
            updates_runtime_state=False,
            append_prepared_event=False,
        )
        prepared_record = prepared.to_dict(project_root=project)
        prompt_content = _build_agent_inspector_prompt(
            project=project,
            paths=paths,
            request=request,
            prepared=prepared,
            context_paths=context_paths,
            change_request=change_request,
        )
        prompt_path = prepared.prompt_path
        _atomic_write_text(prompt_path, prompt_content)
        _atomic_write_json(
            prepared.role_output_dir / "inspector_prompt_metadata.json",
            {
                "schema_version": SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "request_id": request_id,
                "run_id": prepared.run_id,
                "runner_id": runner.runner_id,
                "prompt_path": _path_for_record(project, prepared.prompt_path),
                "template_path": _path_for_record(project, INSPECTOR_TEMPLATE_PATH),
                "context_paths": list(context_paths),
                "access_policy": "full_agent_access",
            },
        )
        adapter_input = AdapterInput.from_runner_config(
            run_id=prepared.run_id,
            workflow_id=workflow_id,
            runner_config=runner,
            prompt_path=prepared.prompt_path,
            prompt_content=prompt_content,
            scheduler_run_dir=prepared.scheduler_run_dir,
            role_output_dir=prepared.role_output_dir,
            task_id=None,
            task_evidence_run_dir=None,
            cwd=str(_resolve_cwd(project, runner.cwd)),
            env={
                "LOOPPLANE_INSPECTION_REQUEST_ID": request_id,
                "LOOPPLANE_INSPECTION_RESPONSE_PATH": (prepared.role_output_dir / "inspection_response.json").as_posix(),
                "LOOPPLANE_CHAT_RESPONSES_PATH": response_path.as_posix(),
            },
            role="inspector",
        )
        adapter_input.write_json(prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME)
        files_written.extend(
            [
                _path_for_record(project, prepared.prompt_path),
                _path_for_record(project, prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME),
                _path_for_record(project, prepared.role_output_dir / "inspector_prompt_metadata.json"),
            ]
        )
        _update_active_run_lease(prepared.active_run_lease_path, status="running", started_at=utc_timestamp())
        _append_inspector_event(
            warnings,
            paths,
            workflow_id=workflow_id,
            run_id=prepared.run_id,
            event_type="inspector_adapter_started",
            data={
                "request_id": request_id,
                "runner_id": runner.runner_id,
                "adapter": runner.adapter,
                "role": "inspector",
                "prompt_path": _path_for_record(project, prepared.prompt_path),
                "role_output_dir": _path_for_record(project, prepared.role_output_dir),
            },
        )
        output = adapter.run(adapter_input)
        adapter_output = output.to_dict()
        output.write_json()
        files_written.append(_path_for_record(project, output.adapter_result_path))
        answer, details = _extract_inspector_answer(prepared.role_output_dir, output)
        if output.exit_code != 0 or output.timed_out:
            ok = False
            response_status = "failed_agent"
            if not answer:
                answer = f"Inspector agent exited with code {output.exit_code}."
            errors.append(answer)
        _append_inspector_event(
            warnings,
            paths,
            workflow_id=workflow_id,
            run_id=prepared.run_id,
            event_type="inspector_adapter_completed" if ok else "inspector_adapter_failed",
            data={
                "request_id": request_id,
                "runner_id": runner.runner_id,
                "adapter": runner.adapter,
                "role": "inspector",
                "exit_code": output.exit_code,
                "timed_out": output.timed_out,
                "adapter_result_path": _path_for_record(project, output.adapter_result_path),
                "role_output_dir": _path_for_record(project, prepared.role_output_dir),
            },
        )
        _update_active_run_lease(
            prepared.active_run_lease_path,
            status="completed" if ok else "failed",
            ended_at=utc_timestamp(),
        )
    except (AgentRunnerConfigError, AdapterLookupError, AdapterContractError, SchedulerError, OSError, json.JSONDecodeError, RuntimeError) as error:
        ok = False
        response_status = "failed_agent"
        answer = f"Inspector agent could not run: {error}"
        errors.append(answer)
        if prepared_record.get("active_run_lease_path"):
            _update_active_run_lease(project / str(prepared_record["active_run_lease_path"]), status="failed", ended_at=utc_timestamp())
        if prepared_record.get("run_id"):
            _append_inspector_event(
                warnings,
                paths,
                workflow_id=workflow_id,
                run_id=str(prepared_record.get("run_id")),
                event_type="inspector_adapter_failed",
                data={
                    "request_id": request_id,
                    "runner_id": runner_id,
                    "role": "inspector",
                    "error": str(error),
                    "role_output_dir": prepared_record.get("role_output_dir"),
                },
            )

    refs_summary = _refs_summary(refs)
    response = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "response_id": new_chat_response_id(),
        "ts": utc_timestamp(),
        "mode": INSPECTION_MODE,
        "role": "inspector",
        "runner_id": runner_id,
        "status": response_status,
        "answer": answer or "Inspector agent finished without a visible answer.",
        "summary": answer or "Inspector agent finished without a visible answer.",
        "details": details,
        "refs": refs,
        "refs_summary": refs_summary,
        "read_only": False,
        "access_policy": "full_agent_access",
        "commands_executed": _adapter_command(adapter_output),
        "prohibited_actions": [],
        "claims_completion": False,
        "agent_run": prepared_record,
        "adapter_result": adapter_output,
    }
    if change_request is not None:
        response["change_request_id"] = change_request["change_request_id"]
    _append_jsonl(response_path, response)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": response_status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "role": "inspector",
        "runner_id": runner_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "request": request,
        "response": response,
        "answer": response["answer"],
        "change_request": change_request,
        "allowed_paths": list(context_paths),
        "access_policy": "full_agent_access",
        "refs": refs,
        "refs_summary": refs_summary,
        "files_written": files_written,
        "commands_executed": response["commands_executed"],
        "prohibited_actions": [],
        "errors": errors,
        "warnings": warnings,
    }


def default_allowed_paths(paths: WorkflowPaths) -> list[str]:
    read_models = paths.value("read_models_dir").rstrip("/") + "/"
    results = paths.value("results_dir").rstrip("/") + "/"
    return [
        paths.value("plan_file"),
        read_models,
        paths.value("runtime_dir").rstrip("/") + "/state.json",
        results,
    ]


def _inspector_runner_problem(project: Path, runner_id: str) -> str | None:
    try:
        runner = load_agent_runners(project).runner(runner_id)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError) as error:
        return f"Inspector runner configuration is not usable: {error}"
    if runner.role != "inspector":
        return f"Runner {runner_id!r} has role {runner.role!r}, expected 'inspector'."
    if not runner.enabled:
        return f"Runner {runner_id!r} is disabled."
    return None


def normalize_allowed_paths(
    requested: Sequence[str] | None,
    *,
    default_allowlist: Sequence[str],
) -> list[str]:
    defaults = [_normalize_allowed_entry(path) for path in default_allowlist]
    if not requested:
        return defaults
    normalized: list[str] = []
    for value in requested:
        try:
            path = _normalize_allowed_entry(value)
        except WorkflowPathError:
            continue
        normalized.append(path)
    return sorted(dict.fromkeys(normalized))


def _full_access_runner(runner: RunnerConfig) -> RunnerConfig:
    permission_policy = dict(runner.permission_policy)
    permission_policy.update(
        {
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": False,
            "read_only": False,
        }
    )
    return replace(runner, permission_policy=permission_policy)


def _build_agent_inspector_prompt(
    *,
    project: Path,
    paths: WorkflowPaths,
    request: Mapping[str, Any],
    prepared: Any,
    context_paths: Sequence[str],
    change_request: Mapping[str, Any] | None,
) -> str:
    template = read_process_template(INSPECTOR_TEMPLATE_PATH)
    response_path = prepared.role_output_dir / "inspection_response.json"
    variables = {
        "project_root": project.as_posix(),
        "workflow_id": str(request.get("workflow_id") or ""),
        "inspection_request_id": str(request.get("request_id") or ""),
        "inspection_question": str(request.get("user_message") or ""),
        "inspection_source": str(request.get("source") or "cli"),
        "brief_file": paths.value("brief_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "plan_file": paths.value("plan_file"),
        "read_models_dir": paths.value("read_models_dir"),
        "runtime_dir": paths.value("runtime_dir"),
        "results_dir": paths.value("results_dir"),
        "requests_dir": paths.value("requests_dir"),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "final_output_path": _path_for_record(project, prepared.final_output_path),
        "inspection_response_path": _path_for_record(project, response_path),
        "context_paths": "\n".join(f"- {path}" for path in context_paths) or "- .",
        "change_request_id": str((change_request or {}).get("change_request_id") or "none"),
        "schema_version": SCHEMA_VERSION,
    }
    return _render_template(template, variables)


def _render_template(template: str, variables: Mapping[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(variables.get(key, match.group(0)))

    return re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", replace_match, template)


def _extract_inspector_answer(role_output_dir: Path, adapter_output: Any) -> tuple[str, dict[str, Any]]:
    response_path = role_output_dir / "inspection_response.json"
    if response_path.is_file():
        data = _read_json_object(response_path, default={})
        if isinstance(data, Mapping):
            answer = str(data.get("answer") or data.get("summary") or data.get("response") or "").strip()
            return answer, dict(data)
    final_text = _read_text_if_present(adapter_output.final_output_path).strip()
    stdout_text = _read_text_if_present(adapter_output.stdout_path).strip()
    stderr_text = _read_text_if_present(adapter_output.stderr_path).strip()
    answer = final_text or stdout_text
    if not answer and stderr_text:
        answer = stderr_text
    return answer, {
        "final_output_path": str(adapter_output.final_output_path),
        "stdout_path": str(adapter_output.stdout_path),
        "stderr_path": str(adapter_output.stderr_path),
    }


def _read_text_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _adapter_command(adapter_output: Mapping[str, Any]) -> list[str]:
    metadata = _mapping(adapter_output.get("adapter_metadata"))
    argv = metadata.get("argv")
    if isinstance(argv, Sequence) and not isinstance(argv, (str, bytes)):
        return [" ".join(str(part) for part in argv)]
    command = adapter_output.get("command")
    return [str(command)] if command else []


def _agent_context_refs(paths: WorkflowPaths, *, change_request: Mapping[str, Any] | None) -> list[str]:
    refs = [
        paths.value("brief_file"),
        paths.value("shared_context_file"),
        paths.value("plan_file"),
        paths.value("read_models_dir").rstrip("/") + "/",
        paths.value("runtime_dir").rstrip("/") + "/",
        paths.value("results_dir").rstrip("/") + "/",
    ]
    if change_request is not None:
        refs.append(paths.value("requests_dir").rstrip("/") + "/" + CHANGE_REQUESTS_FILENAME)
    return sorted(dict.fromkeys(refs))


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = cwd.replace("{{project_root}}", project.as_posix())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _update_active_run_lease(path: Path, **updates: Any) -> None:
    data = _read_json_object(path, default={})
    if not isinstance(data, Mapping):
        data = {}
    record = dict(data)
    record.update({key: value for key, value in updates.items() if value is not None})
    record["heartbeat_at"] = utc_timestamp()
    _atomic_write_json(path, record)


def _append_inspector_event(warnings: list[Any], paths: WorkflowPaths, **event: Any) -> None:
    try:
        append_event(paths, **event)
    except Exception as error:
        warnings.append(
            {
                "code": "inspector_event_append_failed",
                "message": str(error),
                "event_type": event.get("event_type"),
                "run_id": event.get("run_id"),
            }
        )


def inspection_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") else 1


def format_inspection_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane ask: {result.get('status', 'unknown')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
    ]
    request = result.get("request")
    if isinstance(request, Mapping):
        lines.append(f"request_id: {request.get('request_id')}")
    response = result.get("response")
    if isinstance(response, Mapping):
        lines.append(f"response_id: {response.get('response_id')}")
        lines.append(f"summary: {response.get('summary') or ''}")
    change_request = result.get("change_request")
    if isinstance(change_request, Mapping):
        lines.append(f"change_request_id: {change_request.get('change_request_id')}")
        lines.append(f"change_request_status: {change_request.get('status')}")
    refs = result.get("refs")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and refs:
        lines.append("refs:")
        visible_refs = [str(ref) for ref in refs[:8]]
        lines.extend(f"  - {ref}" for ref in visible_refs)
        if len(refs) > len(visible_refs):
            lines.append(f"  - ... {len(refs) - len(visible_refs)} more refs omitted; use --json for the full list")
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("warnings:")
        for warning in warnings:
            if isinstance(warning, Mapping):
                lines.append(f"  - {warning.get('code')}: {warning.get('message')}")
            else:
                lines.append(f"  - {warning}")
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("errors:")
        lines.extend(f"  - {error}" for error in errors)
    return "\n".join(lines) + "\n"


def new_chat_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"chat_{stamp}_{uuid.uuid4().hex[:8]}"


def new_chat_response_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"chat_response_{stamp}_{uuid.uuid4().hex[:8]}"


def new_change_request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"cr_{stamp}_{uuid.uuid4().hex[:8]}"


def _read_inspection_context(
    project: Path,
    paths: WorkflowPaths,
    allowed_paths: Sequence[str],
) -> dict[str, Any]:
    refs: list[str] = []
    warnings: list[dict[str, Any]] = []
    context: dict[str, Any] = {
        "refs": refs,
        "warnings": warnings,
        "read_models": {},
        "read_model_jsonl": {},
        "plan_tasks": [],
        "runtime_state": {},
        "result_summaries": [],
    }

    plan_rel = paths.value("plan_file")
    if _path_is_allowed(plan_rel, allowed_paths):
        try:
            plan_text = paths.plan_file.read_text(encoding="utf-8")
            context["plan_tasks"] = [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status,
                    "phase": task.phase,
                }
                for task in parse_plan_tasks(plan_text).values()
            ]
            refs.append(plan_rel)
        except OSError as error:
            warnings.append({"code": "plan_unreadable", "message": f"{plan_rel}: {error}"})

    state_rel = paths.value("runtime_dir").rstrip("/") + "/state.json"
    if _path_is_allowed(state_rel, allowed_paths):
        state = _read_json_object(paths.runtime_dir / "state.json", default={})
        if isinstance(state, Mapping):
            context["runtime_state"] = dict(state)
            refs.append(state_rel)

    read_models_rel = paths.value("read_models_dir").rstrip("/")
    if _path_is_allowed(read_models_rel + "/", allowed_paths):
        for filename in READ_MODEL_JSON_FILES:
            rel = f"{read_models_rel}/{filename}"
            payload = _read_json_object(paths.read_models_dir / filename, default=None)
            if isinstance(payload, Mapping):
                context["read_models"][filename] = dict(payload)
                refs.append(rel)
            else:
                warnings.append({"code": "read_model_missing", "message": f"{rel} is missing or not a JSON object."})
        for filename in READ_MODEL_JSONL_FILES:
            rel = f"{read_models_rel}/{filename}"
            records = _read_jsonl(paths.read_models_dir / filename)
            if (paths.read_models_dir / filename).exists():
                context["read_model_jsonl"][filename] = records
                refs.append(rel)
            else:
                warnings.append({"code": "read_model_missing", "message": f"{rel} is missing."})
        context["read_model_freshness"] = _read_model_freshness(paths, context["read_models"])
        freshness = context["read_model_freshness"]
        if isinstance(freshness, Mapping) and freshness.get("status") != "current":
            warnings.append(
                {
                    "code": "read_model_freshness",
                    "message": str(freshness.get("summary") or "Read model freshness could not be verified."),
                }
            )

    results_rel = paths.value("results_dir").rstrip("/")
    if _path_is_allowed(results_rel + "/", allowed_paths):
        result_summaries = _result_summaries(project, paths)
        context["result_summaries"] = result_summaries
        refs.extend(summary["path"] for summary in result_summaries if isinstance(summary.get("path"), str))

    context["refs"] = sorted(dict.fromkeys(refs))
    return context


def _build_summary(
    user_message: str,
    *,
    context: Mapping[str, Any],
    requested_disallowed: Sequence[str],
    change_request: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    workflow_status = _mapping(_mapping(context.get("read_models")).get("workflow_status.json"))
    plan_index = _mapping(_mapping(context.get("read_models")).get("plan_index.json"))
    runtime_state = _mapping(context.get("runtime_state"))
    plan_tasks = _sequence(context.get("plan_tasks"))
    freshness = _mapping(context.get("read_model_freshness"))
    progress = _progress_from_context(workflow_status, plan_index, plan_tasks)
    blocked_items = _blocked_items(workflow_status, plan_index, plan_tasks)
    pending_items = _pending_items(plan_index, plan_tasks)

    details = {
        "workflow_status": workflow_status.get("status") or runtime_state.get("status") or "unknown",
        "phase": workflow_status.get("phase"),
        "active_task_id": workflow_status.get("active_task_id"),
        "progress": progress,
        "blocked_items": blocked_items,
        "pending_items": pending_items[:5],
        "read_model_freshness": freshness.get("status") or "unknown",
        "requested_disallowed_paths": list(requested_disallowed),
    }

    if change_request is not None:
        details["change_request_id"] = change_request.get("change_request_id")
        return (
            "Inspector mode cannot edit PLAN.md, validation, or workflow state. "
            f"Created change request {change_request.get('change_request_id')} for planner review.",
            details,
        )

    preface = ""
    if requested_disallowed:
        preface = "Paths outside the inspector allowlist were not read. "

    question = user_message.lower()
    status = details["workflow_status"]
    progress_text = _progress_text(progress)
    active_task_id = details.get("active_task_id")
    focused = _focused_validation_summary(user_message, _sequence(context.get("result_summaries")))
    if focused is not None:
        details["focused_result"] = focused["details"]
        summary = preface + focused["summary"]
    elif "block" in question or "attention" in question:
        if blocked_items:
            first = blocked_items[0]
            summary = (
                f"{preface}Workflow status is {status}. The first item needing attention is "
                f"{first.get('task_id') or first.get('type')}: {first.get('message') or first.get('title') or first.get('status')}."
            )
        else:
            summary = f"{preface}Workflow status is {status}; no blocker is visible in the allowlisted status files."
    elif "task" in question or "progress" in question or "status" in question:
        summary = f"{preface}Workflow status is {status}; {progress_text}."
    else:
        summary = f"{preface}Workflow status is {status}; {progress_text}."

    if active_task_id:
        summary += f" Active task: {active_task_id}."
    if freshness.get("status") and freshness.get("status") != "current":
        summary += f" Read models are {freshness.get('status')}; status may be stale."
    return summary, details


def _append_change_request(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    user_request: str,
    originating_chat_request_id: str,
    source: str,
) -> dict[str, Any]:
    record = {
        "schema_version": SCHEMA_VERSION,
        "change_request_id": new_change_request_id(),
        "created_at": utc_timestamp(),
        "source": source,
        "workflow_id": workflow_id,
        "user_request": user_request,
        "originating_chat_request_id": originating_chat_request_id,
        "status": "pending_review",
        "impact": {
            "scope_change": True,
            "requires_new_tasks": False,
            "requires_approval": True,
            "analysis_required": True,
        },
        "planner_response": None,
        "approval_request_id": None,
        "applied_plan_update_event_id": None,
    }
    _append_jsonl(paths.requests_dir / CHANGE_REQUESTS_FILENAME, record)
    return record


def _response_refs(
    context: Mapping[str, Any],
    *,
    paths: WorkflowPaths,
    change_request: Mapping[str, Any] | None,
) -> list[str]:
    refs = list(context.get("refs") or [])
    if change_request is not None:
        refs.append(paths.value("requests_dir").rstrip("/") + "/" + CHANGE_REQUESTS_FILENAME)
    return sorted(dict.fromkeys(str(ref) for ref in refs if str(ref).strip()))


def _refs_summary(refs: Sequence[str]) -> dict[str, Any]:
    by_prefix: dict[str, int] = {}
    for ref in refs:
        prefix = str(ref).split("/", 1)[0] or "."
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
    return {
        "count": len(refs),
        "shown_in_text": min(len(refs), 8),
        "omitted_in_text": max(0, len(refs) - 8),
        "by_prefix": dict(sorted(by_prefix.items())),
    }


def _is_workflow_change_request(message: str) -> bool:
    text = " ".join(message.lower().split())
    status_prefixes = (
        "what change",
        "which change",
        "list change",
        "show change",
        "summarize change",
        "where",
        "what is",
        "what's",
        "which",
        "show status",
        "summarize status",
        "list status",
    )
    if text.startswith(status_prefixes):
        return False
    markers = (
        "please add",
        "please change",
        "please update",
        "please remove",
        "can you add",
        "can you change",
        "can you update",
        "i want to add",
        "add ",
        "remove ",
        "delete ",
        "modify ",
        "update ",
        "edit ",
        "change the plan",
        "change plan",
        "edit plan",
        "edit plan.md",
        "write plan.md",
        "mark ",
        "skip ",
        "unblock ",
        "block ",
        "rename ",
        "reorder ",
        "create task",
        "new task",
    )
    return any(marker in text for marker in markers)


def _requested_disallowed_paths(message: str, allowed_paths: Sequence[str]) -> list[str]:
    candidates = []
    raw_tokens = message.replace("`", " ").replace(",", " ").split()
    for index, token in enumerate(raw_tokens):
        clean = token.strip(" ;:()[]{}\"'")
        if not clean:
            continue
        if _token_looks_like_explicit_path_request(clean, raw_tokens, index):
            candidates.append(clean)
    disallowed: list[str] = []
    for candidate in candidates:
        try:
            normalized = _normalize_relative_path(candidate)
        except WorkflowPathError:
            disallowed.append(candidate)
            continue
        if not _path_is_allowed(normalized, allowed_paths):
            disallowed.append(candidate)
    return sorted(dict.fromkeys(disallowed))


def _read_model_freshness(
    paths: WorkflowPaths,
    read_models: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_status = _mapping(read_models.get("workflow_status.json"))
    projection = load_event_log_projection(paths)
    state = _mapping(projection.get("state"))
    latest_event = _mapping(state.get("latest_event"))
    event_seq = _coerce_int(latest_event.get("sequence"))
    if event_seq is None:
        event_seq = _coerce_int(state.get("latest_event_seq"))
    model_seq = _coerce_int(workflow_status.get("last_event_seq"))
    missing = not bool(read_models)
    stale = event_seq is not None and model_seq is not None and model_seq < event_seq
    if missing:
        status = "missing"
        summary = "Read models are missing from the allowlisted read-model directory."
    elif stale:
        status = "stale"
        summary = "Read models were generated before the current event log head."
    elif model_seq is None and event_seq is not None:
        status = "unknown"
        summary = "Read model freshness metadata is incomplete."
    else:
        status = "current"
        summary = "Read models match the visible event log head."
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "summary": summary,
        "read_model_last_event_seq": model_seq,
        "event_log_last_event_seq": event_seq,
    }


def _result_summaries(project: Path, paths: WorkflowPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.results_dir.exists():
        return records
    interesting_names = {"latest.json", "validation.json", "agent_status.json", "acceptance_results.json"}
    discovery = discover_files_bounded(
        (paths.results_dir,),
        names=interesting_names,
        max_entries=20_000,
        max_matches=2_000,
        max_depth=10,
    )
    for path in discovery.paths:
        payload = _read_json_object(path, default=None)
        if not isinstance(payload, Mapping):
            continue
        record: dict[str, Any] = {
            "path": _path_for_record(project, path),
            "status": payload.get("status"),
            "task_id": payload.get("task_id") or payload.get("primary_task_id"),
            "run_id": payload.get("run_id") or payload.get("latest_run_id"),
            "kind": path.name.removesuffix(".json"),
        }
        if path.name == "validation.json":
            task_results = [_mapping(item) for item in _sequence(payload.get("task_results"))]
            record["task_results"] = [
                {
                    "task_id": result.get("task_id"),
                    "status": result.get("status"),
                    "verdict": result.get("verdict"),
                    "checks": [
                        {
                            "name": _mapping(check).get("name"),
                            "status": _mapping(check).get("status"),
                            "message": _mapping(check).get("message"),
                        }
                        for check in _sequence(result.get("checks"))
                        if isinstance(check, Mapping)
                    ],
                }
                for result in task_results
            ]
        elif path.name == "agent_status.json":
            record["summary_candidate"] = payload.get("summary_candidate")
            record["commands_run"] = [
                {
                    "command": _mapping(command).get("command") or _mapping(command).get("cmd"),
                    "exit_code": _mapping(command).get("exit_code") or _mapping(command).get("actual_exit_code"),
                }
                for command in _sequence(payload.get("commands_run"))
                if isinstance(command, Mapping)
            ][:8]
        elif path.name == "acceptance_results.json":
            record["kind"] = "acceptance_results"
            record["summary"] = payload.get("summary")
            record["results"] = [
                {
                    "task_id": _mapping(item).get("task_id"),
                    "status": _mapping(item).get("status"),
                    "message": _mapping(item).get("message") or _mapping(item).get("summary"),
                }
                for item in _sequence(payload.get("results") or payload.get("task_results"))
                if isinstance(item, Mapping)
            ][:20]
        records.append(record)
        if len(records) >= 100:
            break
    return records


def _token_looks_like_explicit_path_request(clean: str, raw_tokens: Sequence[str], index: int) -> bool:
    lower = clean.lower()
    if clean.startswith("."):
        return True
    pathish = "/" in clean or lower.endswith((".md", ".json"))
    if not pathish:
        return False
    markers = {
        "cat",
        "content",
        "contents",
        "display",
        "file",
        "inspect",
        "open",
        "path",
        "read",
        "show",
        "summarize",
        "tell",
    }
    window = [
        raw_tokens[position].strip(" ;:()[]{}\"'").lower()
        for position in range(max(0, index - 3), index)
    ]
    if any(token in markers for token in window):
        return True
    if lower.startswith((".loopplane/", "plan.md", "project_brief.md")):
        return True
    return False


def _focused_validation_summary(user_message: str, result_summaries: Sequence[Any]) -> dict[str, Any] | None:
    task_ids = _mentioned_task_ids(user_message)
    if not task_ids:
        return None
    question = user_message.lower()
    wants_validation = any(marker in question for marker in ("validation", "check", "command_exit_code", "exit"))
    wants_deliverable = "deliverable" in question or "summary" in question or "did" in question
    if not wants_validation and not wants_deliverable:
        return None
    matches: list[Mapping[str, Any]] = []
    for record in result_summaries:
        summary = _mapping(record)
        if summary.get("kind") != "validation":
            continue
        if _validation_record_mentions_task(summary, task_ids):
            matches.append(summary)
    if not matches:
        return {
            "summary": f"No validation result for {', '.join(task_ids)} is visible in the allowlisted result summaries.",
            "details": {"task_ids": task_ids, "matched": False},
        }
    chosen = matches[-1]
    task_result = _matching_task_result(chosen, task_ids)
    checks = [_mapping(check) for check in _sequence(task_result.get("checks"))]
    check_name = "command_exit_code" if "command_exit_code" in question or "exit" in question else ""
    selected_checks = [check for check in checks if not check_name or str(check.get("name")) == check_name]
    status = task_result.get("status") or chosen.get("status") or "unknown"
    verdict = task_result.get("verdict") or "unknown"
    task_id = str(task_result.get("task_id") or chosen.get("task_id") or task_ids[0])
    if selected_checks:
        check = selected_checks[0]
        summary = (
            f"{task_id} validation is {status} ({verdict}). "
            f"{check.get('name')} is {check.get('status')}: {check.get('message')}"
        )
    else:
        summary = f"{task_id} validation is {status} ({verdict}); no named validation check matched the question."
    return {
        "summary": summary,
        "details": {
            "task_ids": task_ids,
            "matched": True,
            "path": chosen.get("path"),
            "status": status,
            "verdict": verdict,
            "checks": selected_checks or checks,
        },
    }


def _mentioned_task_ids(value: str) -> list[str]:
    seen: set[str] = set()
    task_ids: list[str] = []
    for match in re.finditer(r"\b[A-Za-z]\d+(?:\.[A-Za-z]\d+)*\.T\d+\b|\b[A-Z]{2,}-\d+\b|\bT\d+\b", value):
        task_id = match.group(0)
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)
    return task_ids


def _validation_record_mentions_task(record: Mapping[str, Any], task_ids: Sequence[str]) -> bool:
    if str(record.get("task_id") or "") in task_ids:
        return True
    return bool(_matching_task_result(record, task_ids))


def _matching_task_result(record: Mapping[str, Any], task_ids: Sequence[str]) -> Mapping[str, Any]:
    for result in _sequence(record.get("task_results")):
        result_map = _mapping(result)
        if str(result_map.get("task_id") or "") in task_ids:
            return result_map
    return {}


def _progress_from_context(
    workflow_status: Mapping[str, Any],
    plan_index: Mapping[str, Any],
    plan_tasks: Sequence[Any],
) -> dict[str, Any]:
    progress = _mapping(workflow_status.get("progress"))
    if progress:
        return dict(progress)
    summary = _mapping(plan_index.get("summary"))
    if summary:
        total = _coerce_int(summary.get("total")) or 0
        done = _coerce_int(summary.get("done")) or 0
        return {
            "total_tasks": total,
            "completed_tasks": done,
            "partial_tasks": _coerce_int(summary.get("partial")) or 0,
            "blocked_tasks": _coerce_int(summary.get("blocked")) or 0,
            "skipped_tasks": _coerce_int(summary.get("skipped")) or 0,
            "progress_percent": summary.get("progress_percent"),
        }
    counts = {"total_tasks": len(plan_tasks), "completed_tasks": 0, "partial_tasks": 0, "blocked_tasks": 0, "skipped_tasks": 0}
    for item in plan_tasks:
        task = _mapping(item)
        status = str(task.get("status") or " ")
        if status == "x":
            counts["completed_tasks"] += 1
        elif status == "~":
            counts["partial_tasks"] += 1
        elif status == "!":
            counts["blocked_tasks"] += 1
        elif status == "-":
            counts["skipped_tasks"] += 1
    return counts


def _blocked_items(
    workflow_status: Mapping[str, Any],
    plan_index: Mapping[str, Any],
    plan_tasks: Sequence[Any],
) -> list[dict[str, Any]]:
    attention = _sequence(workflow_status.get("requires_attention"))
    blocked = [dict(_mapping(item)) for item in attention if isinstance(item, Mapping)]
    for phase in _sequence(plan_index.get("phases")):
        for task in _sequence(_mapping(phase).get("tasks")):
            task_map = _mapping(task)
            if str(task_map.get("status")) in {"blocked", "partial"}:
                blocked.append(
                    {
                        "task_id": task_map.get("task_id"),
                        "title": task_map.get("title"),
                        "status": task_map.get("status"),
                        "message": task_map.get("display"),
                    }
                )
    for item in plan_tasks:
        task = _mapping(item)
        if str(task.get("status")) in {"!", "~"}:
            blocked.append(
                {
                    "task_id": task.get("task_id"),
                    "title": task.get("title"),
                    "status": "blocked" if task.get("status") == "!" else "partial",
                }
            )
    return blocked


def _pending_items(plan_index: Mapping[str, Any], plan_tasks: Sequence[Any]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for phase in _sequence(plan_index.get("phases")):
        for task in _sequence(_mapping(phase).get("tasks")):
            task_map = _mapping(task)
            if str(task_map.get("status")) == "pending":
                pending.append({"task_id": task_map.get("task_id"), "title": task_map.get("title")})
    if pending:
        return pending
    for item in plan_tasks:
        task = _mapping(item)
        if str(task.get("status") or " ") == " ":
            pending.append({"task_id": task.get("task_id"), "title": task.get("title")})
    return pending


def _progress_text(progress: Mapping[str, Any]) -> str:
    total = progress.get("total_tasks")
    done = progress.get("completed_tasks")
    if total is None or done is None:
        return "progress is unknown from the allowlisted files"
    return f"{done}/{total} tasks are marked done in the current plan/read models"


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n")


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


def _failure(
    *,
    project: Path,
    workflow_id: str | None,
    started_at: str,
    status: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "message": message,
        "errors": [message],
        "warnings": [],
    }


def _normalize_allowed_entry(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowPathError("allowed path must be non-empty")
    directory = text.endswith("/")
    normalized = _normalize_relative_path(text.rstrip("/"))
    return normalized.rstrip("/") + "/" if directory else normalized


def _normalize_relative_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowPathError("path must be non-empty")
    if "\\" in text:
        raise WorkflowPathError(f"path must use POSIX-style '/' separators: {text}")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise WorkflowPathError(f"path must be project-root-relative: {text}")
    if path == PurePosixPath(".") or ".." in path.parts:
        raise WorkflowPathError(f"path must stay inside the project root: {text}")
    return path.as_posix()


def _entry_within_allowlist(path: str, defaults: Sequence[str]) -> bool:
    return any(_entry_matches(path, default) for default in defaults)


def _path_is_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    normalized = _normalize_allowed_entry(path)
    return any(_entry_matches(normalized, allowed) for allowed in allowed_paths)


def _entry_matches(candidate: str, allowed: str) -> bool:
    if allowed.endswith("/"):
        return candidate == allowed or candidate.startswith(allowed)
    return candidate == allowed


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")


def _path_for_record(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
