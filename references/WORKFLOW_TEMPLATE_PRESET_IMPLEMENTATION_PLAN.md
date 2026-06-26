# Workflow Template And Preset Implementation Plan

## Purpose

LoopPlane currently supports durable workflow instances, workflow history,
planning, activation, execution, validation, read models, and dashboard
visibility. The missing layer is a lightweight way to repeatedly create similar
workflows with stable structure.

This plan proposes a first-class workflow template and preset feature. The
feature should let users and agents create reproducible workflow drafts by
editing a small preset file instead of relying on free-form prompt-to-plan
translation every time.

The immediate goals are:

- make repeated workflow creation deterministic for common task families;
- let agents migrate and adapt workflow setups by editing small JSON presets;
- keep implementation lightweight and easy to review;
- avoid a plugin system for now;
- preserve existing LoopPlane authority rules: generated plans are drafts until
  normal activation checks pass.

## Non-Goals

Do not implement these in the first pass:

- plugin marketplace or plugin installation;
- arbitrary Python, shell, or JavaScript hooks in templates;
- dynamic validator or adapter loading from templates;
- complex templating languages such as Jinja2;
- direct creation of active `PLAN.md` from a template;
- planner free-form rewriting of a template-generated workflow skeleton;
- remote template fetching.

The MVP should be local, deterministic, file-based, and schema-validated.
It should reuse the existing workflow lifecycle and activation model instead of
creating a second way to execute plans.

## Core Concepts

### Template

A template defines a reusable workflow structure:

- required inputs and defaults;
- rendered files such as `PROJECT_BRIEF.md`, `PLAN_DRAFT.md`, and optionally
  `SHARED_CONTEXT.md`;
- phase and task skeleton;
- objective gates;
- validation strategies;
- optional sections controlled by boolean inputs.

A template is not a runtime extension. It is data used to generate workflow
draft files.

### Preset

A preset defines a specific use of a template:

- template id and expected version;
- simple input values;
- optional policy choices;
- optional template version lock;
- human-readable name and description.

The preset is the primary migration artifact. An agent should be able to copy a
successful preset, change a few values, and create a similar workflow.

### Renderer

The renderer merges template defaults, preset inputs, and CLI overrides, then
renders template files into a workflow root.

The renderer should be deterministic. Given the same template bytes, preset
bytes, CLI overrides, and LoopPlane version, it should produce the same rendered
files.

### Template Instance Record

Each workflow created from a template should record provenance in:

```text
.loopplane/workflows/<workflow_id>/template_instance.json
```

For compatibility-flat layouts, the path can be:

```text
.loopplane/template_instance.json
```

This record is needed for reproducibility, migration, debugging, and dashboard
display.

## Current Architecture Fit

The feature should integrate with existing LoopPlane components instead of
creating a parallel workflow path.

Relevant existing files:

- `runtime/init_workflow.py`
  - `materialize_canonical_workflow_files(...)` creates canonical workflow
    roots without directly mutating workspace-level truth.
- `runtime/workflows.py`
  - `create_workflow(...)` currently creates new canonical workflow histories
    from a brief.
  - workflow registry/current-pointer handling should remain centralized here.
- `runtime/planning.py`
  - `inspect_plan_draft(...)` and activation logic already validate draft plan
    structure.
  - template-created plans should go through these existing checks.
- `scripts/loopplane`
  - add CLI entry points here.
- `templates/`
  - currently contains general document and prompt templates.
  - workflow templates can live under `templates/workflows/`.
- `runtime/schema_validation.py`
  - add schemas for template, preset, and instance records.

The template feature should write draft files and metadata, then let the normal
activation path promote the draft to `PLAN.md`.

## Proposed Repository Layout

Builtin workflow templates:

```text
templates/workflows/
  research-topic-exploration/
    template.json
    PROJECT_BRIEF.md.tpl
    PLAN_DRAFT.md.tpl
    SHARED_CONTEXT.md.tpl
    examples/
      minimal.preset.json
      publication_grade.preset.json

  dashboard-performance-investigation/
    template.json
    PROJECT_BRIEF.md.tpl
    PLAN_DRAFT.md.tpl
    SHARED_CONTEXT.md.tpl
    examples/
      local_dashboard_latency.preset.json
```

