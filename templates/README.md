# LoopPlane Templates

This directory contains the required project, plan, shared-context, and agent
prompt templates derived from `LoopPlane.md`.

## Required Files

- `PROJECT_BRIEF.template.md`
- `PLAN.template.md`
- `SHARED_CONTEXT.template.md`
- `planner_prompt.template.md`
- `auditor_prompt.template.md`
- `worker_prompt.template.md`
- `recovery_prompt.template.md`
- `inspector_prompt.template.md`
- `validator_prompt.template.md`
- `change_request_planner_prompt.template.md`
- `summary_prompt.template.md`
- `final_reviewer_prompt.template.md`
- `expansion_planner_prompt.template.md`
- `objective_verifier_prompt.template.md`

Templates use `{{variable_name}}` placeholders for workflow-configured paths
and run-specific values. Renderers must supply configured paths such as
`{{results_dir}}`, `{{runtime_dir}}`, `{{read_models_dir}}`,
`{{requests_dir}}`, and `{{planning_dir}}` instead of hard-coding local
workflow paths.

Every prompt template includes the LoopPlane untrusted input rule. Document
templates preserve the required project brief fields, plan task grammar,
authority hierarchy, evidence paths, validation strategy, retry budgets,
blocked/skipped metadata, high-level objective gates, self-expansion follow-up
semantics, and completion semantics.

## Workflow Templates

`templates/workflows/` is a separate deterministic workflow-template layer.
Those files are JSON manifests, JSON presets, and Markdown render templates used
by `loopplane template ...` and `loopplane workflow create --preset ...`.

Workflow templates do not replace the prompt templates above. They only render a
new workflow's `PROJECT_BRIEF.md`, `SHARED_CONTEXT.md`, and
`planning/PLAN_DRAFT.md`; `PLAN.md` remains inactive until the normal
`activate-plan` readiness path promotes the draft.
