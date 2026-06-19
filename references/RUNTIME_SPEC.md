# LoopPlane Runtime Reference

Source anchors: `LoopPlane.md` sections 5.3, 9.16-9.20, 10.3-10.12, 14, 15, 22,
and 24; especially lines 349-377, 1461-1605, 2451-2580, 2942-3350,
3773-3824, and 3904-4125.

`LoopPlane.md` remains authoritative. This document summarizes runtime obligations
without adding implementation-specific behavior.

## Runtime Responsibilities

The scheduler owns workflow control. It acquires the scheduler instance lock,
processes control requests, maintains active run leases during long runs,
selects recoveries before new work, prepares run directories before prompt
generation, invokes agent runner adapters, triggers validation and
reconciliation, rebuilds read models, and runs final verification.

The prompt builder creates one prompt at a time from `PLAN.md`, shared context,
the target task block, previous failures, and run-specific paths. Prompts are
derived artifacts, not sources of truth, and configured paths must be injected
rather than hard-coded.

## Coordination Model

The runtime uses separate coordination concepts:

- `scheduler_instance_lock` prevents concurrent scheduler main loops.
- `active_run_lease` represents one active run and must heartbeat during long
  CLI agent execution.
- `event_append_lock` protects append-only event writes.

Event records are append-only JSONL audit entries. `state.json` is a derived
runtime view, not an authority source. Snapshots and event segments are used to
bound replay cost. Each event record carries monotonic `seq`/`sequence`
metadata, a deterministic `event_id`, previous event ID/hash links when a prior
event exists, and a canonical `event_hash`. Event writes must happen through
`event_append_lock` or an equivalent single-writer helper and be flushed
according to the runtime durability policy.

## Run Directories

Every agent invocation has a scheduler run directory under
`.loopplane/runtime/runs/<run_id>/` and a role output directory. Planner and auditor
outputs live under planning runs, inspector and change-request planner outputs
under request runs, and worker evidence under `{{results_dir}}/<task_id>/runs`.

Task workers additionally receive `task_id` and `task_evidence_run_dir`.
Worker-owned evidence includes run reports, commands, logs, artifacts, raw
outputs, node summaries, and agent status. Authoritative validation remains
validator-owned.

## Scheduling Order

A scheduler tick reconciles plan, state, events, and artifacts before selecting
work. The scheduler handles control requests, pending approvals, background
waits, configuration problems, recoverable failures, executable tasks, and then
final verification in that order. Recovery work has priority over new tasks
when a recoverable failure remains within budget.

`prepare_run()` must occur before prompt generation and produce the run ID,
scheduler run directory, role output directory, task evidence directory when
applicable, initial active run lease, prompt path, stdout and stderr paths,
final-output path, adapter-result path, and node ID.

## Operational Edge Cases

Active `PLAN.md` parsing must be strict. If the active plan is malformed, the
scheduler must wait for configuration or reconciliation instead of inferring
task state from partial text.

Authorized plan mutations refresh the accepted active-plan hash. If `PLAN.md`
later differs from that accepted hash outside an authorized activation,
reconciliation, or change-request path, the live scheduler appends
`manual_plan_change_detected`, records that reconciliation is required, and
waits instead of starting more work.

Active run leases block duplicate worker starts until they are safe to pass.
When a lease is stale, the runtime records heartbeat freshness, process
liveness when available, and the presence of adapter outputs before deciding
whether recovery or human attention is needed.

`latest.json` is the authoritative latest pointer for task evidence. A
`latest/` symlink may be created by environments that support it, but the
runtime must not require it and must continue to work when symlinks are
unsupported.

## Preview And Health

`loopplane preview`, `loopplane preview --json`, and `loopplane run --dry-run` use the real
selection logic without starting an agent or mutating authoritative workflow
state. Preview reports the next action, earlier candidates not selected,
whether the workflow would wait, completion-marker freshness, selected runner
metadata, and blocking conditions.

`loopplane health` is independent of dashboard read models. It inspects locks,
leases, runner liveness when available, background jobs, recent agent status
files, validations, completion-marker freshness, failure registry state, Git
checkpoint availability, event segment validity, and read-model rebuildability.
By default it checks the current workflow. `--workflow <workflow_id>` resolves a
registered workflow without changing `.loopplane/current_workflow.json`, and
`--all` reports a workspace-wide summary.

## Validation And Reconciliation

The validator reads the primary task block, any candidate task blocks from
`agent_status.evidence_satisfies`, the worker run directory, agent status,
commands, logs, artifacts, project diff when available, acceptance criteria,
and validation strategy. It writes authoritative `validation.json`.
Candidate task IDs from `evidence_satisfies` are evidence hints only. The
validator must validate the primary task first, then independently validate
each candidate's own acceptance criteria only when controlled absorption policy
allows it: same phase, adjacent claimed task range, compatible dependencies,
open task status, no default-disallowed high risk, and no missing approval.
Accepted candidates appear in `validation.accepted_task_ids` and rejected
candidates remain in per-task `validation.task_results`; the reconciler owns
any later `PLAN.md` closure.

Validation strategies may include schema checks, file existence checks, command
exit codes, tests, lint or type checks, artifact hashes, report content checks,
advisory LLM review, and human-approval clauses. In unattended mode,
human-approval clauses are auto-authorized with warnings instead of blocking
the workflow. Deterministic checks should not rely solely on LLM-assisted
review when deterministic validation is available.

The reconciler marks tasks complete only after authoritative validation, writes
or updates `latest.json` for accepted tasks, appends validation and plan-update
events, updates failures when validation fails, creates approval requests when
human input is needed, and rebuilds read models. It must not accept partial,
skipped, or dashboard/chat-driven plan changes outside their explicit protocol.

## Final Verification

The deterministic final verifier checks plan parseability, unresolved work,
validated latest pointers, skipped-task authorization, failures, active leases,
background jobs, approvals, final deliverables, Git checkpoints, read-model
freshness or rebuildability, and completion-marker freshness. Only after this
passes may the runtime write `plan_loop_complete.json`; stale markers must be
reported or ignored rather than accepted as completion.

## Git Runtime Boundary

Git is the default checkpoint backend. Runtime checkpoints protect
human-readable workspace history but do not replace `PLAN.md`, events,
validation, latest pointers, or deterministic verification. Worker and recovery
runs must have pre-run and post-run Git metadata and diffs captured under the
run's `git/` directory by the Version Control Manager.

Worker and recovery adapters must enforce Git command boundaries before
running commands. They may allow read-only inspection such as `git status` and
`git diff`, but must block write-oriented Git operations and direct
`loopplane vc checkpoint` or `loopplane vc rollback` requests. Only the Version
Control Manager path may create checkpoints or perform rollback, and rollback
remains subject to approval policy.
