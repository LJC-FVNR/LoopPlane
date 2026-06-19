# LoopPlane Planner Reference

Source anchors: `LoopPlane.md` sections 5.2, 9.3-9.7, 10.1-10.2, 12, 13.1-13.2,
17, and 18; especially lines 325-347, 925-1085, 2426-2449, 2695-2753,
2757-2814, 3413-3533.

`LoopPlane.md` remains authoritative. This document summarizes planner and auditor
behavior for implementers.

## Planner Purpose

The planner converts `PROJECT_BRIEF.md` and workspace context into an
executable plan draft. It identifies goals, resources, constraints,
deliverables, risks, assumptions, stable task IDs, acceptance criteria,
evidence roots, dependencies, validation strategies, risk levels, approval
needs, and retry budgets.

The planner may create or revise `PLAN_DRAFT.md`, update
`.loopplane/SHARED_CONTEXT.md`, and write `plan_readiness_report.json`. It must not
execute implementation tasks.

## Plan Draft Requirements

`PLAN_DRAFT.md` uses the same task grammar as active `PLAN.md`, but the
scheduler never executes it. Activation copies or transforms a ready draft into
active `PLAN.md` only after readiness checks pass.

Each active task must include:

- stable task ID and title;
- checkbox status;
- acceptance criteria;
- evidence root and latest pointer path;
- dependencies;
- risk level;
- validation strategy;
- retry budget;
- approval metadata;
- expected deliverables or an explicit none reason.

Skipped tasks also require a skip reason and authorization or approval. Blocked
tasks also require blocked reason, detection time, and unblock condition.

Checkbox status has protocol meaning: `[x]` means done with authoritative
evidence and validation, `[ ]` means not done, `[~]` remains unfinished unless
explicitly accepted, `[!]` is blocked and unresolved, and `[-]` is skipped only
with explicit approval or contractual out-of-scope authorization.

## Readiness Report

`plan_readiness_report.json` summarizes whether a draft is ready for audit or
activation, blocking questions, assumptions, warnings, task counts, high-risk
tasks, human-approval needs, activation blockers, and readiness errors. Allowed
statuses are `draft`, `needs_revision`, `ready_for_audit`,
`ready_for_activation`, `blocked_needs_user`, and `failed`.

The planner should ask the user only when a missing answer blocks plan
creation. Otherwise it should record assumptions and warnings.

## Auditor Boundary

The optional auditor reads `PLAN_DRAFT.md`, checks task IDs, granularity,
acceptance criteria, evidence paths, dependencies, validation feasibility,
risk approvals, final deliverables, skipped or blocked authorization, and
unresolved ambiguity, then writes `audit_report.json`. The auditor does not
execute implementation tasks.

## Activation Boundary

`loopplane activate-plan` checks readiness and writes active `PLAN.md`. Activation
must fail when the draft is malformed, required fields are missing, a required
auditor did not pass, blocking readiness questions remain, or protected paths
would be overwritten without approval.

## Change And Approval Interaction

Scope changes, accepting partial results as final, skipping originally active
tasks, destructive file operations, external publishing, long-running or
expensive jobs, secrets or paid APIs, and killing active processes require
approval when approval gates are enabled. When approvals are disabled, risky
actions must not be silently approved.

Change requests let users add or modify requirements without bypassing
`PLAN.md`. A change request planner reviews impact, may propose a plan patch,
requests approval when scope or risk changes, and does not apply plan changes
directly.