Runtime schemas:

```text
runtime/schemas/workflow_template.schema.json
runtime/schemas/workflow_preset.schema.json
runtime/schemas/template_instance.schema.json
```

Implementation module:

```text
runtime/template_presets.py
```

Project-local presets:

```text
.loopplane/presets/
  topic_3_publication.preset.json
```

Rendered workflow provenance:

```text
.loopplane/workflows/<workflow_id>/template_instance.json
```

## Template Manifest

Use a small JSON manifest. Avoid embedding heavy plan content in the manifest;
keep markdown templates as separate files so agents can inspect and edit them
easily.

Example:

```json
{
  "schema_version": "1.0",
  "kind": "loopplane.workflow_template",
  "id": "research-topic-exploration",
  "version": "0.1.0",
  "title": "Research Topic Exploration",
  "description": "Create a bounded research workflow with experiment, analysis, and publication gates.",
  "inputs": {
    "topic": {
      "type": "string",
      "required": true,
      "description": "Short topic label or research target."
    },
    "topic_slug": {
      "type": "string",
      "required": false,
      "derived": "slug(topic)"
    },
    "target_standard": {
      "type": "string",
      "default": "publication_grade",
      "enum": ["exploratory", "internal_report", "publication_grade"]
    },
    "max_exploration_runs": {
      "type": "integer",
      "default": 50,
      "minimum": 1,
      "maximum": 1000
    },
    "final_report_path": {
      "type": "string",
      "default": "reports/{{topic_slug}}_final.md"
    },
    "enable_literature_review": {
      "type": "boolean",
      "default": true
    },
    "enable_ablation": {
      "type": "boolean",
      "default": true
    }
  },
  "renders": {
    "project_brief": "PROJECT_BRIEF.md.tpl",
    "plan_draft": "PLAN_DRAFT.md.tpl",
    "shared_context": "SHARED_CONTEXT.md.tpl"
  },
  "locked_sections": [
    "phase_structure",
    "task_ids",
    "dependency_graph",
    "objective_gates"
  ],
  "default_policy": {
    "planner_mode": "deterministic",
    "activation_required": true,
    "self_expansion": "bounded",
    "validation_strictness": "high"
  }
}
```

### Input Types

MVP-supported input types:

- `string`;
- `integer`;
- `number`;
- `boolean`;
- `array`;
- `object`.

MVP validation should support:

- `required`;
- `default`;
- `enum`;
- `minimum`;
- `maximum`;
- `minLength`;
- `maxLength`;
- basic path safety for path-like strings.

Avoid complex JSON Schema evaluation in the renderer. Use a straightforward
validator with clear error messages.

### Validation Layers

Use two validation layers instead of duplicating full schema logic inside the
renderer:

- `runtime/schema_validation.py` should own full JSON Schema validation for
  template, preset, and instance record files.
- `runtime/template_presets.py` should perform lightweight runtime checks needed
  for good CLI errors: required inputs, type checks, enum membership, path
  safety, missing render targets, and unknown variables.
- `loopplane template doctor` should run the strictest validation path:
  schema validation, render-file existence checks, example preset checks,
  template directory safety checks, and deterministic-render checks.
- normal `template render` and `workflow create --preset` should run the
  lightweight checks plus plan inspection. They may include schema-validation
  diagnostics when `jsonschema` is available, but they should not require a
  heavyweight dependency beyond the repository's existing validation setup.

### Derived Inputs

Support only a very small derived-input set in MVP:

- `slug(input_name)`;
- `lower(input_name)`;
- `upper(input_name)`.

Derived values should be resolved before rendering. If a preset explicitly
provides a derived value, explicit value should win unless `locked: true` is
set on the input.

## Preset Format

Preset files should be small, flat, and easy for agents to migrate.

Example:

