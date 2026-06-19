# LoopPlane Dashboard Reference

Source anchors: `LoopPlane.md` sections 5.4, 9.30-9.36, 10.10, 10.13-10.14,
18-21, 23.4, and 24.9; especially lines 379-399, 2066-2273, 2543-2554,
2582-2603, 3488-3720, 3875-3889, and 4091-4106.

`LoopPlane.md` remains authoritative. This document summarizes dashboard and
inspection obligations without choosing a frontend framework.

## Dashboard Authority Boundary

The dashboard reads derived read models and writes request records. It must not
directly mutate `PLAN.md`, event logs, runtime state, validations, read models,
Git internals, or completion markers.

Inspector chat is read-only by default. If a user asks to change workflow
requirements, inspector or dashboard code must create a change request rather
than editing `PLAN.md`.

## Read Models

Required read models include:

- `workflow_status.json`;
- `plan_index.json`;
- `workflow_graph.json`;
- `run_summaries.jsonl`;
- `dashboard_feed.jsonl`;
- `metrics.json`;
- `version_control_status.json`.

Read models are derived and rebuildable. `loopplane rebuild-read-models` parses
`PLAN.md`, loads latest pointers and validations, replays event history from
the latest snapshot, rebuilds all read models, rebuilds sanitized
version-control status, and validates read model schemas.

Read models should include generation time, source hashes, and last event
sequence or source event ID. The dashboard should warn when read models are
stale relative to the event log.

The implemented builder writes `workflow_status.json`, `plan_index.json`,
`workflow_graph.json`, `metrics.json`, `version_control_status.json`,
`dashboard_feed.jsonl`, and `run_summaries.jsonl` under the configured
read-model directory. JSON models include `generated_at`, `source_hashes`,
`last_event_seq`, and `source_event_id`; JSONL records include generation and
source event fields per record.

The static dashboard renderer computes `read_model_freshness` by comparing
read-model event references and event source hashes with the current event-log
projection. Stale renders remain read-only but include machine-readable warning
metadata, a visible rebuild command hint, and a disabled rebuild request form
that points operators to server mode. Missing freshness metadata is treated as
an unknown freshness warning and receives the same request affordance.

## UI Shape

The dashboard layout has a top workspace bar, a left plan panel, a right
workflow graph, and a bottom inspector chat or change-request console.

The top bar should support workspace selection, workflow-history switching
inside the current workspace, workflow status, Git checkpoint status, runner
configuration, doctor checks, start/pause/resume/stop through control requests,
logs, settings, and approval alerts. Workflow-history selection is a
visualization operation; it must not update `.loopplane/current_workflow.json`
unless the user explicitly performs a workflow switch, restore, or fork action.
Archived and read-only workflows should be labeled and protected from accidental
mutation.

The plan panel should show phase progress, task status, validation status,
active task, blocked/partial/skipped/failed indicators, and graph-node focus.

The workflow graph should include nodes for brief creation, planner run,
auditor run, plan activation, worker run, validation, reconciliation, recovery,
background job, human approval, change request, inspector query, and final
verification. Node hover and click details are read-model backed and may expose
prompts, final output, reports, logs, validation, artifacts, sanitized project
changes, Git checkpoint summaries, diff summaries, and events.

The standalone dashboard now derives run-node detail sections into
`run_summaries.jsonl` and embeds a `node_details` payload for the static UI.
Run-backed graph nodes expose prompt, final output, report, logs, validation,
artifacts, project changes, Git checkpoint status, diff summary, and events
when records exist, and show explicit empty states when optional evidence is
absent. Detail payloads must stay bounded and sanitized: no raw `.git/`
internals, checkpoint refs, repository roots, absolute private paths, or common
secret patterns should be exposed.

## Request APIs

The recommended API shape includes workspace listing, workflow init, planning,
audit, activation, control requests, status, plan index, graph, run detail,
approvals, chat, and change requests. Mutating APIs write request records and
must not directly mutate runtime state.

The standalone implementation supports a framework-free local server through
`loopplane dashboard --port <port> --project <project>`. The no-port command keeps
the offline/read-only static bundle path. Server mode serves the same read
model backed UI, exposes JSON endpoints for the current workspace workflow, and
uses token authentication by default.

The implemented workspace selector reads `.loopplane/workflow_registry.json`.
Static renders embed available workflow read-model snapshots for browser-local
switching and label missing snapshots as unavailable. Server mode additionally
serves `GET /api/workspace`, `GET /api/workspace/workflows`,
`GET /api/workspace/workflows/<workflow_id>`, and
`GET /api/workflows/<workflow_id>/dashboard-data`, all resolved through the
project-local workspace registry. The read endpoints refresh displayed panels
without mutating authoritative runtime state or `.loopplane/current_workflow.json`.

`POST /api/workspace/workflows` is an explicit workflow-create API. It uses the
same guarded implementation as `loopplane workflow create --brief ...`, so it may
allocate a canonical workflow root, append the workspace registry, and update
the current workflow pointer only after workflow-create safety checks pass. It
is distinct from dashboard control routes that merely append request records.

