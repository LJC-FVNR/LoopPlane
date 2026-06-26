from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.active_projections import sync_active_workflow_projections
from runtime.adapters.base import utc_timestamp
from runtime.init_workflow import InitConflictError, _workflow_config, materialize_canonical_workflow_files
from runtime.path_resolution import WorkflowPaths, path_lines
from runtime.planning import (
    READINESS_REPORT_FILENAME,
    _interactive_approval_enabled,
    _new_run_id,
    _readiness_report,
    inspect_plan_draft,
)
from runtime.version_control import plan_local_repository_initialization
from runtime.workflow_lifecycle import WorkflowLifecycleError, create_workflow_record
import runtime.workflows as workflow_module


TEMPLATE_SCHEMA_VERSION = "1.0"
TEMPLATE_KIND = "loopplane.workflow_template"
PRESET_KIND = "loopplane.workflow_preset"
INSTANCE_KIND = "loopplane.template_instance"
BUILTIN_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "workflows"
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,127}$")
VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
IF_RE = re.compile(r"<!--\s*loopplane:if\s+([A-Za-z_][A-Za-z0-9_]*)\s*-->")
ENDIF_RE = re.compile(r"<!--\s*loopplane:endif\s*-->")
DERIVED_RE = re.compile(r"^(slug|lower|upper)\(([A-Za-z_][A-Za-z0-9_]*)\)$")
PATH_LIKE_INPUT_RE = re.compile(r"(^|_)(path|file|dir)(_|$)")
RENDER_TARGETS = {
    "project_brief": "brief_file",
    "plan_draft": "plan_draft_file",
    "shared_context": "shared_context_file",
}


class TemplatePresetError(ValueError):
    def __init__(self, status: str, errors: Sequence[str], *, warnings: Sequence[str] = ()) -> None:
        self.status = status
        self.errors = list(errors)
        self.warnings = list(warnings)
        super().__init__("; ".join(self.errors))


def list_workflow_templates(project: Path | str | None = None) -> dict[str, Any]:
    roots = [BUILTIN_TEMPLATE_ROOT]
    project_root = Path(project).expanduser().resolve() if project is not None else None
    if project_root is not None:
        roots.append(project_root / ".loopplane" / "templates" / "workflows")

    templates: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for template_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest_path = template_dir / "template.json"
            if not manifest_path.is_file():
                continue
            try:
                template = _load_template_from_root(template_dir)
            except TemplatePresetError as error:
                warnings.extend(error.errors)
                continue
            template_id = str(template["id"])
            if template_id in seen:
                warnings.append(f"duplicate template id ignored: {template_id}")
                continue
            seen.add(template_id)
            templates.append(_template_summary(template))

    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": True,
        "status": "listed",
        "template_count": len(templates),
        "templates": templates,
        "warnings": warnings,
        "errors": [],
    }


def load_workflow_template(template_id: str, *, template_dir: Path | str | None = None) -> dict[str, Any]:
    template_id = str(template_id or "").strip()
    if not TEMPLATE_ID_RE.match(template_id):
        raise TemplatePresetError("template_not_found", [f"invalid template id: {template_id!r}"])
    root = _resolve_template_root(template_id, template_dir=Path(template_dir) if template_dir is not None else None)
    return _load_template_from_root(root)


def load_workflow_preset(path: Path | str) -> dict[str, Any]:
    preset_path = Path(path).expanduser().resolve()
    try:
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TemplatePresetError("preset_invalid", [f"{preset_path}: unable to read preset: {error}"]) from error
    if not isinstance(preset, dict):
        raise TemplatePresetError("preset_invalid", [f"{preset_path}: preset must be a JSON object"])
    errors: list[str] = []
    if preset.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
        errors.append("preset schema_version must be '1.0'")
    if preset.get("kind") != PRESET_KIND:
        errors.append(f"preset kind must be {PRESET_KIND!r}")
    template_id = preset.get("template")
    if not isinstance(template_id, str) or not TEMPLATE_ID_RE.match(template_id):
        errors.append("preset template must be a valid template id")
    inputs = preset.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("preset inputs must be an object")
    if errors:
        raise TemplatePresetError("preset_invalid", [f"{preset_path}: {error}" for error in errors])
    preset["_preset_path"] = preset_path.as_posix()
    preset["_preset_sha256"] = _file_sha256(preset_path)
    return preset