```json
{
  "schema_version": "1.0",
  "kind": "loopplane.workflow_preset",
  "name": "topic-3-publication-run",
  "description": "Publication-grade research workflow for topic 3.",
  "template": "research-topic-exploration",
  "template_version": "0.1.0",
  "template_lock": {
    "source": "builtin",
    "sha256": "optional-template-manifest-or-directory-hash"
  },
  "inputs": {
    "topic": "topic_3",
    "target_standard": "publication_grade",
    "max_exploration_runs": 80,
    "final_report_path": "reports/topic_3_final.md",
    "enable_literature_review": true,
    "enable_ablation": true
  },
  "policy": {
    "planner_mode": "deterministic",
    "self_expansion": "bounded",
    "validation_strictness": "high",
    "final_review": true
  }
}
```

Preset design rules:

- keep primary values under `inputs`;
- keep operational choices under `policy`;
- avoid project-local absolute paths;
- avoid environment-specific runner commands;
- allow agents to modify preset JSON without reading every template file;
- keep unknown fields as warnings initially, not fatal errors, unless
  `--strict` is used.

## Template Instance Record

Write a machine-readable provenance file for every template-created workflow.

Example:

```json
{
  "schema_version": "1.0",
  "kind": "loopplane.template_instance",
  "workflow_id": "wf_20260625_abcd1234",
  "created_at": "2026-06-25T00:00:00Z",
  "created_by": "loopplane workflow create --template",
  "template": {
    "id": "research-topic-exploration",
    "version": "0.1.0",
    "source": "builtin",
    "root": "templates/workflows/research-topic-exploration",
    "sha256": "directory-or-manifest-hash"
  },
  "preset": {
    "name": "topic-3-publication-run",
    "path": ".loopplane/presets/topic_3_publication.preset.json",
    "sha256": "preset-file-hash"
  },
  "inputs": {
    "topic": "topic_3",
    "topic_slug": "topic_3",
    "target_standard": "publication_grade",
    "max_exploration_runs": 80
  },
  "policy": {
    "planner_mode": "deterministic",
    "self_expansion": "bounded",
    "validation_strictness": "high"
  },
  "rendered_files": {
    ".loopplane/workflows/wf_20260625_abcd1234/PROJECT_BRIEF.md": {
      "sha256": "file-hash",
      "bytes": 1234
    },
    ".loopplane/workflows/wf_20260625_abcd1234/planning/PLAN_DRAFT.md": {
      "sha256": "file-hash",
      "bytes": 5678
    },
    ".loopplane/workflows/wf_20260625_abcd1234/SHARED_CONTEXT.md": {
      "sha256": "file-hash",
      "bytes": 901
    }
  },
  "activation": {
    "direct_activation": false,
    "requires_activate_plan": true
  }
}
```

The instance record should be portable. Prefer project-relative paths. Do not
store absolute local paths except inside diagnostic fields that are explicitly
marked non-portable.

All rendered file paths in the instance record should be derived from
`WorkflowPaths` for the selected workflow. Do not hard-code the v1.5 flat
`.loopplane/...` layout in provenance records because canonical v1.6 workflow
roots live under `.loopplane/workflows/<workflow_id>/`.

## Rendering Rules

Keep the templating language intentionally small.

### Variables

Support simple variable interpolation:

```text
{{topic}}
{{final_report_path}}
{{max_exploration_runs}}
```

Behavior:

- missing variable is a render error;
- boolean and numeric variables render as JSON-like scalar strings;
- object and array values should not render unless explicitly encoded with a
  helper in a future version.

### Optional Blocks

Support comment-delimited optional blocks:

```markdown
<!-- loopplane:if enable_ablation -->
## Phase P3: Ablation

- [ ] P3.T001: Run ablation study for {{topic}}
  - acceptance: Ablation evidence exists and is summarized.
  - evidence: {{results_dir}}/P3.T001/
  - latest: {{results_dir}}/P3.T001/latest.json
  - depends_on: [P2.T001]
  - risk: medium
  - validation: command_exit_code: 0; file_exists: {{final_report_path}}
  - max_attempts: 3
<!-- loopplane:endif -->
```

MVP block rules:

- only boolean variables;
- no nested blocks initially;
- unmatched block markers are render errors;
- blocks are stripped when false;
- block markers are stripped from rendered output when true.