`loopplane dashboard list` is the CLI inspection companion for server mode. It
enumerates workflows from the current workspace registry, checks each
workflow's configured `dashboard_server.json` path, reads matching token-free
machine-local discovery records from `$LOOPPLANE_HOME/dashboard/servers.json`,
redacts sensitive fields, and reports a successful no-active-dashboard result
when no server records are present. The LOOPPLANE_HOME index is discovery-only and
must not override project-local workflow truth or carry dashboard tokens.

The implemented run-detail endpoint serves
`GET /api/workflows/<workflow_id>/runs/<run_id>` from `run_summaries.jsonl`.
The endpoint returns the same sanitized node-detail sections used by the
static graph panel and does not directly inspect protected runtime internals or
mutate plan, runtime, validation, event, read-model, or Git state.

The implemented planning controls render in the workflow controls panel.
Static/offline renders show planner, auditor, and plan-activation controls as
disabled read-only affordances. Server mode enables them only for mutable,
policy-eligible workflow histories and POSTs to the existing planning request
endpoints. Those endpoints append `plan`, `audit`, and `activate_plan` records
to `.loopplane/requests/dashboard_requests.jsonl`; they do not run planner code
directly or edit `PLAN.md`, `PLAN_DRAFT.md`, runtime state, event logs, or
completion markers from the browser.

The implemented execution controls render alongside the planning controls.
Static/offline renders show start, pause, resume, and stop as disabled
read-only affordances. Server mode enables them only for mutable,
policy-eligible workflow histories and POSTs to
`/api/workflows/<workflow_id>/control-requests`. The endpoint appends
`start`, `pause`, `resume`, and `stop` records to
`.loopplane/runtime/control_requests.jsonl`; it does not directly mutate `PLAN.md`,
runtime `state.json`, event logs, validation outputs, supervisor metadata, or
completion markers. The scheduler or detached supervisor applies those control
requests at safe points.

Control requests may also include attach, migrate, cancel run, cancel
background job, rebuild read models, and run final verifier. The scheduler
applies them.

The implemented freshness warning includes a read-model rebuild request path.
Static/offline dashboards show the rebuild action as disabled and keep the CLI
`loopplane rebuild-read-models --workflow <workflow_id>` hint visible for the
selected workflow. Server mode enables
`POST /api/workflows/<workflow_id>/rebuild-read-models` for mutable workflow
histories; that endpoint appends a `rebuild_read_models` record to
`.loopplane/runtime/control_requests.jsonl`, preserves the current freshness
diagnostics in the request payload, and does not rebuild read models or edit
authoritative plan, runtime, event, validation, completion marker, or Git state
from dashboard code.

The implemented approval panel renders pending and recent human approval
requests with status, type, task/run context, message, scope, timestamps, and
sanitized evidence or source path references. Static/offline dashboards show
the approval data read-only. Server mode enables approve/reject actions only
for mutable workflow histories with interactive approval enabled; responses
POST to `/api/workflows/<workflow_id>/approvals/<approval_id>/respond`, append
`.loopplane/runtime/human_approval_responses.jsonl`, refresh the approval list, and
do not edit authoritative plan, runtime state, event, validation, or read-model
files directly.

The implemented bottom inspector console renders static/offline chat and
change-request controls as disabled read-only affordances, with recent
chat/change-request records from the selected workflow when request files are
available. Server mode enables read-only chat and change-request submission
only for mutable workflow histories. Chat POSTs to
`/api/workflows/<workflow_id>/chat` with an inspector-role runner, appends
`chat_requests.jsonl` and `chat_responses.jsonl`, and answers from the
inspector read-only allowlist. Change requests POST to
`/api/workflows/<workflow_id>/change-requests` and append
`.loopplane/requests/change_requests.jsonl`. The browser refreshes dashboard data
after either submit so pending/recent records are visible without rebuilding
read models. Neither path directly edits `PLAN.md`, runtime state, event logs,
validation files, read models, Git internals, or completion markers.

Change requests progress through statuses such as pending review, planner
reviewing, needs user approval, approved, rejected, applied, superseded, and
failed. Plan mutation happens only through the approved change request protocol.

## Security Requirements

The dashboard must bind to `127.0.0.1` by default, require token
authentication by default, protect mutating APIs with token and same-origin or
CSRF controls, serve file reads only from allowlisted paths, never expose
arbitrary shell execution, never expose `.git/` internals directly, log request
bodies with redaction, and require approval or trusted local mode for runner
configuration changes.

The standalone dashboard renders a runner configuration panel from
`.loopplane/config/agent_runners.json`. When dashboard trusted-local mode is
disabled, commands are hidden and the panel is read-only. When trusted-local
mode is enabled, the panel shows redacted command labels, prompt delivery mode,
timeout, enabled state, role, adapter, and doctor diagnostics. Server mode may
create `runner_configuration` request records through the dashboard API, but it
does not edit `agent_runners.json` directly. Browser-submitted runner
configuration requests reject environment values, shell fragments, and local
path commands; exact machine-local paths should be configured with
`loopplane configure-agent` on the local CLI, which writes non-portable runner
overrides under the resolved `LOOPPLANE_HOME` instead of portable workflow
truth.

Version-control information must come from sanitized read models such as
`version_control_status.json`, `git_checkpoints.jsonl`, and per-run diff
metadata rather than direct `.git/` reads.
