# LoopPlane Human Quickstart

This document is for users who do not want to run LoopPlane commands directly.
Copy the prompts below into your external coding agent to install LoopPlane, create a
plan, review it, run the workflow, and check progress.

Replace placeholders such as `<PROJECT_DIR>` and `<REQUIREMENTS.md>` before
sending a prompt.

## What You Provide

- The target project directory.
- Optional: the local LoopPlane source/package directory, if you already know it.
- A requirements markdown file, or plain text describing the task.
- Optional preferences, such as using Codex, Claude Code, or both.

You do not need to know LoopPlane internals. Fill in the placeholders and send the
prompt that matches the stage you want.

## Prompt 1: Install LoopPlane

Use this once per target project.

```text
Install LoopPlane into this target project:

<PROJECT_DIR>

LoopPlane source/package path, if provided:
[<LOOPPLANE_SOURCE_DIR> | leave blank]

Use the provided LoopPlane source/package path if it is non-empty. If it is blank,
look for a local LoopPlane package in the workspace; if you cannot find one, ask me
for its path. Configure LoopPlane end to end for this project.

Important requirements:
- Do not ask me to run CLI commands. You run the commands and inspect the files.
- Preserve existing project files. Do not overwrite user-authored files unless
  LoopPlane explicitly requires it and you explain why.
- Discover available agent CLIs automatically. Prefer Codex if available, and
  also configure Claude Code if available. Run runner diagnostics so planner,
  auditor, worker, inspector, and summary roles are actually usable.
- Initialize lightweight Git tracking if appropriate, with a sensible .gitignore
  that avoids large generated files, caches, logs, checkpoints, and temporary
  outputs.
- Start or prepare the dashboard if useful, and tell me the dashboard URL.
- If anything fails, diagnose and fix it yourself where possible. Do not stop
  after merely reporting that something is blocked.

At the end, summarize:
- LoopPlane project path
- configured runner CLIs
- dashboard URL, if available
- any remaining caveats
```

## Prompt 2A: Plan From Requirements

Use this after installation. Choose either a markdown file or plain text.

### With A Markdown File

```text
Use LoopPlane to plan work for this project:

Project: <PROJECT_DIR>
Requirements file: <REQUIREMENTS.md>

Treat the markdown file as the user requirement source. Write it into the LoopPlane
project brief, run the planner/auditor planning loop until a ready plan draft is
produced, and rebuild the dashboard/read models.

Do not start execution yet. I want to review the plan first.

Important requirements:
- Do not ask me to run CLI commands.
- If planner or auditor fails, inspect the generated prompts, logs, runner
  configuration, and LoopPlane state; fix the problem and retry.
- If the plan is structurally invalid or too vague, revise it until it is
  concrete, executable, and validated.
- Keep task validation criteria machine-checkable where possible.

At the end, tell me:
- whether the plan draft is ready
- where the plan draft is stored
- dashboard URL
- a short human-readable summary of phases and tasks
- any questions that truly block plan activation
```

### With Plain Text

```text
Use LoopPlane to plan work for this project:

Project: <PROJECT_DIR>

User requirements:
<PASTE_REQUIREMENTS_HERE>

Write these requirements into the LoopPlane project brief, run the planner/auditor
planning loop until a ready plan draft is produced, and rebuild the dashboard/read
models.

Do not start execution yet. I want to review the plan first.

Follow the same reliability requirements:
- run commands yourself
- fix runner/planning/config problems yourself where possible
- make the plan concrete and executable
- report the dashboard URL and a short plan summary
```

## Prompt 2B: Create Another Workflow And Plan

Use this instead of Prompt 2A when LoopPlane is already installed in the project and
you want a separate new workflow for new requirements. This preserves previous
workflow history.

```text
Create a new LoopPlane workflow in this existing LoopPlane project, then plan the new
work:

<PROJECT_DIR>

New workflow requirements:
[<NEW_REQUIREMENTS.md> | paste plain text requirements]

Existing workflow handling:
[KEEP_EXISTING | STOP_IF_RUNNING | ARCHIVE_IF_COMPLETED | ASK_ME]

Inspect the current LoopPlane workflow state first. Preserve existing workflow
history, logs, evidence, and dashboard data. Do not overwrite the previous
workflow's plan or runtime state.

If a workflow is actively running, do not force-switch or mutate state unless I
explicitly selected STOP_IF_RUNNING above. If the completed workflow can be
archived and I selected ARCHIVE_IF_COMPLETED, archive it through LoopPlane's normal
workflow controls. Archiving should not delete or hide that workflow; it should
remain visible in the dashboard workflow selector with an archived label, while
mutation controls remain disabled for that archived history.

Create a new workflow history for the new requirements and make it the current
workflow through LoopPlane's normal workflow controls. Write the new requirements
into the LoopPlane project brief, run the planner/auditor planning loop until a
ready plan draft is produced, and rebuild the dashboard/read models.

Do not start execution yet. I want to review the plan first.

Important requirements:
- Do not ask me to run CLI commands.
- If workflow creation, planner, or auditor fails, inspect LoopPlane state, runner
  configuration, prompts, logs, and dashboard/read models; fix the problem and
  retry where possible.
- If the plan is structurally invalid or too vague, revise it until it is
  concrete, executable, and validated.
- Keep task validation criteria machine-checkable where possible.

At the end, summarize:
- previous workflow id and state, if available
- new workflow id
- whether the new workflow is now current
- whether the plan draft is ready
- where the plan draft is stored
- dashboard URL
- a short human-readable summary of phases and tasks
- any question that truly blocks plan activation
```