### Comments And Provenance

Rendered files may include a short generated marker:

```markdown
<!-- Generated from LoopPlane workflow template research-topic-exploration@0.1.0. -->
```

Do not include machine-local absolute paths in rendered files.

### No Loops In MVP

Avoid `each` loops in the first pass. Loops make task ID generation and plan
inspection more complex. If repeated tasks are needed, create optional blocks
or explicit task slots.

## Workflow Creation Flow

Add template-aware workflow creation without bypassing existing lifecycle code.
The implementation order matters because rendered plans need a real
`workflow_id` and workflow-specific path values from `WorkflowPaths`.

Recommended flow:

1. Resolve project and workspace state.
2. Validate workflow registry and current pointer using the same safety checks
   as existing `workflow create`.
3. Allocate the new `workflow_id`, `workflow_root`, and canonical workflow
   paths without writing files yet.
4. Resolve template:
   - builtin templates under `templates/workflows/`;
   - optional explicit `--template-dir <path>` for local development.
5. Load preset if provided.
6. Merge values:
   - template defaults;
   - preset `inputs`;
   - CLI `--set key=value` overrides.
7. Add workflow context variables such as `workflow_id`, `brief_file`,
   `plan_file`, `shared_context_file`, `planning_dir`, `results_dir`,
   `runtime_dir`, `read_models_dir`, and `requests_dir`.
8. Validate merged inputs.
9. Render files in memory.
10. Run rendered plan structural inspection.
11. Materialize the canonical workflow root using existing lifecycle helpers.
12. Replace generated `PROJECT_BRIEF.md` and `SHARED_CONTEXT.md` with rendered
   versions when those render targets are present.
13. Write rendered `planning/PLAN_DRAFT.md`.
14. Leave the initial inactive `PLAN.md` in place.
15. Write `template_instance.json`.
16. Register workflow in `.loopplane/workflow_registry.json` and set the current
   pointer.
17. Synchronize active workflow projections after rendered files are written.
18. Return a result that clearly says the workflow is draft and needs
    activation.

Important: do not directly write active `PLAN.md` from the template. Activation
must remain a separate step through existing `activate-plan` checks.

### Generated File Semantics

Existing canonical workflow creation writes a minimal inactive `PLAN.md` with
`active: false`. Template workflow creation should preserve that file until
normal activation. This gives the workspace a valid workflow root immediately
without making template output authoritative execution state.

Template rendering should write:

- `PROJECT_BRIEF.md`, replacing the generic brief generated during
  materialization;
- `SHARED_CONTEXT.md`, when the template provides one;
- `planning/PLAN_DRAFT.md`, which is the reviewable activation candidate;
- `template_instance.json`, which records reproducibility metadata.

Template rendering should not write:

- active `PLAN.md`;
- runtime event logs;
- read models;
- validation records;
- scheduler state.

MVP workflow creation should default to `make_current=true`, matching existing
`loopplane workflow create` behavior. This is necessary because
`loopplane activate-plan` currently operates on the current workflow and does
not accept `--workflow <workflow_id>`. A future release can add non-current
template creation after activation supports explicit workflow selection.

## CLI Design

Add a top-level `template` command group.

```bash
loopplane template list
loopplane template show <template_id>
loopplane template doctor <template_id>
loopplane template render <template_id> --preset preset.json --dry-run
loopplane template render <template_id> --set topic=foo --output /tmp/rendered
```

`template render --output` may write rendered preview files to an explicit
output directory, but it must not register a workflow, update current workflow
pointers, or mutate project-local `.loopplane` workflow truth.

Extend workflow creation:

```bash
loopplane workflow create --template research-topic-exploration \
  --set topic=topic_3 \
  --set target_standard=publication_grade

loopplane workflow create --preset .loopplane/presets/topic_3_publication.preset.json

loopplane workflow create --template research-topic-exploration \
  --preset .loopplane/presets/topic_3_publication.preset.json
```

Useful flags:

- `--template <id>`;
- `--template-dir <path>`;
- `--preset <path>`;
- `--set key=value`, repeatable;
- `--answers <path>` as an alias for preset-like input values if desired later;
- `--dry-run`;
- `--json`;
- `--strict`;
- `--allow-newer-template`.