def merge_template_inputs(
    template: Mapping[str, Any],
    preset: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = _template_manifest(template)
    input_defs = _input_definitions(manifest)
    values: dict[str, Any] = {}
    explicit: set[str] = set()

    for name, spec in input_defs.items():
        if "default" in spec:
            values[name] = spec["default"]

    if preset is not None:
        preset_inputs = preset.get("inputs")
        if isinstance(preset_inputs, Mapping):
            for name, value in preset_inputs.items():
                values[str(name)] = value
                explicit.add(str(name))

    for name, value in dict(overrides or {}).items():
        key = str(name)
        spec = input_defs.get(key)
        if spec is not None and spec.get("locked") is True:
            raise TemplatePresetError("input_validation_failed", [f"input {key!r} is locked and cannot be overridden"])
        values[key] = value
        explicit.add(key)

    _resolve_derived_inputs(input_defs, values, explicit)
    values = _render_string_defaults(input_defs, values)
    _validate_inputs(input_defs, values)
    return values


def render_workflow_template(
    template: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    workflow_context: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _template_manifest(template)
    template_root = _template_root(template)
    render_map = manifest.get("renders")
    if not isinstance(render_map, Mapping):
        raise TemplatePresetError("template_manifest_invalid", ["template renders must be an object"])
    if "plan_draft" not in render_map:
        raise TemplatePresetError("template_manifest_invalid", ["template renders must include plan_draft"])

    variables = {
        **_policy_values(manifest, None),
        **dict(workflow_context),
        **dict(inputs),
    }
    rendered: dict[str, dict[str, Any]] = {}
    for target, source_name in render_map.items():
        target_name = str(target)
        if target_name not in RENDER_TARGETS:
            raise TemplatePresetError("template_manifest_invalid", [f"unknown render target: {target_name}"])
        source_path = _safe_template_child(template_root, str(source_name))
        if not source_path.is_file():
            raise TemplatePresetError("template_manifest_invalid", [f"{source_name}: render file is missing"])
        text = source_path.read_text(encoding="utf-8")
        rendered_text = _render_text(text, variables)
        rendered[target_name] = {
            "text": rendered_text,
            "source": str(source_name),
            "bytes": len(rendered_text.encode("utf-8")),
            "sha256": _sha256_bytes(rendered_text.encode("utf-8")),
        }

    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": True,
        "status": "rendered",
        "template": _template_summary(template),
        "inputs": dict(inputs),
        "rendered": rendered,
        "warnings": [],
        "errors": [],
    }


def render_template_preview(
    *,
    template_id: str,
    preset_path: Path | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    template_dir: Path | str | None = None,
    output: Path | str | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    try:
        template, preset, inputs = _load_template_preset_inputs(
            template_id=template_id,
            preset_path=preset_path,
            overrides=overrides,
            template_dir=template_dir,
        )
        workflow_context = _preview_workflow_context(project_root)
        rendered = render_workflow_template(template, inputs, workflow_context=workflow_context)
        output_files: list[str] = []
        if output is not None:
            output_dir = Path(output).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            for target, record in rendered["rendered"].items():
                filename = _preview_filename(target)
                path = output_dir / filename
                path.write_text(str(record["text"]), encoding="utf-8")
                output_files.append(path.as_posix())
    except TemplatePresetError as error:
        return _template_result(False, error.status, errors=error.errors, warnings=error.warnings)
    except OSError as error:
        return _template_result(False, "render_output_failed", errors=[str(error)])
    rendered["status"] = "rendered"
    rendered["preset"] = _preset_summary(preset) if preset else None
    rendered["output_files"] = output_files
    rendered["mutation_boundary"] = (
        "template render may write preview files only to an explicit output directory; "
        "it does not register workflows or mutate project-local .loopplane workflow truth."
    )
    return rendered


def doctor_workflow_template(template_id: str, *, template_dir: Path | str | None = None) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    try:
        template = load_workflow_template(template_id, template_dir=template_dir)
    except TemplatePresetError as error:
        return _template_result(False, error.status, errors=error.errors, warnings=error.warnings)
    root = _template_root(template)
    render_map = _template_manifest(template).get("renders")
    if not isinstance(render_map, Mapping):
        errors.append("template renders must be an object")
    else:
        for target, source in render_map.items():
            if str(target) not in RENDER_TARGETS:
                errors.append(f"unknown render target: {target}")
            try:
                source_path = _safe_template_child(root, str(source))
            except TemplatePresetError as error:
                errors.extend(error.errors)
                continue
            if not source_path.is_file():
                errors.append(f"{source}: render file is missing")
    for path in sorted((root / "examples").glob("*.preset.json")) if (root / "examples").is_dir() else ():
        try:
            preset = load_workflow_preset(path)
            if preset.get("template") != template["id"]:
                errors.append(f"{path}: preset template does not match {template['id']}")
            merge_template_inputs(template, preset)
        except TemplatePresetError as error:
            errors.extend(error.errors)
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": not errors,
        "status": "valid" if not errors else "invalid",
        "template": _template_summary(template),
        "errors": errors,
        "warnings": warnings,
    }


def create_workflow_from_template(
    project_root: Path | str,
    *,
    template_id: str | None = None,
    preset_path: Path | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    template_dir: Path | str | None = None,
    make_current: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    loopplane_dir = project / ".loopplane"
    workspace_file = loopplane_dir / "workspace.json"
    registry_file = loopplane_dir / "workflow_registry.json"
    current_file = loopplane_dir / "current_workflow.json"
    warnings: list[str] = []
    if not make_current:
        return workflow_module._failure(
            status="unsupported_non_current_template_create",
            project=project,
            errors=["template workflow creation must make the new workflow current until activate-plan supports --workflow"],
            recovery_actions=["Create the workflow as current, or add activate-plan --workflow support first."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )

    context = _load_workflow_create_context(project, workspace_file, registry_file, current_file)
    if context.get("ok") is not True:
        return context
    workspace = context["workspace"]
    registry = context["registry"]
    workflows = context["workflows"]
    current_workflow_id = context.get("current_workflow_id")
    warnings.extend(context.get("warnings") or [])

    try:
        template, preset, inputs = _load_template_preset_inputs(
            template_id=template_id,
            preset_path=preset_path,
            overrides=overrides,
            template_dir=template_dir,
        )
    except TemplatePresetError as error:
        return workflow_module._failure(
            status=error.status,
            project=project,
            errors=error.errors,
            recovery_actions=["Inspect the template and preset with loopplane template doctor/show."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )

    created_at = utc_timestamp()
    workflow_id, workflow_root = workflow_module._new_workflow_identity(project, workflows, created_at)
    workflow_config = _workflow_config(workflow_id, created_at, {}, layout="canonical_v1_6")
    paths = WorkflowPaths.from_config(project, workflow_config)
    workflow_name = _workflow_name(template, preset, inputs)
    name_source = workflow_name
    name_was_truncated = False
    provisional_record = {
        "workflow_id": workflow_id,
        "name": workflow_name,
        "status": "draft",
        "workflow_root": workflow_root,
        "runtime_dir": f"{workflow_root}/runtime",
    }
    safety = workflow_module._workflow_create_safety(
        project,
        workflows=workflows,
        new_record=provisional_record,
        current_workflow_id=current_workflow_id,
    )
    if safety["blockers"]:
        return workflow_module._failure(
            status=str(safety.get("status") or "workflow_create_blocked"),
            project=project,
            errors=[str(blocker.get("message") or blocker.get("code")) for blocker in safety["blockers"]],
            recovery_actions=[
                "Wait for active scheduler work to finish or stop/pause the active workflow before creating a new current workflow.",
                "Inspect loopplane status --project <project> and loopplane attach --project <project> for active runtime details.",
            ],
            extra={
                **workflow_module._file_context(workspace_file, registry_file, current_file),
                "workspace_id": workflow_module._workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": None,
                "proposed_workflow_id": workflow_id,
                "workflow_root": workflow_root,
                "safety": safety,
            },
        )

    workflow_context = _workflow_context(project, paths, workflow_id=workflow_id, generated_at=created_at)
    try:
        rendered = render_workflow_template(template, inputs, workflow_context=workflow_context)
        plan_text = str(rendered["rendered"]["plan_draft"]["text"])
        structural_report = _inspect_rendered_plan(plan_text, workflow_id=workflow_id)
        if not structural_report.get("valid"):
            return workflow_module._failure(
                status="plan_draft_invalid",
                project=project,
                errors=[str(error) for error in structural_report.get("errors", [])],
                recovery_actions=["Fix the workflow template PLAN_DRAFT.md.tpl and rerun template doctor/render."],
                extra={
                    **workflow_module._file_context(workspace_file, registry_file, current_file),
                    "workflow_id": workflow_id,
                    "workflow_root": workflow_root,
                    "plan_draft": structural_report,
                },
            )
    except TemplatePresetError as error:
        return workflow_module._failure(
            status=error.status,
            project=project,
            errors=error.errors,
            recovery_actions=["Fix the template or preset and rerun the command."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )

    if dry_run:
        return {
            "schema_version": "1.6",
            "ok": True,
            "status": "dry_run",
            "project_root": project.as_posix(),
            "loopplane_dir": loopplane_dir.as_posix(),
            "workspace_id": workflow_module._workspace_id(workspace, registry),
            "workflow_id": workflow_id,
            "workflow_root": workflow_root,
            "previous_current_workflow_id": current_workflow_id,
            "current_workflow_id": workflow_id,
            "template": _template_summary(template),
            "preset": _preset_summary(preset) if preset else None,
            "inputs": inputs,
            "plan_draft": structural_report,
            "rendered_files": _rendered_file_preview(project, paths, rendered),
            "errors": [],
            "warnings": warnings,
            "mutation_boundary": "dry-run template workflow creation does not write workflow files or registry metadata.",
        }

    try:
        version_control = plan_local_repository_initialization(project)
        files_result = materialize_canonical_workflow_files(
            project,
            _brief_text(rendered, template, inputs),
            workflow_id=workflow_id,
            created_at=created_at,
            version_control=version_control,
        )
        paths = files_result["paths"]
        written_files = _write_rendered_workflow_files(project, paths, rendered)
        readiness_report = _write_template_readiness_report(
            project,
            paths,
            workflow_id=workflow_id,
            workflow_config=files_result["workflow_config"],
            generated_at=created_at,
        )
        instance = _write_template_instance(
            project,
            paths,
            workflow_id=workflow_id,
            created_at=created_at,
            template=template,
            preset=preset,
            inputs=inputs,
            rendered_files=written_files,
        )
        lifecycle = create_workflow_record(
            project,
            workflow_id=workflow_id,
            name=workflow_name,
            workflow_root=workflow_root,
            status="draft",
            make_current=True,
            selection_reason=workflow_module.WORKFLOW_CREATE_SELECTION_REASON,
            updated_by=workflow_module.WORKFLOW_CREATE_UPDATED_BY,
            created_at=created_at,
            summary={
                "one_line": _summary_one_line(template, preset, inputs),
                "tasks_total": 0,
                "tasks_completed": 0,
                "tasks_blocked": 0,
            },
            path_values=dict(paths.values),
            extra_fields={
                "template": {
                    "id": str(template["id"]),
                    "version": str(template["version"]),
                }
            },
        )
        projection_sync = sync_active_workflow_projections(
            project,
            files_result["workflow_config"],
            paths,
            reason="workflow_template_create",
        )
    except InitConflictError as error:
        return workflow_module._failure(
            status="workflow_create_conflict",
            project=project,
            errors=list(error.conflicts),
            recovery_actions=["Choose a new workflow ID/root or inspect the existing path before retrying."],
            extra={
                **workflow_module._file_context(workspace_file, registry_file, current_file),
                "workspace_id": workflow_module._workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": workflow_id,
                "workflow_root": workflow_root,
            },
        )
    except (OSError, WorkflowLifecycleError, ValueError) as error:
        return workflow_module._failure(
            status="workflow_create_failed",
            project=project,
            errors=[str(error)],
            recovery_actions=["Inspect project-local .loopplane metadata and filesystem permissions."],
            extra={
                **workflow_module._file_context(workspace_file, registry_file, current_file),
                "workspace_id": workflow_module._workspace_id(workspace, registry),
                "previous_current_workflow_id": current_workflow_id,
                "workflow_id": workflow_id,
                "workflow_root": workflow_root,
            },
        )

    updated_registry = workflow_module._load_json_object(registry_file)
    updated_current = workflow_module._load_json_object(current_file)
    updated_workflows = [dict(record) for record in updated_registry.get("workflows", [])]
    selected_index, selected_record = workflow_module._workflow_record_with_index(updated_workflows, workflow_id)
    workflow = workflow_module._workflow_show_record(
        project,
        selected_record or lifecycle["record"],
        index=selected_index if selected_index >= 0 else len(updated_workflows) - 1,
        current_workflow_id=workflow_id,
    )
    projection_warnings = projection_sync.get("warnings") if isinstance(projection_sync, Mapping) else None
    if isinstance(projection_warnings, Sequence) and not isinstance(projection_warnings, (str, bytes)):
        warnings.extend(str(warning) for warning in projection_warnings)
    created_files = list(files_result.get("created") or [])
    for relative in written_files:
        if relative not in created_files:
            created_files.append(relative)
    readiness_relative = _project_relative(project, paths.planning_dir / READINESS_REPORT_FILENAME)
    if readiness_relative not in created_files:
        created_files.append(readiness_relative)
    instance_relative = _project_relative(project, paths.workflow_root / "template_instance.json")
    if instance_relative not in created_files:
        created_files.append(instance_relative)
    return {
        "schema_version": "1.6",
        "ok": True,
        "status": "created_from_template",
        "project_root": project.as_posix(),
        "loopplane_dir": loopplane_dir.as_posix(),
        "workspace_id": workflow_module._workspace_id(workspace, updated_registry),
        "workspace_file": workspace_file.as_posix(),
        "registry_file": registry_file.as_posix(),
        "current_workflow_file": current_file.as_posix(),
        "workflow_id": workflow_id,
        "workflow_root": workflow_root,
        "workflow_config_file": files_result["workflow_config_file"],
        "previous_current_workflow_id": current_workflow_id,
        "current_workflow_id": workflow_id,
        "selection_reason": workflow_module.WORKFLOW_CREATE_SELECTION_REASON,
        "updated_at": str(lifecycle.get("updated_at") or updated_current.get("updated_at") or ""),
        "updated_by": workflow_module.WORKFLOW_CREATE_UPDATED_BY,
        "workflow_name": workflow_name,
        "workflow_name_source_excerpt": name_source,
        "workflow_name_was_truncated": name_was_truncated,
        "workflow_name_limit": 96,
        "created": created_files,
        "preserved": list(files_result.get("preserved") or []),
        "current_update": lifecycle.get("current_update"),
        "current_workflow": updated_current,
        "workflow_count": len(updated_workflows),
        "workflow": workflow,
        "template": _template_summary(template),
        "preset": _preset_summary(preset) if preset else None,
        "template_instance": instance,
        "plan_draft": readiness_report.get("structural_checks") or structural_report,
        "readiness_report": readiness_report,
        "safety": safety,
        "active_projection": projection_sync,
        "errors": [],
        "warnings": warnings,
        "mutation_boundary": (
            "workflow create --template/--preset creates a draft workflow from deterministic template data, "
            "writes planning/PLAN_DRAFT.md, preserves inactive PLAN.md, and updates current workflow only after safety checks pass."
        ),
    }


def template_command_exit_code(result: Mapping[str, Any]) -> int:
    return 0 if result.get("ok") is True else 2


def format_template_list_text(result: Mapping[str, Any]) -> str:
    lines = [f"loopplane template list: {result.get('status', 'unknown')}"]
    if result.get("ok") is True:
        templates = result.get("templates")
        if isinstance(templates, Sequence) and templates and not isinstance(templates, (str, bytes)):
            lines.append("templates:")
            for template in templates:
                if isinstance(template, Mapping):
                    lines.append(
                        f"  - {template.get('id')}@{template.get('version')}: {template.get('title')}"
                    )
        else:
            lines.append("templates: none")
    _append_messages(lines, result)
    return "\n".join(lines) + "\n"


def format_template_show_text(result: Mapping[str, Any]) -> str:
    lines = [f"loopplane template show: {result.get('status', 'unknown')}"]
    template = result.get("template")
    if isinstance(template, Mapping):
        lines.extend(
            [
                f"id: {template.get('id')}",
                f"version: {template.get('version')}",
                f"title: {template.get('title')}",
                f"root: {template.get('root')}",
            ]
        )
        inputs = result.get("inputs")
        if isinstance(inputs, Mapping):
            lines.append("inputs:")
            for name, spec in inputs.items():
                if isinstance(spec, Mapping):
                    required = "required" if spec.get("required") else "optional"
                    default = f" default={spec.get('default')!r}" if "default" in spec else ""
                    lines.append(f"  - {name}: {spec.get('type')} {required}{default}")
    _append_messages(lines, result)
    return "\n".join(lines) + "\n"


def format_template_doctor_text(result: Mapping[str, Any]) -> str:
    lines = [f"loopplane template doctor: {result.get('status', 'unknown')}"]
    template = result.get("template")
    if isinstance(template, Mapping):
        lines.append(f"template: {template.get('id')}@{template.get('version')}")
    _append_messages(lines, result)
    return "\n".join(lines) + "\n"


def format_template_render_text(result: Mapping[str, Any]) -> str:
    lines = [f"loopplane template render: {result.get('status', 'unknown')}"]
    template = result.get("template")
    if isinstance(template, Mapping):
        lines.append(f"template: {template.get('id')}@{template.get('version')}")
    rendered = result.get("rendered")
    if isinstance(rendered, Mapping):
        lines.append("rendered:")
        for target, record in rendered.items():
            if isinstance(record, Mapping):
                lines.append(f"  - {target}: {record.get('bytes', 0)} bytes")
    output_files = result.get("output_files")
    if isinstance(output_files, Sequence) and output_files and not isinstance(output_files, (str, bytes)):
        lines.append("output_files:")
        lines.extend(f"  - {path}" for path in output_files)
    _append_messages(lines, result)
    return "\n".join(lines) + "\n"


def format_template_instance_text(result: Mapping[str, Any]) -> str:
    lines = [f"loopplane template instance: {result.get('status', 'unknown')}"]
    instance = result.get("instance")
    if isinstance(instance, Mapping):
        template = instance.get("template")
        lines.append(f"workflow_id: {instance.get('workflow_id')}")
        if isinstance(template, Mapping):
            lines.append(f"template: {template.get('id')}@{template.get('version')}")
    preset = result.get("preset")
    if isinstance(preset, Mapping):
        lines.append(f"template: {preset.get('template')}@{preset.get('template_version')}")
    output = result.get("output")
    if output:
        lines.append(f"output: {output}")
    _append_messages(lines, result)
    return "\n".join(lines) + "\n"


def show_template(template_id: str, *, template_dir: Path | str | None = None) -> dict[str, Any]:
    try:
        template = load_workflow_template(template_id, template_dir=template_dir)
    except TemplatePresetError as error:
        return _template_result(False, error.status, errors=error.errors, warnings=error.warnings)
    manifest = _template_manifest(template)
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": True,
        "status": "shown",
        "template": _template_summary(template),
        "inputs": manifest.get("inputs") or {},
        "renders": manifest.get("renders") or {},
        "default_policy": manifest.get("default_policy") or {},
        "errors": [],
        "warnings": [],
    }


def show_template_instance(project_root: Path | str, *, workflow_id: str | None = None) -> dict[str, Any]:
    try:
        project, paths, selected_workflow_id = _selected_workflow_paths(project_root, workflow_id)
        instance_path = paths.workflow_root / "template_instance.json"
        if not instance_path.is_file():
            return _template_result(
                False,
                "template_instance_missing",
                errors=[f"{_project_relative(project, instance_path)}: template_instance.json is missing"],
            )
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _template_result(False, "template_instance_invalid", errors=[str(error)])
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": True,
        "status": "shown",
        "project_root": project.as_posix(),
        "workflow_id": selected_workflow_id,
        "instance_file": _project_relative(project, instance_path),
        "instance": instance,
        "errors": [],
        "warnings": [],
    }


def extract_preset_from_template_instance(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    output: Path | str | None = None,
) -> dict[str, Any]:
    shown = show_template_instance(project_root, workflow_id=workflow_id)
    if shown.get("ok") is not True:
        return shown
    instance = shown["instance"]
    template = instance.get("template") if isinstance(instance, Mapping) else None
    if not isinstance(template, Mapping):
        return _template_result(False, "template_instance_invalid", errors=["template_instance.json has no template object"])
    preset = {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "kind": PRESET_KIND,
        "name": str(instance.get("workflow_id") or "extracted-preset"),
        "template": str(template.get("id") or ""),
        "template_version": str(template.get("version") or ""),
        "template_lock": {
            "source": str(template.get("source") or "unknown"),
            "sha256": str(template.get("sha256") or ""),
        },
        "inputs": dict(instance.get("inputs") or {}),
        "policy": dict(instance.get("policy") or {}),
    }
    output_path = None
    if output is not None:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(preset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": True,
        "status": "preset_extracted",
        "project_root": shown.get("project_root"),
        "workflow_id": shown.get("workflow_id"),
        "preset": preset,
        "output": output_path.as_posix() if output_path else None,
        "errors": [],
        "warnings": [],
    }


def _load_workflow_create_context(project: Path, workspace_file: Path, registry_file: Path, current_file: Path) -> dict[str, Any]:
    loopplane_dir = project / ".loopplane"
    if not project.exists():
        return workflow_module._failure(
            status="missing_project",
            project=project,
            errors=[f"Project path does not exist: {project}"],
            recovery_actions=["Create the project directory and initialize LoopPlane first."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )
    if not project.is_dir():
        return workflow_module._failure(
            status="invalid_project",
            project=project,
            errors=[f"Project path is not a directory: {project}"],
            recovery_actions=["Pass an existing LoopPlane project directory to loopplane workflow create."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )
    if not loopplane_dir.exists() or not workspace_file.is_file():
        return workflow_module._failure(
            status="missing_workspace",
            project=project,
            errors=["No project-local .loopplane workspace identity was found."],
            recovery_actions=["Run loopplane init --project <project> --brief <brief> first."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )
    if not registry_file.is_file():
        return workflow_module._failure(
            status="missing_registry",
            project=project,
            errors=["Project is missing authoritative .loopplane/workflow_registry.json."],
            recovery_actions=["Restore .loopplane/workflow_registry.json from a checkpoint or backup."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )
    try:
        workspace = workflow_module._load_json_object(workspace_file)
        registry = workflow_module._load_json_object(registry_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return workflow_module._failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=[str(error)],
            recovery_actions=["Repair project-local .loopplane metadata before creating workflow history."],
            extra=workflow_module._file_context(workspace_file, registry_file, current_file),
        )
    workspace_errors = workflow_module._workspace_errors(workspace)
    if workspace_errors:
        return workflow_module._failure(
            status="invalid_workspace_metadata",
            project=project,
            errors=workspace_errors,
            recovery_actions=["Repair .loopplane/workspace.json so it matches the v1.6 schema."],
            extra={**workflow_module._file_context(workspace_file, registry_file, current_file), "workspace_id": str(workspace.get("workspace_id") or "")},
        )
    registry_errors = workflow_module._registry_errors(registry, workspace=workspace)
    if registry_errors:
        return workflow_module._failure(
            status="malformed_registry",
            project=project,
            errors=registry_errors,
            recovery_actions=["Repair .loopplane/workflow_registry.json before creating workflow history."],
            extra={**workflow_module._file_context(workspace_file, registry_file, current_file), "workspace_id": workflow_module._workspace_id(workspace, registry)},
        )
    workflows = [dict(record) for record in registry.get("workflows", [])]
    warnings: list[str] = []
    current_workflow_id: str | None = None
    if current_file.exists():
        try:
            current_pointer = workflow_module._load_json_object(current_file)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return workflow_module._failure(
                status="malformed_current_pointer",
                project=project,
                errors=[f"Unable to read .loopplane/current_workflow.json: {error}"],
                recovery_actions=["Repair .loopplane/current_workflow.json before creating workflow history."],
                extra={**workflow_module._file_context(workspace_file, registry_file, current_file), "workspace_id": workflow_module._workspace_id(workspace, registry)},
            )
        pointer_malformed, pointer_mismatch = workflow_module._current_pointer_error_groups(
            current_pointer,
            registry=registry,
            workflows=workflows,
        )
        if pointer_malformed or pointer_mismatch:
            return workflow_module._failure(
                status="current_pointer_mismatch" if pointer_mismatch else "malformed_current_pointer",
                project=project,
                errors=[*(pointer_malformed or []), *(pointer_mismatch or [])],
                recovery_actions=["Repair .loopplane/current_workflow.json so it references a registered workflow."],
                extra={**workflow_module._file_context(workspace_file, registry_file, current_file), "workspace_id": workflow_module._workspace_id(workspace, registry)},
            )
        current_workflow_id = str(current_pointer.get("current_workflow_id") or "")
    else:
        warnings.append(".loopplane/current_workflow.json is missing; workflow create will create a new pointer after safety checks pass.")
    return {
        "ok": True,
        "workspace": workspace,
        "registry": registry,
        "workflows": workflows,
        "current_workflow_id": current_workflow_id,
        "warnings": warnings,
    }


def _load_template_preset_inputs(
    *,
    template_id: str | None,
    preset_path: Path | str | None,
    overrides: Mapping[str, Any] | None,
    template_dir: Path | str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    preset = load_workflow_preset(preset_path) if preset_path is not None else None
    resolved_template_id = str(template_id or (preset.get("template") if preset else "") or "").strip()
    if not resolved_template_id:
        raise TemplatePresetError("template_not_found", ["template id is required unless preset supplies template"])
    if preset is not None and template_id and str(preset.get("template")) != str(template_id):
        raise TemplatePresetError(
            "template_preset_conflict",
            [f"preset template {preset.get('template')!r} does not match --template {template_id!r}"],
        )
    template = load_workflow_template(resolved_template_id, template_dir=template_dir)
    if preset is not None:
        preset_version = preset.get("template_version")
        if preset_version and str(preset_version) != str(template["version"]):
            raise TemplatePresetError(
                "template_version_mismatch",
                [f"preset expects {resolved_template_id}@{preset_version}, found {template['version']}"],
            )
        lock = preset.get("template_lock")
        if isinstance(lock, Mapping) and lock.get("sha256"):
            if str(lock["sha256"]) != str(template["_template_sha256"]):
                raise TemplatePresetError(
                    "template_lock_mismatch",
                    [f"preset template_lock sha256 does not match template {resolved_template_id}"],
                )
    policy = _policy_values(template, preset)
    inputs = merge_template_inputs(template, preset, overrides)
    inputs.update(policy)
    return template, preset, inputs


def _resolve_template_root(template_id: str, *, template_dir: Path | None) -> Path:
    candidates: list[Path] = []
    if template_dir is not None:
        raw = template_dir.expanduser().resolve()
        candidates.append(raw if (raw / "template.json").is_file() else raw / template_id)
    candidates.append(BUILTIN_TEMPLATE_ROOT / template_id)
    for candidate in candidates:
        if (candidate / "template.json").is_file():
            return candidate.resolve()
    raise TemplatePresetError("template_not_found", [f"workflow template not found: {template_id}"])


def _load_template_from_root(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    manifest_path = root / "template.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TemplatePresetError("template_manifest_invalid", [f"{manifest_path}: unable to read template: {error}"]) from error
    if not isinstance(manifest, dict):
        raise TemplatePresetError("template_manifest_invalid", [f"{manifest_path}: template manifest must be an object"])
    errors: list[str] = []
    if manifest.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
        errors.append("template schema_version must be '1.0'")
    if manifest.get("kind") != TEMPLATE_KIND:
        errors.append(f"template kind must be {TEMPLATE_KIND!r}")
    template_id = manifest.get("id")
    if not isinstance(template_id, str) or not TEMPLATE_ID_RE.match(template_id):
        errors.append("template id must be a valid template id")
    if not isinstance(manifest.get("version"), str) or not manifest.get("version"):
        errors.append("template version is required")
    if not isinstance(manifest.get("inputs"), Mapping):
        errors.append("template inputs must be an object")
    if not isinstance(manifest.get("renders"), Mapping):
        errors.append("template renders must be an object")
    if errors:
        raise TemplatePresetError("template_manifest_invalid", [f"{manifest_path}: {error}" for error in errors])
    manifest["_template_root"] = root.as_posix()
    manifest["_template_source"] = "builtin" if _is_relative_to(root, BUILTIN_TEMPLATE_ROOT) else "local"
    manifest["_template_sha256"] = _template_directory_sha256(root)
    return manifest


def _template_manifest(template: Mapping[str, Any]) -> Mapping[str, Any]:
    return template


def _template_root(template: Mapping[str, Any]) -> Path:
    value = template.get("_template_root")
    if not isinstance(value, str) or not value:
        raise TemplatePresetError("template_manifest_invalid", ["loaded template is missing _template_root"])
    return Path(value)


def _input_definitions(template: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    inputs = template.get("inputs")
    if not isinstance(inputs, Mapping):
        raise TemplatePresetError("template_manifest_invalid", ["template inputs must be an object"])
    definitions: dict[str, Mapping[str, Any]] = {}
    for name, spec in inputs.items():
        if not isinstance(spec, Mapping):
            raise TemplatePresetError("template_manifest_invalid", [f"input {name!r} must be an object"])
        definitions[str(name)] = spec
    return definitions


def _resolve_derived_inputs(definitions: Mapping[str, Mapping[str, Any]], values: dict[str, Any], explicit: set[str]) -> None:
    for name, spec in definitions.items():
        if name in explicit:
            continue
        derived = spec.get("derived")
        if not isinstance(derived, str):
            continue
        match = DERIVED_RE.match(derived.strip())
        if not match:
            raise TemplatePresetError("input_validation_failed", [f"{name}: unsupported derived expression {derived!r}"])
        function, source = match.groups()
        source_value = str(values.get(source) or "")
        if not source_value:
            continue
        if function == "slug":
            values[name] = _slug(source_value)
        elif function == "lower":
            values[name] = source_value.lower()
        elif function == "upper":
            values[name] = source_value.upper()


def _render_string_defaults(definitions: Mapping[str, Mapping[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    rendered = dict(values)
    for _ in range(4):
        changed = False
        for name, value in list(rendered.items()):
            if not isinstance(value, str) or "{{" not in value:
                continue
            new_value = _replace_variables(value, rendered)
            if new_value != value:
                rendered[name] = new_value
                changed = True
        if not changed:
            break
    return rendered


def _validate_inputs(definitions: Mapping[str, Mapping[str, Any]], values: dict[str, Any]) -> None:
    errors: list[str] = []
    for name, spec in definitions.items():
        if spec.get("required") is True and name not in values:
            errors.append(f"missing required input: {name}")
            continue
        if name not in values:
            continue
        try:
            values[name] = _coerce_value(values[name], str(spec.get("type") or "string"))
        except ValueError as error:
            errors.append(f"{name}: {error}")
            continue
        enum = spec.get("enum")
        if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)) and values[name] not in enum:
            errors.append(f"{name}: value must be one of {list(enum)!r}")
        if isinstance(values[name], (int, float)):
            minimum = spec.get("minimum")
            maximum = spec.get("maximum")
            if isinstance(minimum, (int, float)) and values[name] < minimum:
                errors.append(f"{name}: value must be >= {minimum}")
            if isinstance(maximum, (int, float)) and values[name] > maximum:
                errors.append(f"{name}: value must be <= {maximum}")
        if isinstance(values[name], str):
            min_length = spec.get("minLength")
            max_length = spec.get("maxLength")
            if isinstance(min_length, int) and len(values[name]) < min_length:
                errors.append(f"{name}: value length must be >= {min_length}")
            if isinstance(max_length, int) and len(values[name]) > max_length:
                errors.append(f"{name}: value length must be <= {max_length}")
            if PATH_LIKE_INPUT_RE.search(name):
                path_error = _path_input_error(name, values[name])
                if path_error:
                    errors.append(path_error)
    unknown = sorted(set(values) - set(definitions) - {"planner_mode", "self_expansion", "validation_strictness", "final_review", "activation_required"})
    if unknown:
        errors.append(f"unknown template input(s): {', '.join(unknown)}")
    if errors:
        raise TemplatePresetError("input_validation_failed", errors)


def _coerce_value(value: Any, kind: str) -> Any:
    if kind == "string":
        return str(value)
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError("expected integer")
        return int(value)
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("expected number")
        return float(value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError("expected boolean")
    if kind in {"array", "object"}:
        if isinstance(value, str):
            value = json.loads(value)
        if kind == "array" and not isinstance(value, list):
            raise ValueError("expected array")
        if kind == "object" and not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    raise ValueError(f"unsupported type {kind!r}")


def _render_text(text: str, variables: Mapping[str, Any]) -> str:
    text = _render_optional_blocks(text, variables)
    return _replace_variables(text, variables)


def _render_optional_blocks(text: str, variables: Mapping[str, Any]) -> str:
    output: list[str] = []
    include = True
    in_block = False
    for line in text.splitlines(keepends=True):
        if_match = IF_RE.search(line)
        endif_match = ENDIF_RE.search(line)
        if if_match and endif_match:
            raise TemplatePresetError("render_failed", ["optional block markers cannot share a line"])
        if if_match:
            if in_block:
                raise TemplatePresetError("render_failed", ["nested optional blocks are not supported"])
            key = if_match.group(1)
            if key not in variables:
                raise TemplatePresetError("render_failed", [f"optional block variable {key!r} is missing"])
            value = variables[key]
            if not isinstance(value, bool):
                raise TemplatePresetError("render_failed", [f"optional block variable {key!r} must be boolean"])
            in_block = True
            include = value
            continue
        if endif_match:
            if not in_block:
                raise TemplatePresetError("render_failed", ["unmatched loopplane:endif marker"])
            in_block = False
            include = True
            continue
        if include:
            output.append(line)
    if in_block:
        raise TemplatePresetError("render_failed", ["unclosed loopplane optional block"])
    return "".join(output)


def _replace_variables(text: str, variables: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise TemplatePresetError("render_failed", [f"template variable {key!r} is missing"])
        value = variables[key]
        if isinstance(value, (dict, list)):
            raise TemplatePresetError("render_failed", [f"template variable {key!r} cannot render object/array values"])
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return VARIABLE_RE.sub(replace, text)


def _workflow_context(project: Path, paths: WorkflowPaths, *, workflow_id: str, generated_at: str) -> dict[str, Any]:
    context = dict(paths.values)
    context.update(
        {
            "workflow_id": workflow_id,
            "generated_at": generated_at,
            "plan_version": 1,
            "active": "false",
            "workflow_path_lines": path_lines(paths),
            "plan_draft_file": f"{paths.value('planning_dir')}/PLAN_DRAFT.md",
            "project_root": ".",
            "project_root_abs": project.as_posix(),
        }
    )
    return context


def _preview_workflow_context(project_root: Path | str | None) -> dict[str, Any]:
    project = Path(project_root or ".").expanduser().resolve()
    workflow_id = "wf_20000101_00000000"
    workflow_config = _workflow_config(workflow_id, "2000-01-01T00:00:00Z", {}, layout="canonical_v1_6")
    paths = WorkflowPaths.from_config(project, workflow_config)
    return _workflow_context(project, paths, workflow_id=workflow_id, generated_at="2000-01-01T00:00:00Z")


def _policy_values(template: Mapping[str, Any], preset: Mapping[str, Any] | None) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    default_policy = template.get("default_policy")
    if isinstance(default_policy, Mapping):
        policy.update(default_policy)
    if preset is not None and isinstance(preset.get("policy"), Mapping):
        policy.update(dict(preset["policy"]))
    policy.setdefault("planner_mode", "deterministic")
    policy.setdefault("self_expansion", "bounded")
    policy.setdefault("validation_strictness", "medium")
    policy.setdefault("final_review", True)
    policy.setdefault("activation_required", True)
    return policy


def _inspect_rendered_plan(plan_text: str, *, workflow_id: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "PLAN_DRAFT.md"
        path.write_text(plan_text, encoding="utf-8")
        return inspect_plan_draft(path, workflow_id=workflow_id)


def _write_rendered_workflow_files(project: Path, paths: WorkflowPaths, rendered: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    render_records = rendered.get("rendered")
    if not isinstance(render_records, Mapping):
        raise ValueError("rendered payload is missing render records")
    target_paths = {
        "project_brief": paths.brief_file,
        "plan_draft": paths.planning_dir / "PLAN_DRAFT.md",
        "shared_context": paths.shared_context_file,
    }
    written: dict[str, dict[str, Any]] = {}
    for target, path in target_paths.items():
        record = render_records.get(target)
        if not isinstance(record, Mapping):
            continue
        text = str(record.get("text") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        relative = _project_relative(project, path)
        data = text.encode("utf-8")
        written[relative] = {"sha256": _sha256_bytes(data), "bytes": len(data)}
    return written


def _write_template_instance(
    project: Path,
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    created_at: str,
    template: Mapping[str, Any],
    preset: Mapping[str, Any] | None,
    inputs: Mapping[str, Any],
    rendered_files: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    instance = {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "kind": INSTANCE_KIND,
        "workflow_id": workflow_id,
        "created_at": created_at,
        "created_by": "loopplane workflow create --template",
        "template": {
            "id": str(template["id"]),
            "version": str(template["version"]),
            "source": str(template.get("_template_source") or "unknown"),
            "root": _template_root_record(template),
            "sha256": str(template["_template_sha256"]),
        },
        "preset": _preset_instance_record(project, preset),
        "inputs": dict(inputs),
        "policy": _policy_values(template, preset),
        "rendered_files": {str(path): dict(record) for path, record in rendered_files.items()},
        "activation": {
            "direct_activation": False,
            "requires_activate_plan": True,
        },
    }
    path = paths.workflow_root / "template_instance.json"
    path.write_text(json.dumps(instance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return instance


def _write_template_readiness_report(
    project: Path,
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    workflow_config: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    plan_draft_path = paths.planning_dir / "PLAN_DRAFT.md"
    structural_report = inspect_plan_draft(plan_draft_path, workflow_id=workflow_id)
    planning_config = workflow_config.get("planning")
    if not isinstance(planning_config, Mapping):
        planning_config = {}
    readiness_report = _readiness_report(
        workflow_id=workflow_id,
        run_id=_new_run_id("template_create"),
        runner_id="workflow_template",
        adapter="template_preset",
        adapter_exit_code=0,
        draft_source="template",
        plan_draft_path=plan_draft_path,
        plan_file=_project_relative(project, plan_draft_path),
        structural_report=structural_report,
        auditor_required=bool(planning_config.get("auditor_required", False)),
        approval_enabled=_interactive_approval_enabled(paths),
        generated_at=generated_at,
    )
    readiness_path = paths.planning_dir / READINESS_REPORT_FILENAME
    readiness_path.write_text(json.dumps(readiness_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return readiness_report


def _preset_instance_record(project: Path, preset: Mapping[str, Any] | None) -> dict[str, Any]:
    if preset is None:
        return {"name": None, "path": None, "sha256": None}
    preset_path = preset.get("_preset_path")
    path_value = None
    if isinstance(preset_path, str) and preset_path:
        path_value = _portable_path(project, Path(preset_path))
    return {
        "name": preset.get("name"),
        "path": path_value,
        "sha256": preset.get("_preset_sha256"),
    }


def _rendered_file_preview(project: Path, paths: WorkflowPaths, rendered: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    target_paths = {
        "project_brief": paths.brief_file,
        "plan_draft": paths.planning_dir / "PLAN_DRAFT.md",
        "shared_context": paths.shared_context_file,
    }
    records = rendered.get("rendered")
    preview: dict[str, dict[str, Any]] = {}
    if not isinstance(records, Mapping):
        return preview
    for target, path in target_paths.items():
        record = records.get(target)
        if isinstance(record, Mapping):
            preview[_project_relative(project, path)] = {
                "sha256": str(record.get("sha256") or ""),
                "bytes": int(record.get("bytes") or 0),
            }
    return preview


def _brief_text(rendered: Mapping[str, Any], template: Mapping[str, Any], inputs: Mapping[str, Any]) -> str:
    records = rendered.get("rendered")
    if isinstance(records, Mapping) and isinstance(records.get("project_brief"), Mapping):
        return str(records["project_brief"].get("text") or "")
    return _summary_one_line(template, None, inputs)


def _workflow_name(template: Mapping[str, Any], preset: Mapping[str, Any] | None, inputs: Mapping[str, Any]) -> str:
    name = ""
    if preset is not None:
        name = str(preset.get("name") or "").strip()
    if not name:
        topic = inputs.get("topic") or inputs.get("target_workflow")
        if topic:
            name = f"{template.get('title')}: {topic}"
    if not name:
        name = str(template.get("title") or template.get("id") or "Template workflow")
    return workflow_module._truncate_workflow_name(name, limit=96)


def _summary_one_line(template: Mapping[str, Any], preset: Mapping[str, Any] | None, inputs: Mapping[str, Any]) -> str:
    if preset is not None and preset.get("description"):
        return workflow_module._summary_from_brief(str(preset["description"]))
    target = inputs.get("topic") or inputs.get("target_workflow") or template.get("title") or template.get("id")
    return f"Workflow from template {template.get('id')} for {target}."


def _template_summary(template: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": template.get("id"),
        "version": template.get("version"),
        "title": template.get("title"),
        "description": template.get("description"),
        "root": _template_root_record(template),
        "source": template.get("_template_source"),
        "sha256": template.get("_template_sha256"),
    }


def _template_root_record(template: Mapping[str, Any]) -> str:
    root = _template_root(template)
    repo_root = Path(__file__).resolve().parents[1]
    if _is_relative_to(root, repo_root):
        return root.relative_to(repo_root).as_posix()
    return root.as_posix()


def _preset_summary(preset: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if preset is None:
        return None
    return {
        "name": preset.get("name"),
        "template": preset.get("template"),
        "template_version": preset.get("template_version"),
        "path": preset.get("_preset_path"),
        "sha256": preset.get("_preset_sha256"),
    }


def _selected_workflow_paths(project_root: Path | str, workflow_id: str | None) -> tuple[Path, WorkflowPaths, str]:
    project = Path(project_root).expanduser().resolve()
    if workflow_id:
        result = workflow_module.show_workflow(project, workflow_id)
        if result.get("ok") is not True:
            raise ValueError("; ".join(str(error) for error in result.get("errors", [])))
        workflow = result.get("workflow")
    else:
        result = workflow_module.current_workflow(project)
        if result.get("ok") is not True:
            raise ValueError("; ".join(str(error) for error in result.get("errors", [])))
        workflow = result.get("workflow")
    if not isinstance(workflow, Mapping):
        raise ValueError("unable to resolve workflow")
    selected_workflow_id = str(workflow.get("workflow_id") or workflow_id or "")
    config_path = project / _workflow_config_path_value(workflow)
    workflow_config = json.loads(config_path.read_text(encoding="utf-8"))
    return project, WorkflowPaths.from_config(project, workflow_config), selected_workflow_id


def _workflow_config_path_value(workflow: Mapping[str, Any]) -> str:
    config_path = workflow.get("workflow_config_file")
    if isinstance(config_path, str) and config_path.strip():
        return config_path.strip()
    registry_record = workflow.get("registry_record")
    if isinstance(registry_record, Mapping):
        config_path = registry_record.get("workflow_config_file")
        if isinstance(config_path, str) and config_path.strip():
            return config_path.strip()
    workflow_root = str(workflow.get("workflow_root") or "").strip().rstrip("/")
    if not workflow_root or workflow_root == ".loopplane":
        return ".loopplane/config/workflow.json"
    return f"{workflow_root}/config/workflow.json"


def _safe_template_child(root: Path, child: str) -> Path:
    if PurePosixPath(child).is_absolute() or ".." in PurePosixPath(child).parts:
        raise TemplatePresetError("template_manifest_invalid", [f"{child}: template path must stay under template root"])
    path = (root / child).resolve()
    if not _is_relative_to(path, root):
        raise TemplatePresetError("template_manifest_invalid", [f"{child}: template path escapes template root"])
    return path


def _template_directory_sha256(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        resolved = path.resolve()
        if not _is_relative_to(resolved, root):
            raise TemplatePresetError("template_manifest_invalid", [f"{path}: template file escapes template root"])
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _path_input_error(name: str, value: str) -> str | None:
    path = PurePosixPath(value)
    if path.is_absolute():
        return f"{name}: path must be project-relative"
    if ".." in path.parts:
        return f"{name}: path must not contain parent traversal"
    return None


def _preview_filename(target: str) -> str:
    return {
        "project_brief": "PROJECT_BRIEF.md",
        "plan_draft": "PLAN_DRAFT.md",
        "shared_context": "SHARED_CONTEXT.md",
    }.get(target, f"{target}.txt")


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip().lower())
    text = re.sub(r"_+", "_", text).strip("._-")
    return text or "workflow"


def _project_relative(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _portable_path(project: Path, path: Path) -> str | None:
    resolved = path.expanduser().resolve()
    for root in (project.resolve(), Path(__file__).resolve().parents[1]):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _template_result(ok: bool, status: str, *, errors: Sequence[str] = (), warnings: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "errors": list(errors),
        "warnings": list(warnings),
    }


def _append_messages(lines: list[str], result: Mapping[str, Any]) -> None:
    for key in ("warnings", "errors", "recovery_actions"):
        values = result.get(key)
        if isinstance(values, Sequence) and values and not isinstance(values, (str, bytes)):
            lines.append(f"{key}:")
            lines.extend(f"  - {value}" for value in values)