## Prompt 3: Review Or Request Changes

Use this after you inspect the dashboard or plan summary.

This prompt is optional. Use it when you want help reviewing the draft or when
you want to request plan changes before execution.

```text
Review the current LoopPlane plan draft, or apply the requested planning changes, for
this project:

<PROJECT_DIR>

Requested changes, if any:
[<REQUESTED_PLAN_CHANGES> | leave blank]

Review the current plan draft and dashboard state. If I listed requested changes,
update the brief/planning feedback as needed and rerun the planner/auditor loop
until the draft is ready again. If I left requested changes blank, only review
the draft.

This request is only for the review/change stage. Stop after the plan draft is
reviewed or updated. I will send a separate execution-stage prompt when I want
the plan activated and the workflow started.

Tell me whether the draft looks ready to execute, whether it matches my
requirements, and what changed if you revised it.

At the end, summarize:
- whether the current plan draft is ready
- whether it matches my requirements
- changes made, or suggested fixes if no changes were requested
- any risks or approval gates
- dashboard URL
```

## Prompt 4: Run In The Background

Use this when you are ready to enter the execution stage.

In LoopPlane, "activate" means confirming `PLAN_DRAFT.md` as the active `PLAN.md`,
which makes it executable by the scheduler. Prompt 4 is the execution-stage
prompt: fix any remaining plan issues, activate the plan, and run the workflow.

```text
Enter the LoopPlane execution stage for this project:

<PROJECT_DIR>

Before starting execution, inspect the current plan draft, readiness report,
audit report, dashboard state, and active PLAN. If the plan is not ready or does
not match the requested work, fix the planning state by revising the brief or
planning feedback, rerun the planner/auditor loop, and get the plan ready.

Once the plan is ready, activate it by promoting PLAN_DRAFT.md into the active
PLAN.md, then start the workflow in detached/background mode.

Monitor it long enough to confirm it is actually running. Keep the dashboard
available. If agents fail during execution, inspect their evidence, logs,
validation reports, and runner configuration; fix recoverable problems and
continue. Do not simply mark the workflow blocked unless you have tried to repair
the issue.

At the end, tell me:
- whether the plan was activated
- whether the detached runtime is running
- dashboard URL
- current phase/task progress
- how to ask you for a later status update
```

## Prompt 5: Status Update

Use this any time after starting the workflow.

```text
Check the LoopPlane workflow status for this project:

<PROJECT_DIR>

Inspect the dashboard/read models, detached runtime, recent task summaries,
active jobs, failures, and validation reports. If something is blocked or stale,
first try to repair it and resume progress.

Give me a concise status update:
- current running or recently completed task
- completed / blocked / pending counts
- latest meaningful result
- dashboard URL
- anything that needs my decision
```

## Prompt 6: Stop Or Pause

Use this when you want the workflow to stop safely.

```text
Safely stop or pause the LoopPlane workflow for this project:

<PROJECT_DIR>

Use LoopPlane's normal control mechanisms. Do not kill unrelated processes. Confirm
that the detached runtime has stopped or paused, and preserve logs and evidence
for later review.

Summarize the final observed state and dashboard URL.
```

## Recommended Flow

1. Send **Prompt 1** to install and configure LoopPlane.
2. Send **Prompt 2A** for the first/current workflow, or **Prompt 2B** when you want a new separate workflow.
3. Open the dashboard and review the plan.
4. Send **Prompt 3** if you want the agent to review the plan or make requested plan changes.
5. Send **Prompt 4** when you are ready for the agent to fix any remaining plan issues, activate the plan, and run it in the background.
6. Send **Prompt 5** whenever you want progress.

For most users, this is the clean mental model:

```text
requirement markdown or text
-> external agent
-> LoopPlane project brief
-> planner/auditor loop
-> dashboard review
-> execution-stage activation
-> detached workflow execution
```