Future non-MVP flag:

- `--no-make-current`, only after `activate-plan --workflow` exists.

Conflict rules:

- `--preset` may supply the template id.
- If both `--preset` and `--template` are supplied, they must agree unless
  `--allow-template-override` is explicitly added later.
- CLI `--set` overrides preset `inputs`.
- `--set` cannot override locked template fields in strict mode.
- MVP template workflow creation makes the new workflow current. Do not expose a
  non-current creation flag until activation can target non-current workflows.

Argument routing:

- existing free-form workflow creation still requires `--brief`;
- template/preset workflow creation must not require `--brief`, because the
  rendered `PROJECT_BRIEF.md` comes from the template and preset inputs;
- argparse should make `--brief` optional at parse time and enforce conditional
  requirements in `run_workflow_create`;
- `run_workflow_create` should dispatch to existing `create_workflow(...)` when
  neither `--template` nor `--preset` is supplied, and dispatch to
  `create_workflow_from_template(...)` otherwise;
- if `--brief` is supplied with `--template` or `--preset`, treat it as an
  optional input override only if the template defines a `brief` input;
  otherwise report a clear conflict.

## Agent-Friendly Migration

The feature should make this workflow easy:

1. Agent identifies a successful prior workflow.
2. Agent extracts or copies the preset.
3. Agent edits a few inputs.
4. Agent creates a new workflow from the edited preset.

Post-MVP migration commands:

```bash
loopplane template instance show --workflow <workflow_id> --json
loopplane template extract-preset --workflow <workflow_id> --output preset.json
```

`extract-preset` should work only for workflows that already have
`template_instance.json`. It can reconstruct a preset from the recorded
template id/version, inputs, and policy. Keep this out of the smallest MVP so
the first implementation can focus on deterministic creation from an existing
preset.

Future non-MVP command:

```bash
loopplane template draft-preset --from-workflow <workflow_id> --template <template_id>
```

This could ask an agent to map an arbitrary existing workflow into a template,
but it should not be part of the deterministic MVP.

## Template Resolution

MVP search order:

1. explicit `--template-dir`;
2. builtin `templates/workflows/<template_id>`;
3. project-local `.loopplane/templates/workflows/<template_id>` if added later.

Avoid global shared template discovery in MVP. Global discovery can create
surprising portability and trust problems.

Template compatibility:

- exact `template_version` match by default;
- `--allow-newer-template` may allow newer compatible versions later;
- if preset has `template_lock.sha256`, verify it unless
  `--ignore-template-lock` is explicitly supplied.

## Runtime Module Design

Create:

```text
runtime/template_presets.py
```

Suggested public functions:

```python
def list_workflow_templates(project: Path | str | None = None) -> dict[str, Any]:
    ...

def load_workflow_template(template_id: str, *, template_dir: Path | None = None) -> dict[str, Any]:
    ...

def load_workflow_preset(path: Path | str) -> dict[str, Any]:
    ...

def merge_template_inputs(
    template: Mapping[str, Any],
    preset: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ...

def render_workflow_template(
    template: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    workflow_context: Mapping[str, Any],
) -> dict[str, Any]:
    ...

def create_workflow_from_template(
    project_root: Path | str,
    *,
    template_id: str | None = None,
    preset_path: Path | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    make_current: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    ...

def extract_preset_from_template_instance(
    project_root: Path | str,
    *,
    workflow_id: str,
) -> dict[str, Any]:
    ...
```

Keep pure functions for merge, validation, and rendering. This makes tests easy
and reduces filesystem coupling.

## Integration With `runtime/workflows.py`

Two possible implementation approaches:

### Option A: Keep Template Creation In A Separate Module

`runtime/template_presets.py` orchestrates:

- lifecycle checks;
- `materialize_canonical_workflow_files`;
- file replacement for rendered draft files;
- registry creation through `create_workflow_record`.

Pros:

- lower risk to existing workflow create behavior;
- template logic is isolated;
- easier to test.

Cons:

