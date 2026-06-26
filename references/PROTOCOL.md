# LoopPlane Protocol Reference

Source anchors: `LoopPlane.md` sections 4, 5, 22, and 23; especially lines
210-293, 297-399, 3773-3824, and 3829-3904.

`LoopPlane.md` is the authoritative product specification. This document is a
concise implementer reference and must not be used to weaken or replace the
specification.

## Core Principles

LoopPlane separates durable workflow responsibilities across explicit authorities:
the brief seeds intent, the planner crystallizes it, `PLAN.md` owns active
intent, the scheduler owns control, workers execute scoped work, evidence owns
truth, validators own completion, Git provides human-readable checkpoints, the
dashboard owns visibility, and the package owns portability.

## Authority Layers

When artifacts disagree, prefer the stricter interpretation from the higher
authority layer:

1. Protocol and contract: `LoopPlane.md`, `.loopplane/SHARED_CONTEXT.md`, and
   `security.json`.
2. User intent and active plan: `PROJECT_BRIEF.md`, `PLAN.md`, and approved
   change requests.
3. Evidence and validation: task run evidence, authoritative
   `validation.json`, `latest.json`, and `evidence_manifest.json`.
4. Version-control checkpoints: LoopPlane-managed local Git refs, checkpoint log,
   and captured diffs.
5. Runtime audit state: event log, snapshots, state, failure registry, and
   active run leases.
6. Derived read models: workflow status, plan index, workflow graph, run
   summaries, metrics, and related dashboard data.
7. Presentation and convenience layers: dashboard UI, latest views, and
   user-facing activity feeds.

Runtime caches and read models are rebuildable. Git checkpoints are
recoverability artifacts, not plan authorities.

Runtime events are append-only audit records with monotonic sequence metadata,
deterministic event IDs, hash-chain links, and canonical hashes. Snapshots are
compact replay accelerators: startup and later read-model work may load the
latest snapshot and replay only subsequent events, but snapshots do not replace
the append-only event log as audit evidence.

## Workflow Shape

The initialization phase turns user intent into `PROJECT_BRIEF.md`, runs the
planner to create `PLAN_DRAFT.md` and a readiness report, optionally audits the
draft, and activates `PLAN.md` only after readiness checks pass.

Workflow template presets are deterministic initialization helpers. A template
or preset may render `PROJECT_BRIEF.md`, `SHARED_CONTEXT.md`, and
`planning/PLAN_DRAFT.md`, and may record provenance in `template_instance.json`,
but it does not make the draft authoritative. Template-created workflows must
still pass the same readiness and `activate-plan` promotion path before
`PLAN.md` becomes active.

The execution phase advances one durable unit of work at a time. A scheduler
tick reconciles plan, state, events, and evidence; handles requests, approvals,
background jobs, and configuration waits; selects recovery before new work;
prepares a run; builds a prompt; invokes an external CLI agent through an
adapter; validates outputs; reconciles the plan and latest pointers; creates a
Git checkpoint; rebuilds read models; and runs deterministic final verification
when no active work remains.

The dashboard phase is observational and request-oriented. The dashboard reads
read models and may write control, chat, change, or approval requests. It must
not directly mutate `PLAN.md`, event logs, runtime state, validations, or
completion markers.

## Evidence And Completion

Workers may claim completion, but validators decide whether evidence satisfies
acceptance criteria. `validation.json` is authoritative and may only be written
by the validator. `latest.json` may only be updated after validation passes.
When a worker declares additional `evidence_satisfies` task IDs, those IDs are
candidate completions rather than authority. The validator records one
`task_results` entry per primary or candidate task and lists only
independently accepted, policy-allowed tasks in `accepted_task_ids`; the
reconciler may later close additional tasks only from that accepted set.

Completion requires deterministic final verification. A workflow is not
complete merely because the prompt queue is empty or tasks appear checked. The
final verifier must reject unresolved pending, partial, blocked, or
unauthorized skipped work; missing latest pointers or validations; unrecovered
failures; active leases or background jobs; pending approvals; missing final
deliverables; missing required Git checkpoints; stale read models that cannot
be rebuilt; and stale completion markers.

## Non-Authority Boundaries

Prompt queues are derived from active state and never own truth. The planner
may write `PLAN_DRAFT.md` but may not execute implementation work. The
scheduler may execute only active `PLAN.md`. Inspector chat is read-only by
default; workflow changes enter the change request protocol.