- some workflow-create logic may be duplicated.

### Option B: Extend `create_workflow(...)`

`runtime/workflows.py:create_workflow(...)` accepts template parameters.

Pros:

- one workflow creation entry point.

Cons:

- existing function becomes more complex;
- harder to keep template behavior optional and isolated.

Recommendation: start with Option A and expose it through CLI. Later, if it
stabilizes, refactor common pieces into shared helpers.

For Option A, avoid copy-pasting the entire existing `create_workflow(...)`
function. Instead, extract or reuse small helpers for:

- workspace/registry/current-pointer validation;
- workflow identity allocation;
- workflow-create safety checks;
- workflow name generation;
- canonical file materialization;
- registry creation and current-pointer update;
- text formatting and exit code mapping.

If helper extraction is too large for the first implementation, keep the first
template implementation narrow and add tests around the duplicated safety
behavior so future refactoring is straightforward.

## Plan Inspection And Activation

Rendered `PLAN_DRAFT.md` must be inspected before the workflow creation result
reports success.

Use existing structural validation from `runtime/planning.py`:

- required metadata;
- configured paths;
- task grammar;
- required fields;
- objective checklist semantics;
- active state should remain draft before activation.

Current plan inspection APIs operate on files. The implementation should choose
one of these approaches:

- write the rendered draft to a temporary file under a temporary directory and
  call `inspect_plan_draft(temp_path, workflow_id=workflow_id)`, then delete the
  temporary file; or
- refactor the existing inspection code to expose an internal
  `inspect_plan_text(text, workflow_id=..., expected_active=False)` helper and
  let both file-based and template-based callers use it.

The second approach is cleaner long term, but the temporary-file approach is an
acceptable MVP if it is isolated and tested.

If possible, expose inspection result in the template creation result:

```json
{
  "ok": true,
  "status": "workflow_created_from_template",
  "plan_draft": {
    "path": ".loopplane/workflows/<id>/planning/PLAN_DRAFT.md",
    "structural_status": "valid",
    "errors": [],
    "warnings": []
  },
  "next_actions": [
    "Review PLAN_DRAFT.md.",
    "Run loopplane activate-plan --project <project> when ready."
  ]
}
```

Do not run planner by default in template mode.

## Policy Handling

In MVP, policy values in presets should be recorded in `template_instance.json`
and optionally rendered into brief/shared context text. They should not silently
rewrite low-level runtime config unless explicitly supported.

Candidate MVP policy values:

- `planner_mode`: `deterministic`, `bounded_adaptation`, `freeform`;
- `self_expansion`: `off`, `bounded`, `aggressive`;
- `validation_strictness`: `low`, `medium`, `high`;
- `final_review`: boolean.

Only `planner_mode=deterministic` needs implementation in the first pass.
Other policy values can be passed into templates as variables and recorded for
future behavior.

## Security And Portability Rules

Templates and presets should be safe to review and move between machines.

Validation should reject or warn on:

- absolute paths in rendered workflow files, except documented examples;
- parent directory traversal in output paths;
- path values outside the project root;
- shell commands embedded in template manifest fields;
- unknown render target names;
- missing render files;
- symlinked template files escaping the template root;
- large template files above a reasonable size budget.

The feature should not execute code from template directories.

All stored provenance should prefer project-relative POSIX paths. Workflow path
variables exposed to templates should come from `WorkflowPaths.values`, not from
hand-built `.loopplane/...` strings.

For `template_lock.sha256`, prefer a deterministic full-template-directory hash
over a manifest-only hash. Include `template.json` and render files, sort paths
lexicographically, normalize path separators to POSIX style, and reject symlink
escapes before hashing. This makes preset migration stricter and easier to
debug.

## Dashboard And Read Model Follow-Up

MVP does not need dashboard changes, but follow-up work can expose template
origin in read models:

- add `template_instance` summary to `workflow_status.json`;
- show template id/version in dashboard workflow metadata;
- allow dashboard to copy/extract a preset from a prior workflow.

This should be read-only dashboard behavior.

## Suggested Builtin Templates

Implement one template first, then add more after the flow stabilizes.

### 1. `research-topic-exploration`

Use case:

- repeated research/exploration workflows;
- publication-grade or internal-report standards;
- structured experiment matrix and final review.

Common inputs:

- `topic`;
- `topic_slug`;
- `target_standard`;
- `max_exploration_runs`;
- `final_report_path`;
- `enable_literature_review`;
- `enable_ablation`;
- `enable_baseline_comparison`.

Likely phases:

- P0: Scope and acceptance contract;
- P1: Prior art and baseline;
- P2: Experiment matrix;
- P3: Execution and evidence collection;
- P4: Analysis and ablation;
- P5: Final report and publication gate.

### 2. `dashboard-performance-investigation`

Use case:

- debugging dashboard/read-model latency;
- benchmarking and regression tests;
- performance plan and implementation.

Common inputs:

- `target_workflow`;
- `symptom`;
- `benchmark_command`;
- `latency_budget_ms`;
- `payload_budget_bytes`;
- `maxrss_budget_kb`;
- `final_report_path`.

Likely phases:

- P0: Reproduce and instrument;
- P1: Identify hot paths;
- P2: Implement bounded loading;
- P3: Add regression tests;
- P4: Benchmark and release notes.

## Implementation Phases

### Phase 0: Design Fixtures And Schemas

Tasks:

- add `workflow_template.schema.json`;
- add `workflow_preset.schema.json`;
- add `template_instance.schema.json`;
- add one minimal builtin template fixture;
- add one example preset fixture.

Acceptance:

- schemas validate good fixtures;
- schemas reject missing required template id/version/render targets;
- fixtures contain no local absolute paths.

### Phase 1: Pure Loading, Merging, And Rendering

Tasks:

- implement `runtime/template_presets.py`;
- implement template discovery;
- implement preset loading;
- implement input merge and validation;
- implement workflow-context variable injection from `WorkflowPaths`;
- implement variable rendering;
- implement boolean optional blocks;
- implement template directory hash;
- implement dry-run render result.

Acceptance:

- repeated render with same inputs is byte-identical;
- missing required input gives clear error;
- CLI overrides win over preset values;
- workflow path variables match the selected workflow's `WorkflowPaths`;
- false optional blocks are removed;
- unknown variable fails rendering;
- symlink escape is rejected.

### Phase 2: CLI Read-Only Commands

Tasks:

- add `loopplane template list`;
- add `loopplane template show <template_id>`;
- add `loopplane template doctor <template_id>`;
- add `loopplane template render <template_id> --preset <path> --dry-run --json`.

Acceptance:

- list shows builtin templates;
- show prints template metadata and required inputs;
- doctor validates manifest, render files, examples, and safety rules;
- dry-run render does not mutate project files.

### Phase 3: Workflow Creation From Template/Preset

Tasks:

- add `loopplane workflow create --template <id>`;
- add `loopplane workflow create --preset <path>`;
- make `workflow create --brief` conditionally required only for free-form
  workflow creation;
- support repeated `--set key=value`;
- allocate workflow identity and canonical `WorkflowPaths` before rendering;
- render and inspect `PLAN_DRAFT.md` before committing durable writes;
- create canonical workflow root through existing materialization;
- write rendered `PROJECT_BRIEF.md`;
- write rendered `planning/PLAN_DRAFT.md`;
- write optional `SHARED_CONTEXT.md`;
- preserve the initial inactive `PLAN.md`;
- write `template_instance.json`;
- register workflow and current pointer through existing lifecycle code;
- make the new workflow current in MVP;
- synchronize active workflow projections after rendered files are in place.

Acceptance:

- workflow is created in draft-ready state;
- active `PLAN.md` is not promoted by template creation;
- initial `PLAN.md` remains inactive with `active: false`;
- rendered `planning/PLAN_DRAFT.md` has `active: false`;
- existing `activate-plan` works on the generated draft;
- workflow registry contains the new workflow;
- current workflow pointer references the new workflow;
- active workflow projections reflect the rendered template files;
- `template_instance.json` uses project-relative paths;
- dry-run mode reports intended writes without writing.

### Phase 4: Preset Extraction

This is useful for agent-friendly migration, but it can wait until after the
first workflow-create MVP is stable.

Tasks:

- add `loopplane template instance show --workflow <workflow_id>`;
- add `loopplane template extract-preset --workflow <workflow_id> --output <path>`;
- reconstruct a preset from `template_instance.json`.

Acceptance:

- extracted preset can create an equivalent new workflow when the same template
  version is available;
- command fails clearly if workflow has no `template_instance.json`;
- output preset contains no machine-local absolute paths.

### Phase 5: Builtin Templates

Tasks:

- add `research-topic-exploration`;
- add `dashboard-performance-investigation`;
- include example presets for both;
- add docs in `templates/workflows/README.md`.

Acceptance:

- both templates render valid `PLAN_DRAFT.md`;
- generated plans pass existing structural inspection;
- examples can be rendered in tests.

## Test Plan

Add tests in a new file:

```text
tests/test_template_presets.py
```

Recommended coverage:

- template discovery finds builtin templates;
- preset schema validates;
- input defaults merge correctly;
- CLI `--set` overrides preset input;
- missing required input fails;
- unknown variable fails;
- optional block true/false behavior;
- template render is deterministic;
- render rejects symlink escape;
- render rejects parent traversal output paths;
- dry-run workflow creation does not write files;
- workflow creation writes `template_instance.json`;
- workflow creation preserves inactive `PLAN.md`;
- workflow creation writes valid `planning/PLAN_DRAFT.md`;
- `activate-plan` succeeds after template workflow creation;
- generated `PLAN_DRAFT.md` passes existing inspect logic;
- extracted preset can recreate equivalent rendered files.

Add CLI tests near existing workflow CLI tests:

- `template list --json`;
- `template show --json`;
- `template render --dry-run --json`;
- `workflow create --preset --json`.

## Error Model

Return structured errors with actionable recovery hints.

Common statuses:

- `template_not_found`;
- `template_manifest_invalid`;
- `preset_invalid`;
- `template_version_mismatch`;
- `template_lock_mismatch`;
- `input_validation_failed`;
- `render_failed`;
- `plan_draft_invalid`;
- `workflow_create_conflict`;
- `dry_run`.

Each failure should include:

- `ok: false`;
- `status`;
- `errors`;
- `warnings`;
- `recovery_actions`;
- relevant file paths.

## Documentation Updates

Update:

- `README.md`
  - short user-facing intro and examples.
- `templates/README.md`
  - explain difference between document templates and workflow templates.
- `references/PROTOCOL.md` or a new reference doc
  - explain template/preset authority boundary.

Suggested user-facing examples:

```bash
loopplane template list
loopplane template show research-topic-exploration
loopplane workflow create --template research-topic-exploration --set topic=topic_3
loopplane activate-plan --project .
```

```bash
loopplane workflow create --preset .loopplane/presets/topic_3_publication.preset.json
```

## Open Decisions

- Should project-local `.loopplane/presets/` be created automatically by
  `loopplane init`?
- Should template examples live under `templates/workflows/<id>/examples/` or
  `examples/templates/`?
- Should `bounded_adaptation` planner mode be represented now but disabled, or
  omitted until implemented?
- Should extracted presets include all resolved defaults or only values that
  differ from template defaults?

Resolved MVP decisions:

- Template-created workflows default to `make_current=true` until
  `activate-plan --workflow <workflow_id>` exists.
- `template_lock.sha256` should hash the full template directory, not only
  `template.json`.

## Recommended MVP Cut

The smallest useful version is:

- builtin template discovery;
- template and preset schemas;
- simple variable rendering;
- boolean optional blocks;
- `template list/show/render --dry-run`;
- `workflow create --preset`;
- `template_instance.json`;
- one builtin template: `research-topic-exploration`;
- tests proving generated draft plans pass inspection.

Preset extraction, dashboard display of template provenance, and
`dashboard-performance-investigation` can follow after this MVP.

This MVP is enough to shift repeated workflow creation from unstable prompt
translation to lightweight, agent-editable presets while keeping LoopPlane's
authority and activation model intact.
